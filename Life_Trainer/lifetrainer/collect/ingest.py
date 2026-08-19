"""폰이 밀어 넣는 ActivityWatch 데이터를 받는다 (수신 경로의 단일 정의).

**왜 별도 모듈인가**: 같은 페이로드가 두 경로로 들어온다.

    폰 → Cloudflare Tunnel → `POST /ingest/aw`   (상시)
    adb 로 뽑은 파일 → `lt import`                (Phase 0 표본, 장애 시 수동 복구)

둘이 각자 파싱하면 "같은 값을 여러 곳에서 각자 계산했다"(반복 실패 2번)가 된다.
검증·삽입은 전부 여기 `apply_payload` 한 곳에서만 한다.

**형식은 ActivityWatch 의 export 를 그대로 받는다** (`/api/0/export`,
`/api/0/buckets/<id>/export` 의 출력):

    {"buckets": {"<bucket_id>": {"type": ..., "client": ..., "hostname": ...,
                                 "events": [{"timestamp": ..., "duration": ..., "data": {...}}]}}}

우리 봉투를 새로 만들지 않는 이유는 **adb 로 뽑은 파일이 변환 없이 그대로 들어가야**
하기 때문이다. 폰에서 실물을 뽑아보기도 전에 우리 형식을 정하면, 조사 문서를 사실로
믿는 실패(반복 실패 1번)를 형식 설계에서 되풀이하게 된다.

삽입은 `aw_sync.upsert_bucket`/`upsert_events` 를 그대로 재사용한다. `aw_event` 의
PK 가 `(bucket_id, ts)` 라 **같은 페이로드를 몇 번 넣어도 결과가 같다**.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from lifetrainer.collect.aw_client import AWEvent
from lifetrainer.collect.aw_sync import bucket_type, device_kind_for, upsert_bucket, upsert_events
from lifetrainer.db import upsert_device
from lifetrainer.timeutil import parse_iso

logger = logging.getLogger(__name__)

# 한 번에 받을 이벤트 수 상한. 본문 바이트 상한(`cfg.ingest.max_body_bytes`)과 별개로
# 건다 — 짧은 이벤트가 아주 많은 페이로드는 바이트로는 작아도 삽입이 오래 걸린다.
MAX_EVENTS_PER_PAYLOAD = 50_000


class IngestError(Exception):
    """페이로드 형식 오류·미등록 기기 등 수신 거부 사유."""


@dataclass
class IngestResult:
    """한 번의 수신 결과. 무엇이 들어갔고 **무엇이 안 들어갔는지**를 함께 담는다."""

    device: str
    device_id: int
    buckets: int = 0
    events: int = 0
    skipped: list[str] = field(default_factory=list)
    # 이번에 들어온 이벤트가 걸친 시간 범위. 폰 데이터는 **늦게 도착하므로**
    # 호출자가 "오늘"이 아니라 실제로 건드린 날짜를 다시 롤업해야 한다.
    ts_min: float | None = None
    ts_max: float | None = None


def _require_mapping(value: object, what: str) -> dict:
    if not isinstance(value, dict):
        raise IngestError(f"{what} 은(는) 객체여야 합니다")
    return value


def _parse_events(bucket_id: str, raw_events: object) -> list[AWEvent]:
    """버킷 하나의 이벤트 목록을 파싱한다. 형식이 깨지면 IngestError.

    시각은 ActivityWatch 와 같은 RFC3339 문자열로 받는다. epoch 숫자를 함께 허용하지
    않는 이유는, 초와 밀리초를 조용히 헷갈리면 하루가 통째로 어긋나기 때문이다.
    """
    if not isinstance(raw_events, list):
        raise IngestError(f"버킷 {bucket_id!r} 의 events 는 배열이어야 합니다")

    events: list[AWEvent] = []
    for item in raw_events:
        if not isinstance(item, dict):
            raise IngestError(f"버킷 {bucket_id!r} 의 이벤트는 객체여야 합니다")
        try:
            ts = parse_iso(str(item["timestamp"]))
        except KeyError:
            raise IngestError(f"버킷 {bucket_id!r} 의 이벤트에 timestamp 가 없습니다") from None
        except Exception as exc:  # noqa: BLE001 - 어떤 형태로 깨지든 거부로 통일한다
            raise IngestError(f"버킷 {bucket_id!r} 의 timestamp 를 해석할 수 없습니다: {exc}") from exc

        try:
            duration = float(item.get("duration", 0.0) or 0.0)
        except (TypeError, ValueError):
            raise IngestError(f"버킷 {bucket_id!r} 의 duration 이 숫자가 아닙니다") from None
        if duration < 0:
            raise IngestError(f"버킷 {bucket_id!r} 에 음수 duration 이 있습니다")

        data = item.get("data") or {}
        if not isinstance(data, dict):
            raise IngestError(f"버킷 {bucket_id!r} 의 이벤트 data 는 객체여야 합니다")

        event_id = item.get("id")
        events.append(
            AWEvent(
                bucket_id=bucket_id,
                ts=ts,
                duration=duration,
                data=dict(data),
                event_id=event_id if isinstance(event_id, int) else None,
            )
        )
    return events


def apply_payload(
    conn: sqlite3.Connection,
    payload: object,
    *,
    device: str,
    allow_new_device: bool = False,
    device_kind: str = "phone",
) -> IngestResult:
    """페이로드를 검증하고 `aw_bucket`/`aw_event` 에 넣는다. 호출자가 트랜잭션을 연다.

    `allow_new_device` 가 거짓이면 **`device` 테이블에 이미 있는 기기만** 받는다.
    네트워크로 열린 경로(`POST /ingest/aw`)가 그렇다 — 서명 비밀키가 새더라도
    임의의 기기 이름이 무한히 생기지는 않게 하는 두 번째 방어선이다.
    로컬에서 사람이 직접 돌리는 `lt import` 는 참으로 넘겨 새 기기를 만들 수 있다.
    """
    root = _require_mapping(payload, "페이로드")
    buckets = _require_mapping(root.get("buckets"), "buckets")
    if not buckets:
        raise IngestError("buckets 가 비어 있습니다")

    row = conn.execute("SELECT id, active FROM device WHERE name = ?", (device,)).fetchone()
    if row is None:
        if not allow_new_device:
            raise IngestError(f"등록되지 않은 기기입니다: {device!r} (먼저 `lt import` 로 등록하세요)")
        device_id = upsert_device(conn, device, kind=device_kind)
    else:
        if not row["active"]:
            raise IngestError(f"비활성 기기입니다: {device!r}")
        device_id = int(row["id"])

    result = IngestResult(device=device, device_id=device_id)

    # 이벤트 총량을 **삽입 전에** 센다. 절반 넣고 거절하면 재전송이 어디까지
    # 들어갔는지 알 수 없어진다.
    total = 0
    parsed: list[tuple[str, dict, list[AWEvent]]] = []
    for bucket_id, meta in buckets.items():
        if not isinstance(bucket_id, str) or not bucket_id:
            raise IngestError("버킷 ID 가 비어 있습니다")
        meta = _require_mapping(meta, f"버킷 {bucket_id!r} 의 메타")
        events = _parse_events(bucket_id, meta.get("events", []))
        total += len(events)
        if total > MAX_EVENTS_PER_PAYLOAD:
            raise IngestError(f"이벤트가 너무 많습니다 (상한 {MAX_EVENTS_PER_PAYLOAD}개)")
        parsed.append((bucket_id, meta, events))

    for bucket_id, meta, events in parsed:
        btype = bucket_type(bucket_id, meta)
        if btype == "unknown":
            # 거부하지 않고 넘어간다 — 상류가 새 워처를 붙였을 때 폰 전체 수신이
            # 실패하는 것보다, 아는 것만 받고 무엇을 건너뛰었는지 말하는 편이 낫다.
            result.skipped.append(f"{bucket_id} (알 수 없는 버킷 타입)")
            continue
        # 메타에 events 를 담은 채로 넘기지 않는다 — aw_bucket 이 쓰는 필드만 본다.
        bucket_meta = {k: v for k, v in meta.items() if k != "events"}
        upsert_bucket(conn, bucket_id, bucket_meta, device_id=device_id)
        result.buckets += 1
        result.events += upsert_events(conn, events)
        for ev in events:
            result.ts_min = ev.ts if result.ts_min is None else min(result.ts_min, ev.ts)
            result.ts_max = ev.ts_end if result.ts_max is None else max(result.ts_max, ev.ts_end)

    if result.buckets == 0:
        raise IngestError("받을 수 있는 버킷이 하나도 없습니다: " + ", ".join(result.skipped))

    logger.info(
        "기기 %s: 버킷 %d개 / 이벤트 %d건 수신 (건너뜀 %d)",
        device,
        result.buckets,
        result.events,
        len(result.skipped),
    )
    return result


def infer_device_name(payload: object) -> str | None:
    """페이로드에서 기기 이름을 추론한다 (`lt import` 가 `--device` 없이 쓸 때).

    버킷 메타의 `hostname` 을 쓴다. 서로 다른 hostname 이 섞여 있으면 **고르지 않는다** —
    사람이 `--device` 로 정하게 한다. 여기서 하나를 골라 버리면 두 기기의 기록이
    한 이름으로 합쳐져 나중에 되돌릴 수 없다.
    """
    if not isinstance(payload, dict):
        return None
    buckets = payload.get("buckets")
    if not isinstance(buckets, dict):
        return None
    names = {
        str(meta["hostname"])
        for meta in buckets.values()
        if isinstance(meta, dict) and meta.get("hostname")
    }
    return names.pop() if len(names) == 1 else None


def kind_for_payload(payload: object) -> str:
    """페이로드의 버킷 타입으로 기기 종류를 추론한다 (새 기기를 만들 때만 쓴다)."""
    if not isinstance(payload, dict):
        return "other"
    buckets = payload.get("buckets")
    if not isinstance(buckets, dict):
        return "other"
    for bucket_id, meta in buckets.items():
        if isinstance(bucket_id, str) and isinstance(meta, dict):
            if device_kind_for(bucket_type(bucket_id, meta)) == "phone":
                return "phone"
    return "laptop"
