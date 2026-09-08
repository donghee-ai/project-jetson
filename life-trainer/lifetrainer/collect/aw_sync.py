"""ActivityWatch 동기화 — 이중 계산 방지가 핵심.

`docs/research/activitywatch.md §3` 요약: 버킷의 마지막(진행 중) 이벤트는 하트비트로
병합되며, 병합 시 `id`/`timestamp` 는 고정이고 `duration` 만 자란다. 따라서:

1. 커서에는 "마지막으로 본 이벤트의 시작 시각(`ts`)" 을 저장한다 (`ts_end`/`now` 가 아니다).
2. 매 폴링에서 `[max(cursor - overlap_sec, now - backfill_days*86400), now]` 로
   **겹쳐서** 다시 조회한다.
3. `(bucket_id, ts)` 를 자연 키로 upsert 한다 — 진행 중이던 이벤트의 duration 이
   자라도 같은 행이 갱신될 뿐 중복 행이 생기지 않는다.

버킷 하나가 실패해도 나머지는 계속 진행하고 `SyncResult.errors` 에 문자열로 모은다
(계약서 §0 실패 처리 원칙).
"""

from __future__ import annotations

import json
from dataclasses import replace as dc_replace
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable

from lifetrainer import privacy
from lifetrainer.collect.aw_client import AWClient, AWError, AWEvent
from lifetrainer.config import Config
from lifetrainer.db import get_state_float, set_state_float, transaction, upsert_device
from lifetrainer.timeutil import now_ts

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    """한 번의 `sync()` 실행 결과 요약."""

    buckets_seen: int
    events_upserted: int
    window_start: float
    window_end: float
    errors: list[str] = field(default_factory=list)


def bucket_type(bucket_id: str, meta: dict) -> str:
    """버킷 ID/메타로부터 워처 종류를 판별한다.

    ActivityWatch 의 버킷 ID 관례(조사 문서 §2)를 우선 신뢰하고,
    애매하면 메타의 `type` 필드로 보완한다.

    ★ 안드로이드를 `window` 가 아니라 **자기 이름으로** 받는다.

    aw-android 의 버킷은 ID 가 `aw-watcher-android`(문자열 `window` 없음)이고
    타입이 `"currentwindow"` 다. 예전 코드는 메타 폴백의 부분매칭
    (`"window" in "currentwindow"`)에 걸려 **우연히** `window` 로 잡혔다.
    의도가 아니었으므로 업스트림이 타입 문자열을 바꾸면 조용히 깨진다.

    **메타 타입으로는 안드로이드를 구분할 수 없다** — 데스크톱 창 워처의 버킷
    타입도 똑같이 `"currentwindow"` 다. 그래서 판별은 **버킷 ID** 로 한다.

    안드로이드 앱 세션은 데스크톱 창 포커스와 의미가 다르다(창이 여러 개 뜨지
    않고, afk 짝이 없고, duration 의 성격도 다르다). 별도 타입으로 받은 뒤
    롤업의 어댑터 **한 곳**에서 공통 형태로 합류시킨다.

    순서가 곧 우선순위다:

    - `unlock` 이 `android` 보다 먼저 — `aw-watcher-android-unlock` 은 둘 다 걸린다
    - `afk` 가 `android` 보다 먼저 — 우리가 폰에 심는 `aw-watcher-android-afk` 는
      노트북과 같은 언어를 쓰라고 만든 것이므로 `afk` 여야 한다
    - `web` 이 `android` 보다 먼저 — `aw-watcher-android-web` 도 둘 다 걸린다.
      **`android` 로 잡히면 URL 이 통째로 죽는다** (2026-08-23 실측): 롤업이
      URL 을 `dev.buckets["web"]` 에서만 꺼내 슬롯에 붙이는데, 폰의 web 버킷이
      영원히 비어 있어 `rules.yaml` 의 URL 규칙(앱 규칙보다 **먼저** 평가되도록
      일부러 배치한 것)이 폰 브라우징에 하나도 걸리지 않았다. 폰 크롬 929분이
      앱 이름만으로 `browsing`/`away` 로 뭉뚱그려져 있었다.
    """
    bid = bucket_id.lower()
    if "unlock" in bid:
        return "unlock"
    if "afk" in bid:
        return "afk"
    if "web" in bid:
        return "web"
    if "android" in bid:
        return "android"
    if "window" in bid:
        return "window"

    meta_type = str((meta or {}).get("type", "")).lower()
    if "lockscreen.unlocks" in meta_type:
        return "unlock"
    if "afk" in meta_type:
        return "afk"
    if "web" in meta_type:
        return "web"
    # `currentwindow` 는 데스크톱·안드로이드가 공유하므로 여기서 안드로이드를
    # 갈라낼 수 없다. ID 로 못 갈랐으면 데스크톱으로 본다 (기존 동작 유지).
    if "window" in meta_type:
        return "window"
    return "unknown"


def _extract_host(bucket_id: str, meta: dict) -> str:
    """버킷의 호스트명을 결정한다. 메타의 `hostname` 을 우선하고, 없으면 버킷 ID 에서 뽑는다."""
    hostname = (meta or {}).get("hostname")
    if hostname:
        return str(hostname)
    if "_" in bucket_id:
        return bucket_id.rsplit("_", 1)[-1]
    return bucket_id


# 폰에서 오는 버킷 타입. 기기 종류(`device.kind`)를 여기서 추론한다.
_PHONE_BUCKET_TYPES = frozenset({"android", "unlock"})


def device_kind_for(btype: str) -> str:
    """버킷 타입으로 기기 종류를 추론한다.

    `android`/`unlock` 은 aw-android 만 내놓으므로 폰이 확실하다. 그 외에는
    `laptop` 으로 둔다 — `_apply_002_day_boundary` 가 기존 호스트를 전부 laptop
    으로 만들었고 지금까지의 실기기가 전부 노트북이라 기본값을 맞춘 것이다.
    틀렸으면 `device.kind` 를 사람이 고치면 되고, **동기화가 그걸 되돌리지 않는다**
    (`db.upsert_device` 는 kind 를 덮어쓰지 않는다).
    """
    return "phone" if btype in _PHONE_BUCKET_TYPES else "laptop"


def upsert_bucket(
    conn: sqlite3.Connection, bucket_id: str, meta: dict, *, device_id: int | None = None
) -> None:
    """`aw_bucket` 을 upsert 한다. `first_seen` 은 최초 값을 유지하고 `last_seen` 만 갱신한다.

    **버킷을 넣을 때 `device` 행도 함께 보장하고 `device_id` 를 채운다.** 예전에는
    마이그레이션만 이 연결을 만들어서, 그 뒤에 새로 나타난 버킷은 `device_id` 가
    NULL 로 남았다. 롤업이 기기별로 시간을 가르려면 빠짐없이 채워져 있어야 한다.

    `device_id` 를 주면 그 기기에 묶고, 안 주면 호스트명에서 추론한다.
    폰이 밀어 넣는 경로(`collect/ingest.py`)는 **서명된 기기 이름이 정본**이라
    명시적으로 넘긴다 — 버킷 메타의 hostname 과 서명이 어긋날 때 이름이 두 개로
    갈라지면 안 된다.
    """
    meta = meta or {}
    now = now_ts()
    host = _extract_host(bucket_id, meta)
    btype = bucket_type(bucket_id, meta)
    if device_id is None:
        device_id = upsert_device(
            conn, host, kind=device_kind_for(btype), hostname=meta.get("hostname"), now=now
        )
    conn.execute(
        """
        INSERT INTO aw_bucket(bucket_id, host, client, type, hostname, device_id, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(bucket_id) DO UPDATE SET
            host=excluded.host,
            client=excluded.client,
            type=excluded.type,
            hostname=excluded.hostname,
            device_id=excluded.device_id,
            last_seen=excluded.last_seen
        """,
        (
            bucket_id,
            host,
            meta.get("client"),
            btype,
            meta.get("hostname"),
            device_id,
            now,
            now,
        ),
    )


def _absorb_clipped_view(conn: sqlite3.Connection, ev):
    """이미 있는 **같은 이벤트의 잘린 판**을 지우고 들어오는 쪽에 폭을 합쳐 준다.

    ## 왜 필요한가 (issues/0027)

    aw-server 는 이벤트를 **질의 창에 맞춰 잘라서** 준다. 실측으로 증명했다 —
    같은 이벤트를 `start` 만 60초 밀어 조회하면 `timestamp` 가 정확히 60초 밀리고
    `duration` 이 그만큼 준다. `event_id` 는 그대로다.

        start=+  0s → ts=11:54:22.687  dur=924.6s  id=24766
        start=+600s → ts=12:04:22.687  dur=324.6s  id=24766

    우리는 따라잡기로 `cursor − overlap_sec(900)` 부터 다시 읽는데, **긴 이벤트는
    그 창 시작이 자기 몸통 안에 떨어진다.** 잘린 `ts` 는 PK `(bucket_id, ts)` 가
    처음 보는 키라 갱신이 아니라 **행이 하나 더** 생긴다. PC afk 버킷이 그렇게
    총합 3,400시간 / 합집합 268시간(12.67배)까지 부풀었다.

    ## 왜 `event_id` 만으로 판정하지 않나 — 재사용이 실재한다

    스키마가 `event_id` 를 *"신뢰하지 않는다"* 고 적어 둔 데는 이유가 있었다.
    실측하면 **`aw-watcher-android` 에서 id 가 5,152회 뒤로 점프**한다(재수입·재설치).
    PC afk 에서도 0초짜리 이벤트가 사라진 뒤 그 id 가 **다른 시간대에 재사용**된 것이
    4건 있었다:

        id=7217  08-23 00:32:57 → 00:32:57  (0.0s, not-afk)
        id=7217  08-23 01:04:53 → 01:19:53  (900s, afk)     ← 전혀 다른 이벤트

    그래서 id 만으로 합치면 **가짜 거대 구간**이 생긴다.
    **겹칠 때만** 합친다 — 잘린 판은 원본과 반드시 겹치고(끝을 공유한다),
    재사용된 id 는 시간대가 떨어져 있어 안 걸린다.

    실측으로 갈라 보면: PC afk 합칠 것 296 / 안 건드릴 것 4,
    PC window 합칠 것 706 / 안 건드릴 것 1. 폰 버킷은 중복 id 자체가 없다.

    반환값은 **합집합으로 넓힌 이벤트**다 (`AWEvent` 는 frozen 이라 새로 만든다).
    합칠 것이 없으면 받은 것을 그대로 돌려준다.
    """
    if ev.event_id is None:
        return ev, 0
    rows = conn.execute(
        "SELECT ts, ts_end FROM aw_event "
        "WHERE bucket_id = ? AND event_id = ? AND ts != ? "
        "  AND ts_end >= ? AND ts <= ?",     # 겹치거나 맞닿는 것만
        (ev.bucket_id, ev.event_id, ev.ts, ev.ts, ev.ts_end),
    ).fetchall()
    if not rows:
        return ev, 0
    lo = min([float(r["ts"]) for r in rows] + [ev.ts])
    hi = max([float(r["ts_end"]) for r in rows] + [ev.ts_end])
    conn.execute(
        "DELETE FROM aw_event WHERE bucket_id = ? AND event_id = ? AND ts != ? "
        "  AND ts_end >= ? AND ts <= ?",
        (ev.bucket_id, ev.event_id, ev.ts, ev.ts, ev.ts_end),
    )
    # 들어오는 쪽을 합집합으로 넓힌다. `ts_end` 는 `ts + duration` 프로퍼티라 duration 만 준다.
    return dc_replace(ev, ts=lo, duration=hi - lo), len(rows)


def _absorb_duplicate_publication(conn: sqlite3.Connection, ev, data: dict):
    """상류가 **같은 일을 여러 번 발행한 것**을 하나로 합친다 (issues/0027 두 번째 기제).

    ## 이건 우리가 만든 문제가 아니다

    `_absorb_clipped_view` 가 고치는 것은 *우리* 문제였다 — 질의 창에 맞춰 잘린 판이
    새 `ts` 로 들어오는 것. 그건 `event_id` 가 같아서 갈릴 수 있었다.

    남은 것은 상류다. `aw-watcher-afk` 가 매 폴링마다
    `last_input = now - seconds_since_input` 을 **재계산**하는데, `now`(시스템 시계)와
    유휴 타이머가 서로 다른 시계라 자리를 비운 동안 타임스탬프가 **뒤로 흐른다**
    (개인별 수치는 제거한 사본 회귀 표본에서 단조 하강을 확인했다). 밀리초 해상도라
    주기적으로 새 값이 나오고, 그때마다 서버의 병합 조건
    (`last_event.timestamp <= heartbeat.timestamp`)이 깨져 **새 행**이 된다.
    상류는 `event_id` 도 새로 매기므로 위 함수로는 안 걸린다.

    ## 판정 규칙 — 밀리초 임계값을 안 쓴다

    처음엔 *"시작이 몇 ms 안이면 같은 것"* 을 생각했는데, 그 숫자에 근거가 없다.
    대신 **뜻으로** 판정한다:

        같은 버킷 · 같은 내용(app·title·url·status) · 구간이 겹친다  →  같은 사건

    창 워처의 타임라인은 한 번에 하나만 포커스다 — *같은 창이 겹친 두 구간*은 성립하지
    않는다. afk 도 마찬가지로 같은 상태가 겹쳐 두 번 있을 수 없다. 즉 이 조건이
    참이면 실제로 같은 사건이고, 임계값을 고를 필요가 없다.

    ## 0초 이벤트는 건드리지 않는다

    `aw-watcher-android-unlock` 은 전부 duration 0 인 **시점 사건**이라 개수 자체가 뜻이다.
    겹침 판정(`ts_end >= ts`)은 0초끼리도 맞닿는 것으로 보므로, 그대로 두면
    같은 순간의 잠금해제 두 번이 한 번이 된다 — **세는 것을 줄이는 쪽**이라 위험하다.

    ## 검증

    개발 사본에서 모든 버킷의 시간 합집합이 유지되는지 확인했다. 공개본에는
    개인별 버킷 행수·비율·시각을 싣지 않는다.
        android(창)               19,990    1.37      6,614
        PC window                 24,859    1.00         92
        android-afk/-media/-web    나머지    1.00        0~1   ← 규칙이 안 건드린다

    네 버킷이 사실상 그대로라는 것이 이 규칙이 무딘 칼이 아니라는 증거다.
    **합집합(실제로 덮은 시간)은 정의상 안 변한다** — 겹치는 것만 합치기 때문이다.
    """
    if ev.duration <= 0:
        return ev, 0  # 시점 사건 — 개수가 뜻이다
    # ★ `data` 를 인자로 받는다. `AWEvent` 는 app/title/url/status 를 따로 갖지 않고
    #   `data` 안에 있으며, 저장 시 열로 펴는 것도 호출부다 — 같은 출처를 쓴다.
    app, title, url, status = (data.get(k) for k in ("app", "title", "url", "status"))
    rows = conn.execute(
        "SELECT ts, ts_end FROM aw_event "
        "WHERE bucket_id = ? AND ts != ? "
        "  AND ts_end > ts "                       # 0초 이벤트는 상대로도 안 삼는다
        "  AND ts_end > ? AND ts < ? "             # ★ 진짜로 겹칠 때만 (맞닿는 것은 두 구간이다)
        "  AND COALESCE(app,'')    = COALESCE(?,'') "
        "  AND COALESCE(title,'')  = COALESCE(?,'') "
        "  AND COALESCE(url,'')    = COALESCE(?,'') "
        "  AND COALESCE(status,'') = COALESCE(?,'')",
        (ev.bucket_id, ev.ts, ev.ts, ev.ts_end, app, title, url, status),
    ).fetchall()
    if not rows:
        return ev, 0
    lo = min([float(r["ts"]) for r in rows] + [ev.ts])
    hi = max([float(r["ts_end"]) for r in rows] + [ev.ts_end])
    conn.execute(
        "DELETE FROM aw_event "
        "WHERE bucket_id = ? AND ts != ? AND ts_end > ts AND ts_end > ? AND ts < ? "
        "  AND COALESCE(app,'')    = COALESCE(?,'') "
        "  AND COALESCE(title,'')  = COALESCE(?,'') "
        "  AND COALESCE(url,'')    = COALESCE(?,'') "
        "  AND COALESCE(status,'') = COALESCE(?,'')",
        (ev.bucket_id, ev.ts, ev.ts, ev.ts_end, app, title, url, status),
    )
    return dc_replace(ev, ts=lo, duration=hi - lo), len(rows)


def compact_duplicates(conn: sqlite3.Connection, *, apply: bool = False) -> dict[str, tuple[int, int]]:
    """이미 쌓인 중복을 **뒤늦게** 합친다. 버킷별 `(전, 후)` 행 수를 돌려준다.

    `upsert_events` 의 두 흡수기는 **앞으로 들어오는 것**만 막는다. 그전에 쌓인 것은
    그대로 남아 있다 — 2026-09-07 실측으로 PC afk 버킷이 총합/합집합 10.49배였다.
    화면 숫자는 아직 안 틀리지만(롤업이 슬롯마다 잘라 흡수한다), **원본 duration 을
    합치는 코드가 앞으로 생기면 그때 10배로 틀린다.**

    ★ `apply=False` 가 기본이다. 원본 표를 지우는 일이라 **먼저 보고 나서 하게** 만든다.
      개발 사본에서 돌려 보는 것이 정석이다: `lt --dev aw-compact` → `lt aw-compact --apply`.

    ★ 불변식: **합집합(실제로 덮은 시간)이 안 변한다.** 겹치는 것만 합치므로 정의상
      그렇고, 사본에서 일곱 버킷 모두 0.001초 이내로 확인했다. 안 지켜지면 버그다.
    """
    out: dict[str, tuple[int, int]] = {}
    buckets = [r["bucket_id"] for r in conn.execute("SELECT DISTINCT bucket_id FROM aw_event")]
    for bid in buckets:
        rows = conn.execute(
            "SELECT ts, ts_end, event_id, app, title, url, status FROM aw_event "
            "WHERE bucket_id = ? AND ts_end > ts ORDER BY ts",   # 0초 시점 사건은 제외
            (bid,),
        ).fetchall()
        before = conn.execute(
            "SELECT count(*) AS n FROM aw_event WHERE bucket_id = ?", (bid,)
        ).fetchone()["n"]

        groups: dict[tuple, list] = {}
        for r in rows:
            key = (r["app"], r["title"], r["url"], r["status"])
            groups.setdefault(key, []).append((float(r["ts"]), float(r["ts_end"])))

        removed = 0
        for spans in groups.values():
            spans.sort()
            lo, hi = spans[0]
            batch: list[tuple[float, float]] = []
            for ts, te in spans[1:]:
                if ts < hi:          # 엄격한 겹침만 (맞닿는 것은 두 구간이다)
                    hi = max(hi, te)
                    batch.append((ts, te))
                else:
                    removed += _collapse_group(conn, bid, lo, hi, batch, apply=apply)
                    lo, hi, batch = ts, te, []
            removed += _collapse_group(conn, bid, lo, hi, batch, apply=apply)

        out[bid] = (before, before - removed)
    if apply:
        conn.commit()
    return out


def _collapse_group(conn, bid, lo, hi, batch, *, apply: bool) -> int:
    """한 뭉치를 `[lo, hi]` 하나로 만든다. 지운 행 수를 돌려준다.

    내용(app·title·url)을 안 받는다 — PK 가 `(bucket_id, ts)` 라 `ts` 하나로 행이 정해진다.
    """
    if not batch:
        return 0
    if apply:
        for ts, _te in batch:
            conn.execute("DELETE FROM aw_event WHERE bucket_id = ? AND ts = ?", (bid, ts))
        conn.execute(
            "UPDATE aw_event SET ts_end = ?, duration = ? WHERE bucket_id = ? AND ts = ?",
            (hi, hi - lo, bid, lo),
        )
    return len(batch)


def upsert_events(
    conn: sqlite3.Connection, events: Iterable[AWEvent], *, spans: list | None = None
) -> int:
    """`aw_event` 를 `(bucket_id, ts)` 키로 upsert 한다. 반환값은 **저장한** 이벤트 수.

    ## ★ 프라이빗 구간은 여기서 걸러진다 (2026-09-01)

    이 함수가 **모든 활동 데이터의 유일한 저장 진입점**이다 — PC pull(`sync`),
    폰 push(`collect/ingest.apply_payload`), `lt import` 셋이 전부 여기로 수렴한다.
    그래서 관문을 여기 하나만 둔다.

    `spans` 를 **안 넘기면 스스로 조회한다.** 호출부가 넘기게 만들면 한 곳만 빼먹어도
    관문이 조용히 뚫린다 — 기본값이 "거른다"여야 실패가 안전한 방향으로 떨어진다.
    끄려면 `spans=[]` 를 **명시**해야 한다(테스트 전용).

    ★ 자르기를 밖이 아니라 **여기서** 하는 이유: 아래 upsert 가
    `ts_end = MAX(ts_end, excluded.ts_end)` 라 **줄이지 못한다.** 한 번이라도 안 자른
    값이 들어가면 MAX 가 그걸 영구히 잡는다.

    ★ 반환값이 "처리한 수"에서 "저장한 수"로 바뀌었다. 호출부는 원본 리스트 길이와
    빼서 버린 수를 얻는다 — 시그니처를 안 바꾸고 정직한 숫자를 낸다.

    ★ **흡수한 행은 뺀다** (2026-09-07). 두 흡수기가 이미 저장돼 있던 행을 지우므로,
      insert 횟수를 그대로 세면 표의 실제 행 수보다 커진다.
      `test_synthetic` 이 이걸 잡았다 (2042 를 돌려주는데 행은 2033).
    """
    if spans is None:
        spans = privacy.load_active_spans(conn)
    if spans:
        events = privacy.clip_events(list(events), spans)

    now = now_ts()
    count = 0
    absorbed = 0
    for ev in events:
        data = ev.data or {}
        data_json = json.dumps(data, ensure_ascii=False)
        # ★ 흡수한 행 수를 빼야 반환값이 정직하다. 두 흡수기는 **이미 저장돼 있던 행을
        #   지우고** 들어오는 쪽을 넓힌다 — 그만큼 표의 행은 줄어든다.
        #   빼지 않으면 "저장한 수" 가 실제 행 수보다 커진다(test_synthetic 이 잡았다).
        ev, absorbed_a = _absorb_clipped_view(conn, ev)              # ① 우리가 만든 조각
        ev, absorbed_b = _absorb_duplicate_publication(conn, ev, data)  # ② 상류의 중복 발행
        absorbed += absorbed_a + absorbed_b
        conn.execute(
            """
            INSERT INTO aw_event(
                bucket_id, ts, ts_end, duration, event_id,
                app, title, url, status, data_json, synced_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bucket_id, ts) DO UPDATE SET
                -- ★ 긴 쪽을 남긴다 (2026-08-23).
                --
                -- AW 는 하트비트로 **자라는 중인 이벤트**를 성장 단계별로 여러 벌
                -- 돌려준다. 08-23 하루치 afk 버킷에서 465건이 왔는데 고유
                -- timestamp 는 66개였다 — 39개 그룹이 같은 `ts` 의 다른 길이였다:
                --
                --     20:19:31  [16.6s, 3716.6s]      ← 62분짜리 작업 구간
                --     21:21:27  [202s, 207s, 226s]
                --
                -- 예전에는 `excluded` 를 그대로 썼다 = **마지막에 온 것이 이긴다**.
                -- 순서가 보장되지 않아 사실상 무작위였고, 실측에서는 짧은 쪽이
                -- 자주 이겼다. 그 결과 노트북 활동 9.4시간(08-17~23)이 사라졌고,
                -- 롤업이 그 구간을 "afk 워처가 침묵한 공백"으로 읽어 폰에 넘겼다.
                --
                -- 하트비트 이벤트의 duration 은 **자라기만 하고 줄지 않는다.**
                -- 그래서 MAX 가 의미상 정확하다.
                ts_end=MAX(ts_end, excluded.ts_end),
                duration=MAX(duration, excluded.duration),
                event_id=excluded.event_id,
                app=excluded.app,
                title=excluded.title,
                url=excluded.url,
                status=excluded.status,
                data_json=excluded.data_json,
                synced_at=excluded.synced_at
            """,
            (
                ev.bucket_id,
                ev.ts,
                ev.ts_end,
                ev.duration,
                ev.event_id,
                data.get("app"),
                data.get("title"),
                data.get("url"),
                data.get("status"),
                data_json,
                now,
            ),
        )
        count += 1
    return count - absorbed


def sync(conn: sqlite3.Connection, client: AWClient, cfg: Config, *, now: float | None = None) -> SyncResult:
    """ActivityWatch 서버에서 모든(또는 `cfg.aw.hosts` 로 필터된) 버킷을 동기화한다."""
    now_val = now if now is not None else now_ts()
    backfill_floor = now_val - cfg.aw.backfill_days * 86400.0
    host_filter = set(cfg.aw.hosts) if cfg.aw.hosts else None

    # 버킷 목록 자체를 못 가져오면 동기화 전체가 무의미하므로 여기서는 예외를 그대로 올린다
    # (계약서 §0: 연결 실패처럼 전체가 무의미해지는 경우는 예외를 올린다).
    all_buckets = client.buckets()

    errors: list[str] = []
    events_upserted = 0
    buckets_seen = 0
    window_starts: list[float] = []

    for bucket_id, meta in all_buckets.items():
        if host_filter is not None and _extract_host(bucket_id, meta) not in host_filter:
            continue

        buckets_seen += 1
        cursor_key = f"aw_cursor:{bucket_id}"
        cursor = get_state_float(conn, cursor_key)
        window_start = backfill_floor if cursor is None else max(cursor - cfg.aw.overlap_sec, backfill_floor)
        window_starts.append(window_start)

        try:
            events = client.events(bucket_id, start=window_start, end=now_val, limit=-1)
            with transaction(conn):
                upsert_bucket(conn, bucket_id, meta)
                n = upsert_events(conn, events)
                if events:
                    set_state_float(conn, cursor_key, max(ev.ts for ev in events))
            events_upserted += n
        except (AWError, sqlite3.Error) as exc:
            logger.warning("버킷 동기화 실패 (%s): %s", bucket_id, exc)
            errors.append(f"{bucket_id}: {exc}")
            continue

    window_start_overall = min(window_starts) if window_starts else backfill_floor
    return SyncResult(
        buckets_seen=buckets_seen,
        events_upserted=events_upserted,
        window_start=window_start_overall,
        window_end=now_val,
        errors=errors,
    )
