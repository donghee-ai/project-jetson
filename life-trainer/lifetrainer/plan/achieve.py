"""달성률 계산 (계약서 §3, v2).

설계 원칙 2번("집계는 SQL, 문장화는 LLM")을 그대로 따른다 — `slot_breakdown`
에서 초를 더하는 것은 전부 SQL `SUM`/`GROUP BY` 로 하고, 파이썬은 이미
SQL 이 계산해 준 계획 단위 숫자들을 조합만 한다 (슬롯을 순회하며 더하지 않는다).

v2 전환: `plans_for_day` 는 이제 `plan`(템플릿)이 아니라 `plan_instance`(그날의
실체)를 읽는다. 진입 시 `models.materialize_day` 를 먼저 호출해 그날 뜨는
템플릿들을 전개한다(멱등이라 여러 번 불러도 안전하다).

★ 벽시계 분 -> 슬롯 변환은 반드시 `timeutil.wallclock_min_to_slot` 을 거친다.
하루 경계가 06:00 으로 바뀌었기 때문에 `start_min // slot_minutes` 처럼 직접
나누면 경계를 무시하게 되어(자정 기준 슬롯 0을 가정) `slot_breakdown`(경계
보정이 끝난 값)과 어긋난다.
"""

from __future__ import annotations

import sqlite3

from lifetrainer import timeutil
from lifetrainer.plan.models import Plan, PlanInstance, materialize_day

# plan_instance 를 그날의 plan(템플릿)/subject 와 조인해 필요한 필드를 한 번에 가져온다.
# plan_id 가 NULL 인 인스턴스(수동/이월)는 plan 쪽 컬럼이 전부 NULL 로 온다 — _synth_plan 이 채운다.
_INSTANCES_FOR_DAY_SQL = """
SELECT
    pi.id AS instance_id, pi.day AS day, pi.plan_id AS plan_id, pi.title AS title,
    pi.subject_id AS subject_id, pi.category AS category,
    pi.start_min AS start_min, pi.end_min AS end_min,
    pi.status AS status, pi.ordinal AS ordinal,
    p.kind AS plan_kind, p.weekdays AS plan_weekdays,
    p.active_from AS plan_active_from, p.active_to AS plan_active_to,
    p.color AS plan_color, p.sort_order AS plan_sort_order, p.enabled AS plan_enabled,
    s.category AS subject_category, s.color AS subject_color
FROM plan_instance pi
LEFT JOIN plan p ON p.id = pi.plan_id
LEFT JOIN subject s ON s.id = pi.subject_id
WHERE pi.day = :day AND pi.archived_at IS NULL
ORDER BY pi.ordinal ASC, pi.id ASC
"""

# 인스턴스 각각의 이월 체인 깊이(0=원본). v_carry_debt 와 같은 재귀 CTE지만
# 루트별 최대값이 아니라 "이 노드 자신의 깊이" 를 구한다.
_DEPTH_SQL = """
WITH RECURSIVE chain(id, depth) AS (
    SELECT id, 0 FROM plan_instance WHERE carried_from IS NULL
    UNION ALL
    SELECT p.id, c.depth + 1 FROM plan_instance p JOIN chain c ON p.carried_from = c.id
)
SELECT id, depth FROM chain
"""


def _synth_plan(row: sqlite3.Row) -> Plan:
    """`plan_instance`(+ 조인된 plan/subject) 행 -> 하위호환용 `Plan` 합성.

    `report/planner.py`, `web/app.py`, `cli.py` 가 여전히 `pi.plan.title` 같은
    `Plan` 모양을 기대한다. 템플릿에서 온 인스턴스(`plan_id` 존재)는 실제 plan
    행 값을 쓰고, 수동/이월 인스턴스는 인스턴스 자체 값으로 합성한다.
    """
    plan_id = row["plan_id"]
    from_template = plan_id is not None
    effective_category = row["category"] if row["category"] is not None else row["subject_category"]
    color = row["plan_color"] if from_template else row["subject_color"]
    return Plan(
        id=int(plan_id) if from_template else int(row["instance_id"]),
        title=row["title"],
        category=effective_category,
        start_min=int(row["start_min"]),
        end_min=int(row["end_min"]),
        kind=row["plan_kind"] if from_template else "oneoff",
        weekdays=row["plan_weekdays"] if from_template else "",
        day=row["day"],
        active_from=row["plan_active_from"] if from_template else None,
        active_to=row["plan_active_to"] if from_template else None,
        color=color,
        sort_order=int(row["plan_sort_order"]) if from_template else int(row["ordinal"]),
        enabled=bool(row["plan_enabled"]) if from_template else True,
    )


def _actual_sec(
    conn: sqlite3.Connection, day: str, start_slot: int, end_slot: int, category: str | None, cfg
) -> float:
    """[start_slot, end_slot) 구간에서 계획 카테고리로 계측된 초의 합.

    `category` 가 None 이면 off/away 를 뺀 모든 활동 초의 합.

    ## ★ 사람의 보정을 읽을 때 얹는다 (2026-09-07)

    `slot_breakdown` 은 실측 원본이라 보정이 안 들어간다. 그대로 세면
    **"그때 코딩했다" 고 표에서 고쳐도 코딩 계획의 달성률이 안 오른다** —
    사람이 고친 것이 화면에는 반영되는데 달성률만 딴소리를 한다.

    같은 결함을 오늘 카테고리 합계에서 먼저 잡았고(`stats.category_seconds`),
    여기가 마지막 남은 자리였다. 규칙도 같다 — **보정된 칸은 칸 길이 전부**를
    그 카테고리에 싣는다. 실측 초만 쓰면 `off` 였던 칸을 고쳐도 0 이라 안 잡힌다.
    """
    slot_seconds = cfg.rollup.slot_minutes * 60.0
    away, off = cfg.rollup.afk_category, cfg.rollup.no_data_category

    overrides = {
        int(r["slot"]): r["category"]
        for r in conn.execute(
            "SELECT slot, category FROM slot_override "
            "WHERE day = ? AND slot >= ? AND slot < ?",
            (day, start_slot, end_slot),
        )
    }

    if category is not None:
        rows = conn.execute(
            "SELECT slot, COALESCE(SUM(seconds), 0) AS sec FROM slot_breakdown "
            "WHERE day = ? AND slot >= ? AND slot < ? AND category = ? GROUP BY slot",
            (day, start_slot, end_slot, category),
        ).fetchall()
        total = sum(float(r["sec"]) for r in rows if int(r["slot"]) not in overrides)
        total += slot_seconds * sum(1 for c in overrides.values() if c == category)
        return total

    rows = conn.execute(
        "SELECT slot, COALESCE(SUM(seconds), 0) AS sec FROM slot_breakdown "
        "WHERE day = ? AND slot >= ? AND slot < ? AND category NOT IN (?, ?) GROUP BY slot",
        (day, start_slot, end_slot, off, away),
    ).fetchall()
    total = sum(float(r["sec"]) for r in rows if int(r["slot"]) not in overrides)
    total += slot_seconds * sum(1 for c in overrides.values() if c not in (off, away))
    return total


def _dominant_actual(conn: sqlite3.Connection, day: str, start_slot: int, end_slot: int) -> str | None:
    """[start_slot, end_slot) 구간에서 초가 가장 큰 카테고리 (SQL 집계)."""
    row = conn.execute(
        "SELECT category, SUM(seconds) AS sec FROM slot_breakdown "
        "WHERE day = ? AND slot >= ? AND slot < ? "
        "GROUP BY category ORDER BY sec DESC LIMIT 1",
        (day, start_slot, end_slot),
    ).fetchone()
    return row["category"] if row is not None else None


def plans_for_day(conn: sqlite3.Connection, cfg, day: str) -> list[PlanInstance]:
    """그 날짜의 계획 인스턴스들을 달성률과 함께 반환한다.

    절차(계약서 §3):
    1. 진입 시 `materialize_day` 로 그날 뜨는 템플릿을 전개한다(멱등).
    2. `plan_instance`(+ plan/subject 조인)를 ordinal 순으로 읽는다.
    3. `subject_id` 가 있고 `category` 가 비어 있으면 subject 의 category 를 상속한다.
    4. [start_min, end_min) 을 `timeutil.wallclock_min_to_slot` 으로 슬롯 범위로 바꾼다
       (day_boundary_hour 기준 — 직접 `// slot_minutes` 로 나누지 않는다).
    5. actual_sec 은 slot_breakdown 에서 SQL SUM.
    6. achievement = min(1.0, actual_sec / planned_sec).
    """
    materialize_day(conn, cfg, day)

    slot_minutes = cfg.rollup.slot_minutes
    boundary_hour = cfg.rollup.day_boundary_hour

    rows = conn.execute(_INSTANCES_FOR_DAY_SQL, {"day": day}).fetchall()
    depth_map = {int(r["id"]): int(r["depth"]) for r in conn.execute(_DEPTH_SQL).fetchall()}

    instances: list[PlanInstance] = []
    for row in rows:
        effective_category = row["category"] if row["category"] is not None else row["subject_category"]

        start_slot = timeutil.wallclock_min_to_slot(
            int(row["start_min"]), slot_minutes, boundary_hour=boundary_hour
        )
        end_slot = timeutil.wallclock_min_to_slot(
            int(row["end_min"]), slot_minutes, boundary_hour=boundary_hour
        )
        planned_sec = float(int(row["end_min"]) - int(row["start_min"])) * 60.0

        actual_sec = _actual_sec(conn, day, start_slot, end_slot, effective_category, cfg)
        achievement = min(1.0, actual_sec / planned_sec) if planned_sec > 0 else 0.0
        dominant_actual = _dominant_actual(conn, day, start_slot, end_slot)

        instance_id = int(row["instance_id"])
        instances.append(
            PlanInstance(
                plan=_synth_plan(row),
                day=day,
                start_slot=start_slot,
                end_slot=end_slot,
                checked=(row["status"] == "done"),
                planned_sec=planned_sec,
                actual_sec=actual_sec,
                achievement=achievement,
                dominant_actual=dominant_actual,
                instance_id=instance_id,
                status=row["status"],
                carried_depth=depth_map.get(instance_id, 0),
            )
        )

    return instances


def day_achievement(conn: sqlite3.Connection, cfg, day: str) -> tuple[float, int, int]:
    """(전체 달성률 0~1, 달성한(100%) 계획 수, 전체 계획 수).

    전체 달성률은 계획별 planned_sec 으로 가중 평균한 값이다
    (개별 계획의 초과 달성이 하루 전체를 100% 넘게 만들지 않도록 actual 은
    planned 로 클리핑한다). 계획별 숫자는 plans_for_day 가 이미 SQL 로 계산해
    둔 것을 합산만 한다.
    """
    instances = plans_for_day(conn, cfg, day)
    total = len(instances)
    if total == 0:
        return 0.0, 0, 0

    planned_total = sum(pi.planned_sec for pi in instances)
    actual_total = sum(min(pi.actual_sec, pi.planned_sec) for pi in instances)
    overall = actual_total / planned_total if planned_total > 0 else 0.0
    achieved = sum(1 for pi in instances if pi.achievement >= 1.0)

    return overall, achieved, total
