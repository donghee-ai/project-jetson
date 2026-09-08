"""리포트용 집계 — 숫자는 전부 SQL 에서 나온다.

설계 원칙 2번(`docs/life-trainer-design.md`): "집계는 SQL, 문장화는 LLM".
이 모듈은 파이썬에서 이벤트를 다시 순회해 초를 더하지 않는다.
`slot_breakdown` 이 카테고리별 집계의 원천이다 — `slot.category` 는 승자 하나만
남기는 시각화용 값이라 "이번 주 코딩 몇 시간" 같은 질문의 근거로 쓰면 틀린 숫자가 나온다.
`slot.active_sec`/`afk_sec`/`gap_sec` 는 카테고리와 무관한 원시 관측량이라
(위너 테이크 올이 아니다) 여기서 그대로 SUM 해도 정직하다.

LLM 은 이 단계에 전혀 없다. `render_*_text` 는 이미 계산된 숫자를 한국어
문장 틀에 끼워 넣을 뿐이다 (문장 "생성" 이 아니라 포맷팅).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from lifetrainer import timeutil
from lifetrainer.plan.models import carry_debt_titles

if TYPE_CHECKING:  # 순환 import 방지 — rollup(B) 이 만드는 Classifier 는 타입 힌트로만 쓴다.
    from lifetrainer.config import Config
    from lifetrainer.rollup.classify import Classifier

# 카테고리 목록에서 "활동" 으로 치지 않는 예약 카테고리.
# 이 두 값은 config.rollup.afk_category / no_data_category 로도 오지만,
# 대부분의 배포에서 'away'/'off' 로 고정이라 SQL 바인딩용 상수로도 쓴다.
_TOP_APPS_LIMIT = 10


@dataclass
class CategorySec:
    """카테고리 하나가 차지한 초와 활동 총합 대비 비율."""

    category: str
    seconds: float
    share: float  # 0~1, by_category 안에서 총합 대비 (away/off 제외 총합 기준)


@dataclass
class DailyStats:
    """하루치 집계 결과. 전부 SQL 에서 나온 값이다."""

    day: str
    total_span_sec: float  # 관측된 첫 활동 ~ 마지막 활동
    active_sec: float
    afk_sec: float
    off_sec: float
    private_sec: float  # 재지 않기로 한 초 (수집 실패가 아니다)
    sleep_sec: float    # 잤다고 본 초 — 추정 + 사람이 표에서 고친 것 (2026-09-05)
    repeated_defers: list[tuple[str, int]]  # 2번 이상 미룬 것 (제목, 횟수). 없으면 빈 목록
    coverage: float
    by_category: list[CategorySec]  # 내림차순, away/off 제외
    top_apps: list[tuple[str, float]]
    first_activity_ts: float | None
    last_activity_ts: float | None
    longest_focus: tuple[str, int, int] | None  # (category, 시작슬롯, 슬롯길이)
    slot_categories: list[str]  # 길이 144(기본), 시각화용


@dataclass
class DeviceSec:
    """하루 중 이 기기가 차지한 활동 시간."""

    device_id: int | None
    name: str
    kind: str  # 'laptop' | 'phone' | 'tablet' | ...
    seconds: float


def device_breakdown(conn: sqlite3.Connection, cfg, day: str) -> list[DeviceSec]:
    """하루를 **기기별**로 가른다 (내림차순). away/off 는 활동이 아니므로 뺀다.

    이 값은 폰이 들어오기 전에는 의미가 없었다 — 기기가 하나뿐이었고, 롤업이
    `slot_breakdown.device_id` 를 아예 안 채우고 있었다(2026-08-22 마이그레이션 003
    전까지 100% NULL). 이제 "코딩 2시간" 안에서 노트북과 폰이 갈린다.

    `device_id` 가 NULL 인 행은 사람이 손으로 넣은 구간이다 — 기기가 없는 게 맞다.
    """
    rows = conn.execute(
        """
        SELECT b.device_id, d.name, d.kind, SUM(b.seconds) AS secs
        FROM slot_breakdown b
        LEFT JOIN device d ON d.id = b.device_id
        WHERE b.day = ? AND b.category NOT IN ('away', 'off')
        GROUP BY b.device_id, d.name, d.kind
        HAVING secs > 0
        ORDER BY secs DESC
        """,
        (day,),
    ).fetchall()
    return [
        DeviceSec(
            device_id=r["device_id"],
            name=r["name"] or "수동 입력",
            kind=r["kind"] or "manual",
            seconds=float(r["secs"]),
        )
        for r in rows
    ]


@dataclass
class WeeklyStats:
    """7일치 집계 + 지난주 대비 비교."""

    end_day: str
    days: list[str]
    this_week: list[CategorySec]
    last_week: list[CategorySec]
    delta: dict[str, float]  # category -> 초 증감 (this_week - last_week)
    daily_active: list[tuple[str, float]]
    best_day: tuple[str, float] | None


def _share_list(rows: list[tuple[str, float]]) -> list[CategorySec]:
    """(category, seconds) 목록을 내림차순 CategorySec 목록으로. share 합은 1.0(부동소수 오차 제외)."""
    total = sum(sec for _, sec in rows)
    ordered = sorted(rows, key=lambda p: p[1], reverse=True)
    return [
        CategorySec(category=cat, seconds=sec, share=(sec / total if total > 0 else 0.0))
        for cat, sec in ordered
    ]


def category_seconds(
    conn: sqlite3.Connection,
    cfg,
    day: str,
    *,
    overlay: dict[int, str] | None = None,
) -> dict[str, float]:
    """그 날의 **카테고리별 초** — 화면과 집계가 쓰는 단 하나의 값.

    ## 왜 함수가 하나여야 하나

    `slot_breakdown` 은 실측 원본이라 **사람의 보정이 안 들어간다**
    (`plan/override.py` 의 결정: "실측 원본은 절대 건드리지 않는다"). 그건 옳은데,
    각 화면이 원본을 직접 SELECT 하면 **사람이 칠한 것이 그 화면에만 안 나타난다.**

    합성 회귀 데이터에서 `학습`으로 보정하니:

        격자 칸      learning ✅        오늘의 수면·커버리지  ✅ (slot.category 를 본다)
        차트·원그래프  학습 없음 ❌        상위 앱·기기별        ❌ (slot_breakdown 을 본다)

    **수면만의 문제가 아니라 모든 카테고리에서 그랬다.** 화면마다 따로 고치면 다음에
    붙는 화면이 또 틀린다. 그래서 원본은 그대로 두고 **읽는 함수를 하나로** 모은다.
    `tests/test_no_direct_breakdown.py` 가 새 직접 조회를 막는다.

    ## 규칙

    | 층 | 이기는 순서 | 그 칸이 싣는 값 |
    |---|---|---|
    | `overlay`(계획) | 1 | 칸 길이 **전부** |
    | `slot_override`(사람의 보정) | 2 | 칸 길이 **전부** |
    | `slot_breakdown`(실측) | 3 | 잰 초 그대로 |

    ★ 보정·계획 칸이 **칸 길이 전부**인 이유: 실측 초만 쓰면 `off` 였던 칸(측정 0초)을
      보정해도 여전히 0이라 화면에 안 나타난다 — "그때 이걸 했다" 는 말을 무시하는 셈이다.

    ★ `away`/`off` 는 활동이 아니라 언제나 뺀다. `sleep`·`private` 은 `slot_breakdown`
      에 애초에 안 들어간다(슬롯 라벨이라서) — 필요한 화면이 따로 싣는다.
    """
    away_cat = cfg.rollup.afk_category
    off_cat = cfg.rollup.no_data_category
    slot_seconds = cfg.rollup.slot_minutes * 60.0
    overlay = overlay or {}

    overrides = {
        int(r["slot"]): r["category"]
        for r in conn.execute("SELECT slot, category FROM slot_override WHERE day = ?", (day,))
    }
    taken = set(overlay) | set(overrides)

    totals: dict[str, float] = {}
    for r in conn.execute(
        "SELECT slot, category, SUM(seconds) AS sec FROM slot_breakdown "
        "WHERE day = ? AND category NOT IN (?, ?) GROUP BY slot, category",
        (day, away_cat, off_cat),
    ):
        if int(r["slot"]) in taken:
            continue
        totals[r["category"]] = totals.get(r["category"], 0.0) + float(r["sec"])

    for source in (overrides, overlay):   # 보정 → 계획 순으로 덮는다
        for slot, cat in source.items():
            if slot in overlay and source is overrides:
                continue                  # 계획이 있는 칸은 계획이 이긴다
            if cat in (away_cat, off_cat):
                continue
            totals[cat] = totals.get(cat, 0.0) + slot_seconds
    return totals


def compute_daily(conn: sqlite3.Connection, cfg: "Config", day: str) -> DailyStats:
    """하루치 통계를 SQL 집계만으로 계산한다.

    데이터가 아예 없는 날(=`slot` 에 그 날짜 행이 없음)에도 예외 없이
    전부 0으로 채운 빈 통계를 반환한다.
    """
    slot_minutes = cfg.rollup.slot_minutes
    slot_seconds = slot_minutes * 60
    slots_total = cfg.slots_per_day
    off_cat = cfg.rollup.no_data_category
    away_cat = cfg.rollup.afk_category

    base_row = conn.execute(
        "SELECT COALESCE(SUM(active_sec), 0) AS active_sec, "
        "COALESCE(SUM(afk_sec), 0) AS afk_sec, COUNT(*) AS n_slots "
        "FROM slot WHERE day = ?",
        (day,),
    ).fetchone()
    active_sec = float(base_row["active_sec"])
    afk_sec = float(base_row["afk_sec"])
    n_slots = int(base_row["n_slots"])

    if n_slots == 0:
        # 이 날짜는 아직 롤업된 적이 없다 — 커버리지를 1.0으로 오판하면 안 되므로
        # off_sec/coverage 도 전부 0으로 둔다 ("데이터 없음"과 "하루 종일 off"는 다르다).
        off_sec = 0.0
        private_sec = 0.0
        sleep_sec = 0.0
        coverage = 0.0
    else:
        # ★ `sleep` 도 같이 센다 (2026-09-05). 수면은 `off`/`away` 에서 추정한 것이라
        #   여전히 **재어지지 않은 시간**이다 — 이름이 바뀌었다고 커버리지가 오르면
        #   측정이 나아진 것처럼 보인다. rollup.py 가 같은 규칙을 쓴다.
        off_row = conn.execute(
            "SELECT COUNT(*) AS n FROM slot WHERE day = ? AND category IN (?, 'sleep')",
            (day, off_cat),
        ).fetchone()
        off_slots = int(off_row["n"])
        off_sec = off_slots * slot_seconds
        # ★ 프라이빗은 분모에서 뺀다 — 수집 실패가 아니라 **재지 않기로 한 시간**이다.
        #   같은 식이 rollup.py 에도 있다. 한쪽만 고치면 `lt rollup` 출력과 웹이 갈린다.
        priv_slots = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM slot WHERE day = ? AND category = 'private'", (day,)
            ).fetchone()["n"]
        )
        private_sec = priv_slots * slot_seconds
        # ★ **추정과 보정을 같이 센다.** `slot.category` 를 세므로 사람이 표에서
        #   수면으로 바꾼 칸(낮잠·중간에 깬 밤)도 그대로 들어온다 — 추정이 못 잡는
        #   쪽은 사람이 고치고, 그 고친 것이 이 숫자에 반영돼야 고칠 맛이 난다.
        sleep_sec = float(
            conn.execute(
                "SELECT COUNT(*) AS n FROM slot WHERE day = ? AND category = 'sleep'", (day,)
            ).fetchone()["n"]
        ) * slot_seconds
        measurable = slots_total - priv_slots
        coverage = (measurable - off_slots) / measurable if measurable else 0.0

    span_row = conn.execute(
        "SELECT MIN(start_ts) AS first_ts, MAX(start_ts) AS last_start "
        "FROM slot WHERE day = ? AND active_sec > 0",
        (day,),
    ).fetchone()
    first_activity_ts = span_row["first_ts"]
    last_activity_ts = (
        span_row["last_start"] + slot_seconds if span_row["last_start"] is not None else None
    )
    total_span_sec = (
        last_activity_ts - first_activity_ts
        if (first_activity_ts is not None and last_activity_ts is not None)
        else 0.0
    )

    by_category = _share_list(
        sorted(category_seconds(conn, cfg, day).items(), key=lambda kv: -kv[1])
    )

    app_rows = conn.execute(
        "SELECT app, SUM(seconds) AS sec FROM slot_breakdown "
        "WHERE day = ? AND app != '' AND category NOT IN (?, ?) "
        "GROUP BY app ORDER BY sec DESC LIMIT ?",
        (day, away_cat, off_cat, _TOP_APPS_LIMIT),
    ).fetchall()
    top_apps = [(r["app"], float(r["sec"])) for r in app_rows]

    # gaps-and-islands: 같은 카테고리가 연속되는 구간을 찾는다.
    # away/off 는 "집중" 이라 부를 대상이 아니므로 제외한다 (야간 off 가 매번
    # longest_focus 를 차지해버리면 지표로서 의미가 없다).
    #
    # ★ `sleep`·`private` 도 같은 이유로 뺀다 (2026-09-05). 수면은 정의상 야간 off 의
    #   다른 이름이라, 안 빼면 **최장 집중이 수면 구간**이 된다. 실제로 그렇게
    #   나왔고, 게다가 `classifier.label('sleep')` 이 KeyError 로 죽었다 —
    #   구조 상태는 `rules.yaml` 의 카테고리가 아니다.
    focus_row = conn.execute(
        """
        WITH ranked AS (
            SELECT slot, category,
                   slot - ROW_NUMBER() OVER (PARTITION BY category ORDER BY slot) AS grp
            FROM slot
            WHERE day = ? AND category NOT IN (?, ?, 'sleep', 'private')
        )
        SELECT category, MIN(slot) AS start_slot, COUNT(*) AS length
        FROM ranked
        GROUP BY category, grp
        ORDER BY length DESC, start_slot ASC
        LIMIT 1
        """,
        (day, away_cat, off_cat),
    ).fetchone()
    longest_focus: tuple[str, int, int] | None
    if focus_row is not None and focus_row["category"] is not None:
        longest_focus = (focus_row["category"], int(focus_row["start_slot"]), int(focus_row["length"]))
    else:
        longest_focus = None

    slot_rows = conn.execute(
        "SELECT slot, category FROM slot WHERE day = ? ORDER BY slot", (day,)
    ).fetchall()
    slot_map = {int(r["slot"]): r["category"] for r in slot_rows}
    slot_categories = [slot_map.get(i, off_cat) for i in range(slots_total)]

    return DailyStats(
        day=day,
        total_span_sec=total_span_sec,
        active_sec=active_sec,
        afk_sec=afk_sec,
        off_sec=off_sec,
        private_sec=private_sec,
        sleep_sec=sleep_sec,
        repeated_defers=carry_debt_titles(conn),
        coverage=coverage,
        by_category=by_category,
        top_apps=top_apps,
        first_activity_ts=first_activity_ts,
        last_activity_ts=last_activity_ts,
        longest_focus=longest_focus,
        slot_categories=slot_categories,
    )


def _week_category_totals(
    conn: sqlite3.Connection, days: list[str], away_cat: str, off_cat: str
) -> list[tuple[str, float]]:
    """주어진 날짜들에 대해 slot_breakdown 을 카테고리별로 합산한다 (away/off 제외)."""
    if not days:
        return []
    placeholders = ", ".join("?" for _ in days)
    rows = conn.execute(
        f"SELECT category, SUM(seconds) AS sec FROM slot_breakdown "
        f"WHERE day IN ({placeholders}) AND category NOT IN (?, ?) GROUP BY category",
        (*days, away_cat, off_cat),
    ).fetchall()
    return [(r["category"], float(r["sec"])) for r in rows]


def _daily_active_totals(conn: sqlite3.Connection, days: list[str]) -> dict[str, float]:
    """주어진 날짜들에 대해 slot.active_sec 합계를 날짜별로 구한다. 행이 없는 날짜는 0.0."""
    result = {d: 0.0 for d in days}
    if not days:
        return result
    placeholders = ", ".join("?" for _ in days)
    rows = conn.execute(
        f"SELECT day, COALESCE(SUM(active_sec), 0) AS sec FROM slot "
        f"WHERE day IN ({placeholders}) GROUP BY day",
        tuple(days),
    ).fetchall()
    for r in rows:
        result[r["day"]] = float(r["sec"])
    return result


def compute_weekly(conn: sqlite3.Connection, cfg: "Config", end_day: str) -> WeeklyStats:
    """end_day 를 포함한 최근 7일과, 그 직전 7일(지난주 같은 요일 범위)을 비교한다."""
    away_cat = cfg.rollup.afk_category
    off_cat = cfg.rollup.no_data_category

    end_date = date.fromisoformat(end_day)
    start_date = end_date - timedelta(days=6)
    days = timeutil.day_range(start_date.isoformat(), end_day)

    prev_end_date = start_date - timedelta(days=1)
    prev_start_date = prev_end_date - timedelta(days=6)
    prev_days = timeutil.day_range(prev_start_date.isoformat(), prev_end_date.isoformat())

    this_week = _share_list(_week_category_totals(conn, days, away_cat, off_cat))
    last_week = _share_list(_week_category_totals(conn, prev_days, away_cat, off_cat))

    this_map = {c.category: c.seconds for c in this_week}
    last_map = {c.category: c.seconds for c in last_week}
    delta = {
        cat: this_map.get(cat, 0.0) - last_map.get(cat, 0.0)
        for cat in set(this_map) | set(last_map)
    }

    active_by_day = _daily_active_totals(conn, days)
    daily_active = [(d, active_by_day[d]) for d in days]
    best_day = max(daily_active, key=lambda p: p[1]) if daily_active else None

    return WeeklyStats(
        end_day=end_day,
        days=days,
        this_week=this_week,
        last_week=last_week,
        delta=delta,
        daily_active=daily_active,
        best_day=best_day,
    )


def format_hm(seconds: float) -> str:
    """초를 '1시간 10분' 같은 한국어 문자열로. 분 단위로 내림한다.

    >>> format_hm(4230)
    '1시간 10분'
    >>> format_hm(600)
    '10분'
    >>> format_hm(0)
    '0분'
    """
    total_minutes = int(max(0.0, seconds) // 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours == 0:
        return f"{minutes}분"
    if minutes == 0:
        return f"{hours}시간"
    return f"{hours}시간 {minutes}분"


def render_daily_text(stats: DailyStats, classifier: "Classifier") -> str:
    """하루 통계를 사람이 읽는 한국어 평문으로 포맷한다 (LLM 없음, 순수 템플릿)."""
    lines = [f"{stats.day} 하루 요약"]
    lines.append(
        f"활동 {format_hm(stats.active_sec)} · 자리비움 {format_hm(stats.afk_sec)} "
        f"· 커버리지 {stats.coverage * 100:.0f}%"
    )

    if stats.by_category:
        lines.append("카테고리별:")
        for cs in stats.by_category:
            label = classifier.label(cs.category)
            lines.append(f"  - {label}: {format_hm(cs.seconds)} ({cs.share * 100:.0f}%)")
    else:
        lines.append("기록된 활동이 없습니다.")

    if stats.longest_focus is not None:
        # 렌더 함수는 cfg 를 받지 않으므로, 슬롯 하나의 길이는 slot_categories 의
        # 총 길이(하루=1440분)로부터 역산한다.
        n_slots = len(stats.slot_categories) or 144
        slot_seconds = (24 * 60 * 60) / n_slots
        category, _start_slot, length = stats.longest_focus
        focus_label = classifier.label(category)
        lines.append(f"최장 집중: {focus_label} {format_hm(length * slot_seconds)}")

    if stats.top_apps:
        top_label, top_sec = stats.top_apps[0]
        lines.append(f"가장 많이 쓴 앱: {top_label} ({format_hm(top_sec)})")

    return "\n".join(lines)


def render_weekly_text(stats: WeeklyStats, classifier: "Classifier") -> str:
    """주간 통계를 사람이 읽는 한국어 평문으로 포맷한다 (LLM 없음, 순수 템플릿)."""
    lines = [f"{stats.days[0]} ~ {stats.end_day} 주간 요약"]

    if stats.this_week:
        lines.append("카테고리별(이번 주):")
        for cs in stats.this_week:
            label = classifier.label(cs.category)
            change = stats.delta.get(cs.category, 0.0)
            sign = "+" if change >= 0 else "-"
            lines.append(
                f"  - {label}: {format_hm(cs.seconds)} ({cs.share * 100:.0f}%) "
                f"[{sign}{format_hm(abs(change))} 대비 지난주]"
            )
    else:
        lines.append("기록된 활동이 없습니다.")

    if stats.best_day is not None and stats.best_day[1] > 0:
        lines.append(f"가장 활동적인 날: {stats.best_day[0]} ({format_hm(stats.best_day[1])})")

    return "\n".join(lines)
