"""10분 슬롯 롤업 (계약서 §4).

`aw_event`(window/afk/web) 와 `manual_entry` 를 하루 144개의 10분 슬롯으로 접는다.
슬롯 하나를 계산하는 절차는 계약서에 명시된 8단계를 그대로 따른다 — 특히
3단계(AFK 클리핑)를 빼먹으면 "잠든 사이에 8시간 코딩했다" 는 리포트가 나온다.

성능: 하루치 이벤트를 SQL 두 번(aw_event, manual_entry)으로 전부 읽어와
파이썬에서 슬롯마다 배분한다. 슬롯마다 쿼리를 날리지 않는다.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from lifetrainer import db, privacy, timeutil
from lifetrainer.plan.override import apply_overrides
from lifetrainer.rollup.classify import Classifier, normalize_fingerprint

logger = logging.getLogger(__name__)


@dataclass
class RollupResult:
    day: str
    slots_written: int
    breakdown_rows: int
    unclassified_seen: int
    active_sec: float
    # ★ 프라이빗은 **분모에서 뺀다** — 수집 실패가 아니라 재지 않기로 한 시간이다.
    #   분자에서만 빼면 "하루 종일 프라이빗 = 커버리지 0% = 시스템 고장"으로 읽힌다.
    #   (측정대상 − off) / 측정대상,  측정대상 = 전체슬롯 − 프라이빗슬롯
    coverage: float
    private_slots: int = 0


@dataclass
class _SlotWork:
    """슬롯 하나를 계산하는 동안 쓰는 임시 누적 상태."""

    active_sec: float = 0.0
    afk_sec: float = 0.0
    gap_sec: float = 0.0
    # (category, app, device_id) -> 초. slot_breakdown 에 그대로 들어가는 "정직한" 집계.
    # ★ 기기가 키에 있어야 하는 이유: 폰과 노트북이 같은 슬롯에서 같은 앱 이름을 만들 수
    #   있고(양쪽 'Chrome'), 기기를 빼면 두 기여가 한 행으로 합쳐져 "어느 기기였나"가
    #   사라진다. 사람이 넣은 수동 입력은 기기가 없으므로 None 이다.
    breakdown: dict[tuple[str, str, object], float] = field(default_factory=dict)
    # 승자 결정에만 쓰는 점수. breakdown 합계 + 수동입력 가중치(2배)가 반영된다.
    win_score: dict[str, float] = field(default_factory=dict)
    # 카테고리별 대표 subcategory (마지막으로 관측된 값).
    category_subcat: dict[str, str | None] = field(default_factory=dict)
    # (app, title, seconds) — 승자 카테고리의 top_app/top_title 선정용.
    contribs: list[tuple[str, str | None, str | None, float]] = field(default_factory=list)
    rule_categories: set[str] = field(default_factory=set)
    manual_categories: set[str] = field(default_factory=set)


# 폰으로 취급할 `device.kind`. 이 기기들은 afk 워처가 없을 수 있고,
# 짧은 사용이 노트북 활동에 흡수되는 쪽(§absorb)에 선다.
_PHONE_KINDS = frozenset({"phone", "tablet"})

_BUCKET_KINDS = ("window", "afk", "web", "android", "unlock", "media")


@dataclass
class _DeviceDay:
    """기기 하나의 하루치 이벤트. 중재는 이 단위로 한다."""

    key: object  # device_id, 없으면 "host:<호스트명>"
    kind: str
    buckets: dict[str, list[dict]] = field(default_factory=lambda: {k: [] for k in _BUCKET_KINDS})

    @property
    def is_phone(self) -> bool:
        return self.kind in _PHONE_KINDS


def _fetch_day_events(conn: sqlite3.Connection, start: float, end: float) -> list[_DeviceDay]:
    """[start, end) 와 겹치는 aw_event 를 **기기별·버킷 타입별**로 나눠 반환한다.

    한 번의 SQL 로 하루치 전체를 읽어온다 — 슬롯마다 쿼리를 날리지 않는다.

    ★ 예전에는 기기 구분 없이 타입별 리스트 3개로 합쳤다. 노트북 하나뿐일 때는
    맞았지만 폰이 들어오면 **폰의 창 이벤트가 노트북의 afk 구간으로 클리핑되고**
    (폰을 쓰는 동안 노트북은 afk 다) 두 기기 활동이 한 슬롯에서 합산돼 하루가
    24시간을 넘을 수 있다. 그래서 기기별로 받아 `_arbitrate_devices` 로 시간을
    가른 뒤에 합친다.

    `device_id` 가 NULL 인 버킷(마이그레이션 이전에 만들어진 행)은 호스트명으로
    묶는다 — 기기 차원이 없다고 하루 집계가 통째로 무너지면 안 된다.
    """
    rows = conn.execute(
        """
        SELECT e.bucket_id, e.ts, e.ts_end, e.app, e.title, e.url, e.status,
               b.type AS btype, b.device_id, b.host, d.kind AS device_kind
        FROM aw_event e
        JOIN aw_bucket b ON b.bucket_id = e.bucket_id
        LEFT JOIN device d ON d.id = b.device_id
        WHERE e.ts_end > ? AND e.ts < ?
        ORDER BY e.ts ASC
        """,
        (start, end),
    ).fetchall()

    # 버킷별로 타임라인을 정규화한다 (겹침 제거). 아래 _dedupe_overlaps 주석 참조.
    by_bucket: dict[str, list[dict]] = {}
    for row in rows:
        by_bucket.setdefault(row["bucket_id"], []).append(dict(row))

    devices: dict[object, _DeviceDay] = {}
    for events in by_bucket.values():
        head = events[0]
        key = head["device_id"] if head["device_id"] is not None else f"host:{head['host']}"
        dev = devices.get(key)
        if dev is None:
            dev = devices[key] = _DeviceDay(key=key, kind=head["device_kind"] or "other")
        for ev in _dedupe_overlaps(events):
            btype = _bucket_kind(ev)
            if btype in dev.buckets:
                dev.buckets[btype].append(ev)

    for dev in devices.values():
        for lst in dev.buckets.values():
            lst.sort(key=lambda e: e["ts"])
    # 기기 순서를 고정한다 — 동점 처리가 실행마다 달라지면 안 된다.
    return sorted(devices.values(), key=lambda d: (d.is_phone, str(d.key)))


# ── 구간 대수 (중재용) ────────────────────────────────────────────────────


# ★ 정의는 `timeutil` 로 옮겼다 — `collect/` 에서도 써야 하는데 `collect → rollup` 은
#   층이 뒤집히기 때문이다. 이름은 여기 그대로 남긴다(호출부·테스트가 이걸 쓴다).
# 프라이빗 슬롯의 카테고리 이름. 구조 상태(off/away/unknown)의 네 번째다.
_PRIVATE_CATEGORY = "private"

# ★ **롤업이 직접 붙이는 카테고리들.** 규칙(rules.yaml)이 아니라 코드가 정한다.
#
#   이 목록이 `config/rules.yaml` 의 선언과 어긋나면 `classifier.label()` 이
#   `KeyError` 로 죽는다 — PNG 리포트 범례가 그 날 나온 카테고리를 전부 라벨로 바꾼다.
#   실제로 `private` 이 2026-09-01 부터 빠져 있었고, **프라이빗이 있는 날의 일일 PNG 를
#   그렸으면 죽었을 것이다.** 09-05 에 수면을 넣다가 같은 자리에서 드러났다.
#
#   FK 대신 이 목록과 rules.yaml 을 `tests/test_categories.py` 가 맞춰 본다 —
#   카테고리의 정본은 DB 표가 아니라 rules.yaml 이다 (표를 만들면 정본이 둘이 된다).
CODE_ASSIGNED_CATEGORIES = ("private", "sleep")

_union = timeutil.union_spans
_subtract = timeutil.subtract_spans


def _clip_events_excluding(events: list[dict], cut: list[tuple[float, float]]) -> list[dict]:
    """이벤트에서 `cut`(다른 기기가 소유한 시간)과 겹치는 조각을 잘라낸다.

    **자기 소유 구간으로 잘라내는 게 아니라 남의 소유만 뺀다.** 창 이벤트가
    자기 기기의 afk 시간까지 이어지는 것은 정상이고, `_process_slot` 이 그 부분을
    `away` 로 귀속시킨다 — 여기서 미리 잘라버리면 away 가 어느 앱 때문이었는지
    사라진다. 반면 남이 소유한 시간에 남은 조각은 **그 기기가 이미 계산했으므로**
    빼야 한다. 그러지 않으면 같은 시간이 두 번 잡힌다.

    ★ 길이 0 이벤트는 **점**이라 잘라낼 것이 없다 — `privacy.clip_events` 와 같은
    규칙으로 구간 안이면 버리고 밖이면 남긴다. 초를 하나도 안 물고 오므로 이중 계산은
    생기지 않고, 대신 그 순간의 앱·제목이 살아남는다.
    """
    if not cut:
        return list(events)
    cut = _union(cut)
    out: list[dict] = []
    for ev in events:
        ts, ts_end = ev["ts"], ev["ts_end"]
        if ts_end <= ts:
            if not any(s <= ts < e for s, e in cut):
                out.append({**ev})
            continue
        for s, e in _subtract([(ts, ts_end)], cut):
            out.append({**ev, "ts": s, "ts_end": e})
    out.sort(key=lambda e: e["ts"])
    return out


def _device_of(ev) -> int | None:
    """이벤트가 어느 기기에서 왔는지. 없으면 None.

    `_fetch_day_events` 가 dict 로 실어 오지만, 합성된 afk 구간이나 수동 입력 Row 처럼
    이 키가 아예 없는 것도 같은 자리를 지나간다. 없으면 조용히 None 이다 —
    기기 차원이 없다고 하루 집계가 무너지면 안 된다.
    """
    try:
        return ev["device_id"]
    except (KeyError, IndexError):
        return None


def _title_from_media(sessions: list[dict], media: list[dict]) -> list[dict]:
    """폰 앱 세션의 제목을 **재생 중이던 것의 제목**으로 바꾼다.

    ## 왜 필요한가

    안드로이드 앱 세션(UsageStats)이 주는 `title` 은 **앱 이름**이다 —
    유튜브를 두 시간 봐도 전부 `"YouTube"` 라 무엇을 봤는지 알 수 없다.
    노트북 쪽은 창 제목이 그대로 오는데 폰만 비어 있던 셈이다.

    `aw-watcher-android-media` 가 재생 중인 곡·영상의 제목을 따로 보고한다
    공개본에는 개인별 앱 이벤트 개수를 싣지 않는다.
    그걸 같은 앱의 세션에 얹는다.

    ## 규칙

    - **같은 패키지일 때만** 얹는다. 백그라운드 음악이 다른 앱 세션의 제목을
      바꾸면 "무엇을 했나"가 거짓이 된다
    - 한 세션에 여러 곡이 걸리면 **겹친 시간이 가장 긴 것**을 쓴다
    - 제목이 비었으면 그냥 둔다 — 앱 이름이라도 남는 편이 낫다
    - 원본을 바꾸지 않는다(새 dict 를 만든다). 같은 이벤트를 두 번 훑어도 안전하다
    """
    if not media:
        return sessions
    by_app: dict[str, list[dict]] = {}
    for ev in media:
        app = (ev.get("app") or "").lower()
        if app and (ev.get("title") or "").strip():
            by_app.setdefault(app, []).append(ev)
    if not by_app:
        return sessions

    out: list[dict] = []
    for ev in sessions:
        cands = by_app.get((ev.get("app") or "").lower())
        if not cands:
            out.append(ev)
            continue
        best, best_overlap = None, 0.0
        for m in cands:
            overlap = min(ev["ts_end"], m["ts_end"]) - max(ev["ts"], m["ts"])
            # 미디어 이벤트는 길이 0 인 상태 전환도 있다. 구간이 겹치지 않아도
            # **세션 안에서 일어난 전환**이면 그 곡이 재생된 것이다.
            if overlap > best_overlap or (
                overlap >= 0 and best is None and ev["ts"] <= m["ts"] <= ev["ts_end"]
            ):
                best, best_overlap = m, max(overlap, 0.0)
        out.append({**ev, "title": best["title"]} if best else ev)
    return out


def _android_to_window(events: list[dict]) -> list[dict]:
    """★ 안드로이드 → 공통 window 형태로 바꾸는 **유일한 지점**.

    폰 이벤트를 받을 때 `android` 라는 자기 이름으로 받아둔 이유가 여기 있다.
    변환이 여러 곳에 흩어지면 "같은 값을 여러 곳에서 각자 계산"(반복 실패 2번)이
    된다. 아래 한 줄 말고는 어디서도 android 를 window 로 바꾸지 않는다.
    """
    return [{**ev, "btype": "window"} for ev in events]


# 소유권 경쟁의 등급. 같은 시간을 두 기기가 주장할 때 **등급이 먼저**고,
# 등급이 같을 때만 "늦게 시작한 쪽이 이긴다"를 본다.
_LOCKED_TIER = 2  # 흡수(absorb_short_switches)가 이미 정한 구간 — 무조건 이긴다
TOUCHED = 1   # afk 워처가 not-afk 라고 **말한** 구간 = 사람이 만졌다는 증거
PRESENT = 0   # 보고 공백을 앱 세션으로 메운 구간 = 활동은 맞지만 상호작용 증거가 없다


def _activity_intervals(dev: _DeviceDay) -> list[tuple[float, float]]:
    """기기가 '활동 중'이던 구간 (등급 구분 없이 전부)."""
    return _union([iv for iv, _tier in _activity_claims(dev)])


def _activity_claims(dev: _DeviceDay) -> list[tuple[tuple[float, float], int]]:
    """기기가 '활동 중'이던 구간을 **소유권 등급과 함께** 돌려준다.

    afk 버킷이 있으면 그것이 정본이다 (노트북, 그리고 포크가 화면·터치로 afk 를
    합성해 주는 폰). **afk 버킷이 없는 폰**은 앱 세션 자체를 활동으로 본다 —
    안드로이드엔 AFK 워처가 없어서, 그러지 않으면 폰 활동이 전부 away 가 된다.

    ## 등급을 왜 나누나 (2026-08-25)

    "마지막 상호작용이 그 시간을 소유한다"는 규칙은 맞는데, **"만진 것"과
    "틀어둔 것"을 구분하지 못했다.** 실측:

        T0~T1              폰 영상 연속 재생
                           폰 afk 보고 없음   ← Doze 로 워처가 통째로 침묵
                           폰 앱 세션 전 구간 ← UsageStats 는 소급 기록된다
                           노트북 not-afk 100%  (코딩 중)

    폰은 `not-afk` 를 한 번도 말한 적이 없는데, **보고 공백을 앱 세션으로 메우는
    규칙**이 재생 구간을 통째로 활동으로 만들었고 그게 노트북보다 늦게 시작해서 이겼다.
    결과: 연속된 두 슬롯에서 영상이 코딩보다 앞서는 잘못된 판정.

    그래서 **활동으로 인정하는 것과 소유권을 주는 것을 분리한다.**
    보고 공백 세션은 여전히 활동이지만(그래야 게임 세션이 안 날아간다),
    상대 기기가 "만졌다"고 말하면 그쪽에 진다.

    ★ **폰이 항상 지는 것이 아니다.** 폰만 보는 시간에는 경쟁자가 없으므로
    `PRESENT` 등급이라도 그대로 이긴다. 이동 중·자기 전 폰 사용이 그 경우다.
    """
    sessions = _union(
        [(ev["ts"], ev["ts_end"]) for ev in (dev.buckets["android"] or dev.buckets["window"])]
    )
    afk = dev.buckets["afk"]
    if not afk:
        # afk 워처가 아예 없는 기기. 앱 세션 말고는 판단 재료가 없으므로
        # 그것을 상호작용으로 인정한다 — 안 그러면 이 기기는 영원히 진다.
        return [(iv, TOUCHED) for iv in sessions]

    not_afk = _union([(ev["ts"], ev["ts_end"]) for ev in afk if ev["status"] == "not-afk"])
    # ★ **보고가 없는 것은 '자리비움'의 증거가 아니다** (2026-08-22 실측).
    #
    # 폰의 afk 워처는 살아 있어야 보고한다 — Doze 에 들어가면 통째로 멈춘다.
    # 반면 앱 세션(UsageStats)은 **시스템이 소급해서 기록**하므로 Doze 구간도 나중에
    # 그대로 올라온다. 그래서 "afk 버킷이 아무 말도 하지 않은 구간"에서는 앱 세션이
    # 더 나은 증거다. 그 구간을 away로 떨어뜨렸더니 게임 세션이 통째로
    # 자리비움으로 찍혔다. 개인별 앱·시각·분량은 공개본에서 제거했다.
    #
    # 명시적 'afk'(화면 꺼짐이라고 **말한** 구간)는 그대로 존중한다 — 그건 부재의
    # 증거가 맞다. 명시적 afk 구간보다 보고 공백 구간에서 앱 전환이 더 잦아
    # 실제 손이 닿은 모양이었다.
    # ★ 노트북에는 보고 공백 규칙을 쓰지 않는다 (2026-08-23 정정).
    #
    # 근거가 폰 전용이었다: 폰의 afk 워처는 Doze 에 들어가면 멈추는데 앱 세션은
    # 시스템이 소급 기록하므로, 보고 공백에서는 앱 세션이 더 나은 증거다.
    # 노트북은 그렇지 않다 — 워처가 상주하고, 공백은 대개 **우리 쪽 정규화가
    # 만든 빈틈**이다(`_dedupe_overlaps` 가 하트비트 중복을 자르며 남긴 구멍).
    #
    # 실제 사고: 자는 동안 VS Code 를 켜둔 구간을 afk 가 정확히 'afk' 로
    # 보고했는데, 정규화가 중간에 구멍을 남겼고 이 규칙이 그 구멍을
    # 활동으로 읽어 **잠든 시간이 '코딩'으로 찍혔다.**
    if not dev.is_phone:
        return [(iv, TOUCHED) for iv in not_afk]

    reported = _union([(ev["ts"], ev["ts_end"]) for ev in afk])
    gap_sessions = _subtract(sessions, reported)
    return [(iv, TOUCHED) for iv in not_afk] + [(iv, PRESENT) for iv in gap_sessions]


def _bucket_kind(ev: dict) -> str:
    """이 이벤트를 어느 통에 담을지. `aw_bucket.type` 이 정본이되 예외가 하나 있다.

    ★ **폰의 미디어 워처가 `type='android'` 로 등록돼 있다.** 버킷 이름만
    `aw-watcher-android-media` 로 다르다. 타입만 보면 재생 상태 이벤트(대부분
    길이 0인 playing/paused 전환)가 **앱 세션과 같은 통에 섞인다** — 그래서
    유튜브 세션의 제목이 곡 제목으로 안 바뀌고 앱 이름(`"YouTube"`)에 머물렀다.

    버킷을 다시 만들게 하는 대신 여기서 한 줄로 가른다. 이름으로 가르는 것이
    타입으로 가르는 것보다 약하지만, 워처가 이미 그 이름으로 보내고 있고
    바꾸려면 폰 앱을 고쳐야 한다.
    """
    if str(ev.get("bucket_id") or "").endswith("-media"):
        return "media"
    return ev["btype"]


def _dedupe_overlaps(events: list[dict]) -> list[dict]:
    """한 버킷의 이벤트를 겹치지 않는 타임라인으로 정규화한다.

    **왜 필요한가 — 실데이터에서 겪은 문제다.**

    조사 문서(`docs/research/activitywatch.md §3`)는 aw-server-rust 기준으로
    "한 버킷 안에서 이벤트는 겹치지 않는다"고 정리했고, 롤업은 그 전제로 겹친 초를
    단순 합산했다. 그런데 실제로 붙여보니 파이썬 aw-server(v0.13.2)에서 전제가 깨졌다.

    서버가 잠깐 죽어 있는 동안 워처는 이벤트를 `persistqueue` 에 쌓아두는데,
    서버가 살아나면 **자라는 중이던 하나의 하트비트가 성장 단계마다 개별 이벤트로**
    한꺼번에 들어온다. 시작 시각이 (마이크로초 단위로) 거의 같고 duration 만 다른
    행이 23개씩 생겼고, 그걸 전부 더해 **2시간짜리 자리비움이 22시간으로 부풀었다.**

    해결: 뒤 이벤트가 앞 이벤트를 자른다. 버킷은 본질적으로 하나의 타임라인이고,
    같은 순간에 두 상태가 동시에 참일 수 없다. 나중 관측을 신뢰한다.
    완전히 덮인 이벤트는 길이 0 이 되어 사라진다.

    이 정규화는 상류가 어떻게 이상해지든 롤업을 방어한다 — 합산 전에 겹침이 없음을
    보장하므로, 활동 시간이 물리적으로 가능한 값을 넘길 수 없다.
    """
    if len(events) < 2:
        return list(events)

    ordered = sorted(events, key=lambda e: (e["ts"], e["ts_end"]))
    out: list[dict] = []
    for i, ev in enumerate(ordered):
        end = ev["ts_end"]
        # 뒤따르는 이벤트 중 가장 이른 시작으로 자른다.
        for nxt in ordered[i + 1 :]:
            if nxt["ts"] >= end:
                break
            end = nxt["ts"]
            break
        if end > ev["ts"]:
            clipped = dict(ev)
            clipped["ts_end"] = end
            out.append(clipped)
    return out


def _fetch_manual_entries(conn: sqlite3.Connection, start: float, end: float) -> list[sqlite3.Row]:
    # ts/ts_end 로 별칭을 줘서 _advance_and_collect 가 aw_event 와 같은 컬럼명으로 다룰 수 있게 한다.
    return conn.execute(
        """
        SELECT start_ts AS ts, end_ts AS ts_end, category, subcategory
        FROM manual_entry
        WHERE revoked = 0 AND end_ts > ? AND start_ts < ?
        ORDER BY start_ts ASC
        """,
        (start, end),
    ).fetchall()


def _advance_and_collect(
    events: list[sqlite3.Row], idx: int, s: float, e: float
) -> tuple[list[sqlite3.Row], int]:
    """ts 오름차순 events 에서 [s, e) 와 겹치는 항목을 모으고, 다음 슬롯을 위한 시작 인덱스를 반환한다.

    슬롯은 시간순으로 순회되므로, ts_end <= s 로 완전히 지나간 이벤트는 idx 를 넘겨
    다시 보지 않는다 (two-pointer sweep) — O(events + slots).
    """
    n = len(events)
    while idx < n and events[idx]["ts_end"] <= s:
        idx += 1
    hits: list[sqlite3.Row] = []
    j = idx
    while j < n and events[j]["ts"] < e:
        if timeutil.overlap_sec(events[j]["ts"], events[j]["ts_end"], s, e) > 0:
            hits.append(events[j])
        j += 1
    return hits, idx


def _best_web(
    web_hits: list[sqlite3.Row], w_start: float, w_end: float, field: str
) -> str | None:
    """window 이벤트의 [w_start, w_end] 구간과 겹침이 가장 큰 web 이벤트의 `field`.

    값이 비어 있는 이벤트는 후보가 아니다 — 그래서 `url` 과 `title` 의 답이 다를 수
    있다. 짧은 전환 이벤트는 url 만 있고 제목이 아직 안 붙은 경우가 흔하다.
    """
    best: str | None = None
    best_overlap = 0.0
    for w in web_hits:
        ov = timeutil.overlap_sec(w["ts"], w["ts_end"], w_start, w_end)
        if ov > best_overlap and w[field]:
            best_overlap = ov
            best = w[field]
    return best


def _arbitrate_devices(
    devices: list[_DeviceDay], locked: list[tuple[float, float]], locked_key: object | None
) -> tuple[list[dict], list[dict], list[dict]]:
    """기기끼리 겹치는 시간을 갈라 하나의 타임라인으로 합친다.

    ★ 규칙: **마지막 상호작용이 그 시간을 소유한다.** 단 "상호작용"이 먼저다 —
    만졌다는 증거(`TOUCHED`)가 켜져 있었다는 사실(`PRESENT`)을 항상 이긴다.
    등급이 같을 때만 늦게 시작한 쪽이 이긴다 (`_activity_claims` 참고).

        폰을 켜둔 채 노트북에서 작업  → 노트북 (노트북 활동이 더 늦게 시작했다)
        노트북 작업 중 폰을 만짐      → 폰   (폰 활동이 더 늦게 시작했다)
        폰에서 손을 떼면              → 노트북이 다시 가져간다 (폰 구간이 끝났으므로)

    구간 단위로 보면 "그 순간을 덮고 있는 활동 구간 중 **가장 늦게 시작한 것**이
    이긴다" 와 같다. 진짜 기기 전환은 언제나 나중에 시작하는 구간을 만들기 때문이다.
    시작 시각이 완전히 같으면 전환이 일어난 게 아니므로 **폰이 아닌 쪽**을 택한다
    (`_DeviceDay` 정렬이 이 순서를 고정한다).

    `locked` 는 `absorb_short_switches` 가 앞 활동으로 흡수하기로 한 공백이다.
    그 구간은 `locked_key` 기기가 무조건 가져간다 — 흡수와 중재가 같은 구간을 놓고
    싸우면 "같은 값을 두 곳에서 각자 계산"(반복 실패 2번)이 된다. 흡수가 먼저 정하고,
    중재는 남은 곳만 정한다.

    반환: 합쳐진 (window, afk, web) 이벤트. 소유 구간이 기기 간에 서로 겹치지 않으므로
    **슬롯 활동 합이 슬롯 길이를 넘을 수 없다.**
    """
    # (start, end, 등급, 우선순위, 기기 index)
    claims: list[tuple[float, float, int, float, int]] = []
    for idx, dev in enumerate(devices):
        for (iv_s, iv_e), tier in _activity_claims(dev):
            claims.append((iv_s, iv_e, tier, iv_s, idx))
    # 흡수된 공백은 그 기기가 무조건 이기도록 등급·우선순위를 무한대로 준다.
    if locked and locked_key is not None:
        for idx, dev in enumerate(devices):
            if dev.key == locked_key:
                for ls, le in _union(locked):
                    claims.append((ls, le, _LOCKED_TIER, float("inf"), idx))
                break

    owned: list[list[tuple[float, float]]] = [[] for _ in devices]
    if claims:
        edges = sorted({p for s_, e_, _, _, _ in claims for p in (s_, e_)})
        for seg_s, seg_e in zip(edges, edges[1:], strict=False):  # pairwise — 길이가 1 다른 것이 정상
            if seg_e <= seg_s:
                continue
            best: tuple[int, float, int] | None = None
            for c_s, c_e, tier, prio, idx in claims:
                if c_s <= seg_s and c_e >= seg_e:
                    # ★ **등급이 먼저다.** "만졌다"(TOUCHED)가 "켜져 있었다"(PRESENT)를
                    #   항상 이긴다 — 틀어둔 유튜브가 코딩을 밀어내던 자리다.
                    #   등급이 같을 때만 우선순위(=시작 시각)가 늦은 쪽이 이기고,
                    #   그것도 같으면 앞선 기기(폰이 아닌 쪽)가 이긴다 —
                    #   devices 정렬이 그 순서를 만든다.
                    if best is None or (tier, prio) > (best[0], best[1]):
                        best = (tier, prio, idx)
            if best is not None:
                owned[best[2]].append((seg_s, seg_e))

    window_out: list[dict] = []
    afk_out: list[dict] = []
    web_out: list[dict] = []
    all_owned: list[tuple[float, float]] = []

    for idx, dev in enumerate(devices):
        mine = _union(owned[idx])
        all_owned.extend(mine)
        others = _union([iv for j, spans in enumerate(owned) if j != idx for iv in spans])
        # 폰의 앱 세션은 여기서 **한 번만** window 로 바뀐다.
        source = dev.buckets["window"] + _android_to_window(
            _title_from_media(dev.buckets["android"], dev.buckets["media"])
        )
        window_out.extend(_clip_events_excluding(source, others))
        web_out.extend(_clip_events_excluding(dev.buckets["web"], others))

    # afk 타임라인은 전역으로 하나만 만든다.
    #   not-afk = 어느 기기든 소유한 구간   (기기 간 겹침이 없으므로 합 ≤ 하루)
    #   afk     = 누군가 afk 라고 보고했는데 아무도 소유하지 않은 구간
    all_owned = _union(all_owned)
    reported_afk = _union(
        [
            (ev["ts"], ev["ts_end"])
            for dev in devices
            for ev in dev.buckets["afk"]
            if ev["status"] == "afk"
        ]
    )
    for s_, e_ in all_owned:
        afk_out.append({"ts": s_, "ts_end": e_, "status": "not-afk", "app": None, "title": None, "url": None})
    for s_, e_ in _subtract(reported_afk, all_owned):
        afk_out.append({"ts": s_, "ts_end": e_, "status": "afk", "app": None, "title": None, "url": None})

    window_out.sort(key=lambda e: e["ts"])
    afk_out.sort(key=lambda e: e["ts"])
    web_out.sort(key=lambda e: e["ts"])
    return window_out, afk_out, web_out


def _process_slot(
    work: _SlotWork,
    classifier: Classifier,
    cfg_rollup,
    s: float,
    e: float,
    afk_hits: list[sqlite3.Row],
    window_hits: list[sqlite3.Row],
    web_hits: list[sqlite3.Row],
    manual_hits: list[sqlite3.Row],
    unclassified_acc: dict[str, list],
) -> None:
    """슬롯 하나(계약서 §4 절차 1~8단계)를 계산해 work 에 채운다."""
    afk_category = cfg_rollup.afk_category

    # 1. afk 버킷으로 active_sec/afk_sec/gap_sec 계산. 동시에 not-afk 구간을 모아
    #    2~3단계(윈도우 클리핑)에서 재사용한다.
    not_afk_intervals: list[tuple[float, float]] = []
    for ev in afk_hits:
        ov_start = max(ev["ts"], s)
        ov_end = min(ev["ts_end"], e)
        if ov_end <= ov_start:
            continue
        status = ev["status"]
        if status == "not-afk":
            work.active_sec += ov_end - ov_start
            not_afk_intervals.append((ov_start, ov_end))
        elif status == "afk":
            work.afk_sec += ov_end - ov_start
    work.gap_sec = max(0.0, (e - s) - work.active_sec - work.afk_sec)

    # 2~3. window 이벤트를 순회하며 분류하고, not-afk 구간으로 클리핑한다.
    #    클리핑되어 떨어져 나간 시간은 away 로 보낸다.
    for win in window_hits:
        w_start = max(win["ts"], s)
        w_end = min(win["ts_end"], e)
        if w_end <= w_start:
            continue
        win_span = w_end - w_start

        active_portion = 0.0
        for na_s, na_e in not_afk_intervals:
            active_portion += timeutil.overlap_sec(w_start, w_end, na_s, na_e)
        active_portion = min(active_portion, win_span)
        away_portion = max(0.0, win_span - active_portion)

        app = win["app"]
        title = win["title"]

        # ── 브라우저 세션의 **표시용** 제목 ───────────────────────────
        #
        # 폰의 앱 세션이 주는 title 은 **앱 이름**이다 — 크롬으로 뭘 봐도 전부
        # `"Chrome"` 이라 그 칸이 아무것도 말하지 않는다. 웹 버킷에는 페이지 제목이
        # 들어와 있는데(폰 앱이 2026-09-03 부터 채운다) 여기까지 오는 길이 없었다:
        # `dev.buckets["web"]` 은 url 부착에만 쓰이고 `contribs` 에는 window 이벤트만
        # 들어가는데, `top_title` 은 그 `contribs` 에서 뽑힌다.
        #
        # ★ **분류(`classify`)에는 넣지 않는다.** `title` 을 바꾸면 제목 규칙이 다르게
        #   걸려 카테고리가 움직이고, 그러면 `slot_breakdown`(집계 원천)이 바뀐다.
        #   이 변경은 시각화 층만 건드리는 것으로 둔다 — 제목을 분류에 먹이는 것은
        #   따로 재고 정할 일이다.
        display_title = title
        if classifier.is_browser(app):
            display_title = _best_web(web_hits, w_start, w_end, "title") or title

        if active_portion > 0:
            url = None
            if classifier.is_browser(app):
                url = _best_web(web_hits, w_start, w_end, "url")
            cls = classifier.classify(app=app, title=title, url=url)

            key = (cls.category, app or "", _device_of(win))
            work.breakdown[key] = work.breakdown.get(key, 0.0) + active_portion
            work.win_score[cls.category] = work.win_score.get(cls.category, 0.0) + active_portion
            if cls.subcategory:
                work.category_subcat[cls.category] = cls.subcategory
            work.contribs.append((cls.category, app, display_title, active_portion))
            work.rule_categories.add(cls.category)

            if cls.source == "default":
                fp = normalize_fingerprint(app, title)
                acc = unclassified_acc.setdefault(fp, [app, title, 0.0, 0, win["ts"], win["ts_end"]])
                acc[2] += active_portion  # seconds_total
                acc[3] += 1  # hits
                acc[4] = min(acc[4], win["ts"])  # first_seen
                acc[5] = max(acc[5], win["ts_end"])  # last_seen

        if away_portion > 0:
            key = (afk_category, app or "", _device_of(win))
            work.breakdown[key] = work.breakdown.get(key, 0.0) + away_portion
            work.win_score[afk_category] = work.win_score.get(afk_category, 0.0) + away_portion
            work.contribs.append((afk_category, app, display_title, away_portion))
            work.rule_categories.add(afk_category)

    # 5. 수동 입력이 겹치면 그 카테고리에 겹친 초를 더하고(정직한 breakdown 은 1배),
    #    승자 결정 점수(win_score)에는 2배 가중치를 준다.
    for m in manual_hits:
        ov = timeutil.overlap_sec(m["ts"], m["ts_end"], s, e)
        if ov <= 0:
            continue
        category = m["category"]
        key = (category, "", None)  # 수동 입력은 기기가 없다
        work.breakdown[key] = work.breakdown.get(key, 0.0) + ov
        # win_score = breakdown 1배 + 추가 1배 = 총 2배 가중치.
        work.win_score[category] = work.win_score.get(category, 0.0) + ov + ov
        if m["subcategory"]:
            work.category_subcat[category] = m["subcategory"]
        work.contribs.append((category, None, None, ov))
        work.manual_categories.add(category)


def absorb_short_switches(
    window_events: list[dict],
    afk_events: list[dict],
    threshold_sec: float,
    *,
    absorbed_out: list[tuple[float, float]] | None = None,
) -> tuple[int, float]:
    """활동 사이에 낀 **짧은 기기 전환**을 앞 활동으로 흡수한다 (제자리 수정).

    폰을 잠깐 만지면 컴퓨터 입력이 끊기므로 afk 워처가 `away` 로 잡고, 창 이벤트도
    끊긴다. 그대로 두면 "코딩 10분 / 자리비움 3분 / 코딩 10분" 세 토막이 되는데,
    사람이 보기에 그건 그냥 **코딩 23분**이다. 잠깐 딴짓한 것까지 전부 찍히면
    플래너가 난잡해서 읽을 수가 없다.

    그래서 `threshold_sec`(기본 5분) 미만이고 **양옆이 모두 활동인** 공백만 흡수한다.
    - 앞 창 이벤트의 끝을 다음 창 이벤트 시작까지 늘린다 (카테고리가 앞쪽으로 이어짐)
    - 같은 구간의 afk 이벤트를 `not-afk` 로 뒤집는다 (안 그러면 클리핑에서 잘려나감)

    5분 이상이면 손대지 않는다 — 그건 실제로 자리를 비웠거나 다른 기기로 옮겨 간
    것이고, 플래너에 남아야 하는 정보다.

    양옆이 모두 활동일 때만 흡수하는 이유: 하루 시작·끝의 공백까지 앞으로 늘리면
    "잠들기 전 코딩" 이 아침까지 이어진 것으로 기록된다.

    `absorbed_out` 을 주면 흡수한 구간을 거기 담는다. 기기 중재(`_arbitrate_devices`)가
    **그 구간을 이 기기 소유로 고정**하는 데 쓴다 — 흡수가 "이 3분은 앞 활동"이라고
    정한 구간을 중재가 다시 폰에게 주면 둘이 싸운다.

    반환: (흡수한 공백 수, 흡수한 총 초).
    """
    if threshold_sec <= 0 or len(window_events) < 2:
        return 0, 0.0

    absorbed_spans: list[tuple[float, float]] = []
    for prev, nxt in zip(window_events, window_events[1:], strict=False):  # pairwise
        gap_start, gap_end = prev["ts_end"], nxt["ts"]
        gap = gap_end - gap_start
        if 0 < gap < threshold_sec:
            prev["ts_end"] = gap_end  # 앞 활동을 늘려 공백을 덮는다
            absorbed_spans.append((gap_start, gap_end))

    if not absorbed_spans:
        return 0, 0.0

    if absorbed_out is not None:
        absorbed_out.extend(absorbed_spans)

    # 흡수한 구간을 활동으로 인정해야 창 이벤트가 클리핑에서 살아남는다.
    #
    # ★ 여기서 두 번 틀렸다.
    # (1) "afk 이벤트가 공백 안에 완전히 들어올 때만 뒤집기" → 실데이터는 경계가
    #     정확히 맞지 않아 거의 안 뒤집혔고, 창만 늘어나
    #     그 구간이 away 로 분류돼 **away 가 늘었다.**
    # (2) 그래서 겹친 부분만 잘라내게 고쳤는데도 여전히 늘었다. 사본을 재 보니
    #     공백 대부분에는 afk 이벤트가 아예 없었다.
    #     나머지는 afk 이벤트가 **아예 없는** 구간(워처 미보고)이라, 잘라낼 것이
    #     없으니 active_portion 이 0 이 되어 통째로 away 가 됐다.
    #
    # 결론: 잘라내는 것으로는 부족하고 **덮어야 한다.** 흡수 구간과 겹치는 afk
    # 이벤트 조각을 전부 제거한 뒤, 구간마다 not-afk 를 하나씩 넣는다.
    # 겹침 없이 정확히 한 번만 덮이므로 active_sec 이 이중 계산되지 않는다.
    template = afk_events[0] if afk_events else {}
    rebuilt: list[dict] = []
    for ev in afk_events:
        pieces = [(ev["ts"], ev["ts_end"])]
        for gs, ge in absorbed_spans:
            nxt: list[tuple[float, float]] = []
            for ps, pe in pieces:
                if pe <= gs or ps >= ge:
                    nxt.append((ps, pe))
                    continue
                if ps < gs:
                    nxt.append((ps, gs))
                if pe > ge:
                    nxt.append((ge, pe))
            pieces = nxt
        for ps, pe in pieces:
            if pe > ps:
                rebuilt.append({**ev, "ts": ps, "ts_end": pe})

    for gs, ge in absorbed_spans:
        rebuilt.append({**template, "ts": gs, "ts_end": ge, "status": "not-afk"})

    rebuilt.sort(key=lambda x: x["ts"])
    afk_events[:] = rebuilt

    total = sum(ge - gs for gs, ge in absorbed_spans)
    logger.debug("짧은 전환 %d건(%.0f초)을 앞 활동으로 흡수했습니다", len(absorbed_spans), total)
    return len(absorbed_spans), total


SLEEP_CATEGORY = "sleep"
# 이만큼 이어지면 잠으로 본다. **보수적인 값이다** — 낮잠·중간에 깬 밤은 안 잡힌다.
# 그건 사람이 표에서 직접 고친다(보정이 추정을 이긴다).
SLEEP_MIN_HOURS = 3.0
# ★ 상한도 필요하다. 상한이 없으면 **빈 하루가 "24시간 잤다"** 가 된다 —
#   기기를 안 켠 날·수집이 통째로 빠진 날이 그렇고, 그건 "잤다" 가 아니라 "모른다" 다.
#   (상한 없이 만들었더니 기존 테스트 다섯이 바로 잡았다.)
#
#   23시간은 **사람이 정한 값이다.** 하한 3시간이 보수적인 것과 같은 뜻으로,
#   상한은 "하루가 통째로 빈 경우만 걸러라" 는 쪽으로 느슨하게 뒀다 — 못 거른 것은
#   표에서 고치면 되고, 지나치게 잘라내면 고칠 것조차 안 보인다.
SLEEP_MAX_HOURS = 23.0


def infer_sleep(conn, cfg, day: str, *, now: float) -> int:
    """`day` 를 둘러싼 조용한 구간을 보고 **수면**으로 적는다. 반환값은 바꾼 칸 수.

    ## 왜 away 만으로는 안 되나 — 재 보고 알았다

    밤은 `away` 가 아니라 **`off`(결측)** 다. 08-30~09-04 실측:

        밤(23~08) 분포     off 305칸 · away 9칸
        하루 최장 연속     away 0.2~0.5h · away+off 2.7~5.7h

    기기를 안 쓰면 워처가 아무것도 안 보내므로 "자리를 비웠다"가 아니라 "관측이 없다"로
    떨어진다. 그래서 둘을 같이 본다.

    ## ★ 논리적 하루 경계를 넘어서 본다 (2026-09-05 오후)

    하루씩 따로 보면 **06:00 에서 잠이 잘린다.** 23시~07시를 자면 앞날은 7시간(잡힘),
    다음날은 1시간(3시간 문턱 미달 → 안 잡힘)이 되어 **아침 몫이 통째로 빠진다.**
    04시~07시처럼 경계에 걸친 짧은 잠은 양쪽 다 미달이라 아예 사라진다.

    그래서 `day-1 · day · day+1` 을 이어 붙인 하나의 줄에서 구간을 재고, **구간이
    걸친 날 전부에** 적는다. 이웃 날을 같이 고치는 것이 요점이다 — 안 그러면
    "앞날만 잠이고 다음날 아침은 아닌" 비대칭이 남는다.

    ## 무엇을 안 하나

    - **사람이 고친 칸은 안 건드린다** (`slot_override`). 보정이 추정을 이긴다 —
      추정이 사람의 결정을 덮으면 그때부터 아무도 안 고친다
    - **낮잠·중간에 깬 밤을 맞히려 하지 않는다.** 3시간은 보수적인 값이고 못 잡는
      쪽은 사람이 표에서 고친다
    - **너무 긴 것도 잠이라 하지 않는다** (`SLEEP_MAX_HOURS`) — 하루가 통째로 조용한
      것은 "잤다" 가 아니라 "모른다" 다
    - **아직 안 온 시간은 안 센다.** 오늘 격자의 뒤쪽은 전부 `off` 라, 그냥 세면
      아침에 미래 시간까지 잤다고 뜬다 (합성 회귀 데이터로 확인)
    """
    slot_sec = cfg.rollup.slot_minutes * 60.0
    need = max(1, int(round(SLEEP_MIN_HOURS * 3600.0 / slot_sec)))
    cap = int(round(SLEEP_MAX_HOURS * 3600.0 / slot_sec))
    away, off = cfg.rollup.afk_category, cfg.rollup.no_data_category

    d = date.fromisoformat(day)
    days = [(d - timedelta(days=1)).isoformat(), day, (d + timedelta(days=1)).isoformat()]

    rows = conn.execute(
        "SELECT s.day, s.slot, s.category, s.start_ts, o.category AS override "
        "FROM slot s LEFT JOIN slot_override o ON o.day = s.day AND o.slot = s.slot "
        "WHERE s.day IN (?, ?, ?) ORDER BY s.day, s.slot",
        days,
    ).fetchall()
    if not rows:
        return 0

    # 이미 `sleep` 인 칸도 조용한 것으로 센다 — 이웃 날은 다시 롤업되지 않았을 수 있고,
    # 그래야 여러 번 돌려도 같은 결과가 나온다(멱등).
    quiet = [
        r["override"] is None
        and r["category"] in (away, off, SLEEP_CATEGORY)
        and float(r["start_ts"]) + slot_sec <= now
        for r in rows
    ]

    changed: list[tuple[str, int]] = []
    run = None
    for i in range(len(rows) + 1):
        if i < len(rows) and quiet[i]:
            if run is None:
                run = i
        elif run is not None:
            if need <= i - run <= cap:
                changed += [
                    (rows[j]["day"], int(rows[j]["slot"]))
                    for j in range(run, i)
                    if rows[j]["category"] != SLEEP_CATEGORY
                ]
            run = None

    if changed:
        with db.transaction(conn) as tx:
            tx.executemany(
                "UPDATE slot SET category = ? WHERE day = ? AND slot = ?",
                [(SLEEP_CATEGORY, dy, sl) for dy, sl in changed],
            )
    return len(changed)



def rollup_day(
    conn: sqlite3.Connection, cfg, classifier: Classifier, day: str, *, now: float | None = None
) -> RollupResult:
    """하루치 슬롯을 다시 계산해 slot/slot_breakdown 을 지우고 다시 쓴다 (멱등).

    전부 하나의 트랜잭션 안에서 실행된다.
    """
    tz = cfg.tz
    slot_minutes = cfg.rollup.slot_minutes
    n_slots = cfg.slots_per_day
    min_active_ratio = cfg.rollup.min_active_ratio
    off_category = cfg.rollup.no_data_category
    away_category = cfg.rollup.afk_category  # 삭제된 구간을 이걸로 그린다 (아래 private_spans)
    boundary_hour = cfg.rollup.day_boundary_hour  # 논리적 하루 경계(기본 06:00). 전달만 한다 — 계산 로직은 timeutil 소관.

    day_start, day_end = timeutil.day_bounds(day, tz, boundary_hour=boundary_hour)

    devices = _fetch_day_events(conn, day_start, day_end)
    manual_entries = _fetch_manual_entries(conn, day_start, day_end)

    # ── 프라이빗 구간 (2026-09-01) ────────────────────────────────────────
    #
    # ★ **원천에서 뺀다.** `apply_overrides` 처럼 마지막에 `slot.category` 만 덮는
    #   방식은 안 된다 — 그건 `slot_breakdown` 을 안 건드려서 집계에는 그대로 남는다
    #   (`plan/override.py` 가 그렇게 하는 이유와 한계를 적어 뒀다).
    #   여기서 빼면 breakdown 에 애초에 안 들어가므로 `by_category`·주간 집계·
    #   `top_apps` 가 전부 자동으로 따라온다 — 기제를 하나만 둔다.
    #
    # ★ `manual_entry` 도 뺀다. 안 빼면 "프라이빗인데 그 시간의 수동 기록은 집계에
    #   남는" 모순이 생긴다. 행은 지우지 않으므로 구간을 줄이면 다시 살아난다.
    private_spans = privacy.load_spans(conn, day_start, day_end)
    # ★ **토글로 켠 것만** 화면에서 '프라이빗' 이다 (2026-09-04).
    #   소급 삭제(`kind='purge'`)는 **자리비움**으로 그린다 — 지운 사람의 뜻이
    #   "이 시간을 없애 달라" 이지 "여기 뭔가 있었다고 표시해 달라" 가 아니다.
    #   이벤트를 자르는 데는 둘 다 똑같이 쓴다(`private_spans`). 가르는 건 표시뿐이다.
    live_raw = privacy.load_spans(conn, day_start, day_end, kinds=("live",))
    purge_spans = privacy.load_spans(conn, day_start, day_end, kinds=("purge",))
    # ★ **삭제가 프라이빗을 이긴다** (2026-09-07).
    #
    #   프라이빗 구간에는 이벤트가 아예 없다 — 저장 관문이 들어올 때 잘라냈다.
    #   그래서 그 칸에 "기록 지우기" 를 눌러도 지울 행이 없고, 구간이 남아 칸은 계속
    #   프라이빗이었다. **사람 눈에는 삭제가 안 먹는 것으로 보인다.**
    #
    #   뜻으로 보면 둘은 다른 요청이다:
    #     프라이빗 = "이 시간은 가려 달라"  → 가렸다는 **표시가 남는다**
    #     삭제     = "이 시간을 없애 달라"  → **흔적도 남기지 않는다**(자리비움)
    #
    #   그래서 삭제 구간과 겹치는 프라이빗은 빼고 센다. 되돌리면(`undo` 가 purge 구간을
    #   revoked 로 만든다) 프라이빗 표시가 **저절로 돌아온다** — 따로 기록할 게 없다.
    live_spans = timeutil.subtract_spans(live_raw, purge_spans) if purge_spans else live_raw
    if private_spans:
        for dev in devices:
            for btype, evs in dev.buckets.items():
                dev.buckets[btype] = _clip_events_excluding(evs, private_spans)
        manual_entries = _clip_events_excluding(manual_entries, private_spans)

    # ★ 순서가 중요하다: **흡수 → 중재**.
    #
    # 1) 슬롯으로 쪼개기 **전에** 짧은 기기 전환을 흡수한다. 슬롯 단위로는 공백의
    #    진짜 길이를 알 수 없다 — 3분짜리 공백이 슬롯 경계에 걸치면 두 조각으로 보인다.
    # 2) 흡수는 폰이 아닌 기기(노트북)의 타임라인에서만 한다. "잠깐 폰 만진 3분"을
    #    앞 활동으로 덮는 것이 이 단계의 목적이기 때문이다.
    # 3) 흡수한 구간은 중재에서 그 기기 소유로 **고정**한다. 안 그러면 폰이 그 구간을
    #    되찾아 흡수와 중재가 같은 구간을 놓고 싸운다 (반복 실패 2번).
    absorbed_n = 0
    absorbed_sec = 0.0
    locked: list[tuple[float, float]] = []
    locked_key: object | None = None
    threshold = getattr(cfg.rollup, "switch_absorb_sec", 0.0)
    for dev in devices:
        if dev.is_phone:
            continue
        spans: list[tuple[float, float]] = []
        n, sec = absorb_short_switches(
            dev.buckets["window"], dev.buckets["afk"], threshold, absorbed_out=spans
        )
        absorbed_n += n
        absorbed_sec += sec
        if spans and locked_key is None:
            locked_key = dev.key
        if dev.key == locked_key:
            locked.extend(spans)

    window_events, afk_events, web_events = _arbitrate_devices(devices, locked, locked_key)

    afk_idx = window_idx = web_idx = manual_idx = 0
    updated_at = now if now is not None else timeutil.now_ts()

    unclassified_acc: dict[str, list] = {}
    slot_rows: list[tuple] = []
    breakdown_rows: list[tuple] = []
    total_active_sec = 0.0
    off_slots = 0
    private_slots = 0

    for slot in range(n_slots):
        s, e = timeutil.slot_bounds(day, slot, tz, slot_minutes, boundary_hour=boundary_hour)
        if e <= s:
            # DST 경계 등으로 슬롯이 비어버리는 극단적인 경우. 빈 슬롯으로 기록한다.
            slot_rows.append(
                (day, slot, s, off_category, None, None, None, 0.0, 0.0, 0.0, 0.0, 0.0, "none", 1.0, updated_at)
            )
            off_slots += 1
            continue

        afk_hits, afk_idx = _advance_and_collect(afk_events, afk_idx, s, e)
        window_hits, window_idx = _advance_and_collect(window_events, window_idx, s, e)
        web_hits, web_idx = _advance_and_collect(web_events, web_idx, s, e)
        manual_hits, manual_idx = _advance_and_collect(manual_entries, manual_idx, s, e)

        work = _SlotWork()
        _process_slot(
            work, classifier, cfg.rollup, s, e, afk_hits, window_hits, web_hits, manual_hits, unclassified_acc
        )

        total_active_sec += work.active_sec

        # 이 슬롯에서 재지 않기로 한 초. `gap_sec`(관측 실패)와 **다른 것**이다.
        hidden_sec = timeutil.spans_overlap_sec((s, e), private_spans) if private_spans else 0.0
        live_sec = timeutil.spans_overlap_sec((s, e), live_spans) if live_spans else 0.0
        # `slot.private_sec` 에는 **토글로 켠 것만** 싣는다. 삭제분까지 실으면
        # "자리비움처럼 보이는데 프라이빗 초가 붙어 있는" 칸이 되어 그대로 새어 나간다.
        private_sec = live_sec

        # manual_hits 는 이미 overlap>0 인 것만 담겨 있다 (_advance_and_collect).
        has_manual = bool(manual_hits)

        forced_off = (work.active_sec / (e - s)) < min_active_ratio and not has_manual

        # ★ 과반이 프라이빗이면 그 칸은 프라이빗이다. **1초라도 겹치면** 으로 하면
        #   1분짜리 프라이빗이 10분 칸을 통째로 잡아먹어 거짓말이 된다.
        #   과반이 아닌 칸은 정상 판정하되, 프라이빗 부분은 이미 위에서 빠졌으므로
        #   어디에도 안 실린다.
        if live_sec > (e - s) / 2:
            category = _PRIVATE_CATEGORY
            subcategory = top_app = top_title = None
            winner_sec = live_sec
            source = "private"
            private_slots += 1
        elif hidden_sec > (e - s) / 2:
            # 삭제된 구간이 과반이다. **자리비움과 똑같이 적는다** — category·source·
            # winner_sec 어느 것으로도 구분되면 안 된다. 근거는 `private_span` 테이블이
            # 계속 갖고 있으므로 추적성은 안 잃는다.
            category = away_category
            subcategory = top_app = top_title = None
            winner_sec = hidden_sec
            source = "rule"
        elif forced_off or not work.win_score:
            category = off_category
            subcategory = None
            top_app = None
            top_title = None
            winner_sec = 0.0
            source = "none"
            off_slots += 1
        else:
            # 6. 승자 = 초가 가장 큰 카테고리. 동점이면 classifier.order() 순.
            order = classifier.order()
            order_rank = {cat: i for i, cat in enumerate(order)}
            category = max(
                work.win_score,
                key=lambda cat: (work.win_score[cat], -order_rank.get(cat, len(order))),
            )
            subcategory = work.category_subcat.get(category)
            winner_sec = sum(sec for (cat, _a, _d), sec in work.breakdown.items() if cat == category)

            # 이 슬롯의 top_app/top_title: 승자 카테고리에 가장 크게 기여한 (app, title).
            winner_contribs = [c for c in work.contribs if c[0] == category]
            if winner_contribs:
                best = max(winner_contribs, key=lambda c: c[3])
                top_app, top_title = best[1], best[2]
            else:
                top_app = top_title = None

            in_rule = category in work.rule_categories
            in_manual = category in work.manual_categories
            if in_rule and in_manual:
                source = "mixed"
            elif in_manual:
                source = "manual"
            else:
                source = "rule"

            if category == off_category:
                off_slots += 1

        slot_rows.append(
            (
                day,
                slot,
                s,
                category,
                subcategory,
                top_app,
                top_title,
                work.active_sec,
                work.afk_sec,
                work.gap_sec,
                private_sec,
                winner_sec,
                source,
                1.0,
                updated_at,
            )
        )

        for (cat, app, device_id), seconds in work.breakdown.items():
            if seconds > 0:
                breakdown_rows.append((day, slot, cat, app, device_id, seconds))


    # 9. 해당 날짜 것을 지우고 다시 쓴다 (멱등). 전부 한 트랜잭션.
    with db.transaction(conn):
        conn.execute("DELETE FROM slot WHERE day = ?", (day,))
        conn.execute("DELETE FROM slot_breakdown WHERE day = ?", (day,))

        conn.executemany(
            """
            INSERT INTO slot(
                day, slot, start_ts, category, subcategory, top_app, top_title,
                active_sec, afk_sec, gap_sec, private_sec, winner_sec, source, confidence, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            slot_rows,
        )

        if breakdown_rows:
            # (day, slot, category, app, device_id) 키는 슬롯별 딕셔너리에서 이미 유일하게
            # 모아졌으므로 단순 INSERT 로 충분하다 (같은 날짜 행은 위에서 이미 지웠다).
            conn.executemany(
                """
                INSERT INTO slot_breakdown(day, slot, category, app, device_id, seconds)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                breakdown_rows,
            )

        # 8. 기본값으로 떨어진 (app, title) 지문을 unclassified/unclassified_day 에 누적한다.
        #
        # 롤업은 오늘 것을 몇 분마다 다시 돌리므로, 날짜별 실적(unclassified_day)은
        # slot/slot_breakdown 과 같은 "지우고 다시 쓰기" 패턴으로 멱등하게 유지하고,
        # 지문의 정체성(unclassified)은 그 위에서 다시 계산한다.
        # unclassified.llm_category/llm_subcategory/llm_confidence/llm_at 은 이미 태깅된
        # 결과이므로 여기서 절대 건드리지 않는다.
        #
        conn.execute("DELETE FROM unclassified_day WHERE day = ?", (day,))

        if unclassified_acc:
            conn.executemany(
                "INSERT INTO unclassified_day(day, fingerprint, seconds, hits) VALUES (?, ?, ?, ?)",
                [
                    (day, fp, seconds, hits)
                    for fp, (app, title, seconds, hits, first_seen, last_seen) in unclassified_acc.items()
                ],
            )

            # 정체성(app/title_sample/first_seen/last_seen)만 upsert. seconds_total/hits/llm_* 는 안 건드린다.
            conn.executemany(
                """
                INSERT INTO unclassified(fingerprint, app, title_sample, seconds_total, hits, first_seen, last_seen)
                VALUES (?, ?, ?, 0, 0, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    app = excluded.app,
                    title_sample = excluded.title_sample,
                    first_seen = MIN(unclassified.first_seen, excluded.first_seen),
                    last_seen = MAX(unclassified.last_seen, excluded.last_seen)
                """,
                [
                    (fp, app, title, first_seen, last_seen)
                    for fp, (app, title, seconds, hits, first_seen, last_seen) in unclassified_acc.items()
                ],
            )

        # seconds_total/hits 는 unclassified_day 전체(모든 날짜)의 합으로 **재계산**한다 —
        # 직접 더하면 같은 날 재롤업 시 부풀어 오른다.
        #
        # ★ 대상은 이번에 미분류로 떨어진 지문이 아니라 **테이블 전체**다.
        #   규칙을 보강하면(2026-08-19 게임 규칙) 어떤 지문은 더 이상 미분류가
        #   아니게 되는데, 그 지문은 `unclassified_acc` 에 없으므로 "이번에 본 것만"
        #   재계산하면 옛날 합계가 그대로 굳는다. 실제로 규칙 추가 직후 미분류
        #   1~3위가 방금 분류한 게임 세 개였다. 미분류 지문은 수십~수백 개 규모라
        #   전수 재계산 비용이 무의미하다 — 파생값은 한 곳에서만 계산한다.
        conn.execute(
            """
            UPDATE unclassified
            SET seconds_total = (
                    SELECT COALESCE(SUM(seconds), 0) FROM unclassified_day
                    WHERE unclassified_day.fingerprint = unclassified.fingerprint
                ),
                hits = (
                    SELECT COALESCE(SUM(hits), 0) FROM unclassified_day
                    WHERE unclassified_day.fingerprint = unclassified.fingerprint
                )
            """
        )

        # 어느 날짜에도 실적이 안 남은 지문은 지운다. 다시 나타나면 그때 다시 만들어진다.
        # **LLM 태깅 결과가 있는 것은 남긴다** — 규칙으로 흡수됐는지 사람이 확인하기
        # 전에 지우면 태깅에 쓴 GPU 시간이 증발한다.
        conn.execute(
            "DELETE FROM unclassified WHERE seconds_total = 0 AND llm_category IS NULL"
        )

    # 사람이 넣은 수동 보정(slot_override)을 slot.category 에 반영한다.
    # slot_override 테이블 자체는 지우지 않고(사람의 입력), slot_breakdown 도
    # 건드리지 않는다(실측 원본 보존) — 격자 표시만 바뀌고 집계 숫자는 그대로다.
    apply_overrides(conn, day)

    # 10. **잠든 시간을 추정한다** (2026-09-05). 보정 **뒤에** 돈다 — 사람이 고친 칸은
    #     안 건드리고, 하루 경계를 넘어 이웃 날까지 같이 본다 (`infer_sleep` 머리말).
    infer_sleep(conn, cfg, day, now=updated_at)

    # ★ 커버리지는 **추정 뒤에** 센다. 수면은 `off`/`away` 에서 왔으므로 여전히
    #   "재어지지 않은 시간" 이다 — 잠으로 이름이 바뀌었다고 커버리지가 오르면
    #   그건 측정이 나아진 게 아니라 이름을 바꾼 것이다. stats.py 가 같은 규칙을 쓴다.
    off_slots = int(
        conn.execute(
            "SELECT count(*) AS n FROM slot WHERE day = ? AND category IN (?, ?)",
            (day, off_category, SLEEP_CATEGORY),
        ).fetchone()["n"]
    )

    measurable = n_slots - private_slots
    coverage = (measurable - off_slots) / measurable if measurable else 0.0

    logger.info(
        "롤업 완료: day=%s slots=%d breakdown_rows=%d unclassified=%d coverage=%.2f",
        day,
        len(slot_rows),
        len(breakdown_rows),
        len(unclassified_acc),
        coverage,
    )

    return RollupResult(
        day=day,
        slots_written=len(slot_rows),
        breakdown_rows=len(breakdown_rows),
        unclassified_seen=len(unclassified_acc),
        active_sec=total_active_sec,
        coverage=coverage,
        private_slots=private_slots,
    )


def rollup_range(
    conn: sqlite3.Connection, cfg, classifier: Classifier, start_day: str, end_day: str
) -> list[RollupResult]:
    """[start_day, end_day] (양 끝 포함) 을 하루씩 rollup_day 로 처리한다."""
    results = []
    for day in timeutil.day_range(start_day, end_day):
        results.append(rollup_day(conn, cfg, classifier, day))
    return results
