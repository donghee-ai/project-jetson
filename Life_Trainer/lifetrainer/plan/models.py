"""계획(plan) CRUD + 계획 인스턴스(plan_instance) · 시간/요일 문자열 파서 (계약서 §3, v2).

`plan` 테이블은 사람이 선언한 "의도"(반복 템플릿)다. 반복 계획(`recurring`)과
일회성 계획(`oneoff`) 두 종류가 있고, 반복 계획은 `weekdays`(ISO 요일, 1=월..7=일)로
어느 요일에 뜨는지 정한다. 자정을 넘기는 계획은 지원하지 않는다 — 플래너는
하루 단위다.

`plan_instance` 는 그 템플릿이 특정 날짜에 전개된 "실체"다. `materialize_day` 가
템플릿 -> 인스턴스 전개를, `set_status` 가 상태 전환(및 `deferred` 일 때의 이월)을
담당한다. 모든 상태 전환은 `plan_instance_event` 에 감사 로그로 남는다.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, timedelta

from lifetrainer import db, timeutil

# ─────────────────────────────────────────────────────────────
# 데이터클래스
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Plan:
    id: int
    title: str
    category: str | None
    start_min: int
    end_min: int
    kind: str
    weekdays: str
    day: str | None
    active_from: str | None
    active_to: str | None
    color: str | None
    sort_order: int
    enabled: bool


@dataclass
class PlanInstance:
    """특정 날짜에 실제로 뜬 계획 하나 + 그날의 달성 결과 (achieve.py 가 채운다).

    `plan` 필드는 하위호환용이다 — `report/planner.py`, `web/app.py` 가 여전히
    `Plan` 모양(`.title`/`.start_min`/`.end_min`/`.kind`/`.weekdays`/...)을 기대하므로,
    `plan_instance` 기반으로 전환한 뒤에도 achieve.py 가 각 인스턴스에 대해 이 모양의
    객체를 합성해 채워 넣는다(템플릿에서 온 인스턴스는 실제 `plan` 행과 조인, 수동/이월
    인스턴스는 합성값). 새 코드는 `instance_id`/`status`/`carried_depth` 를 쓴다.
    """

    plan: Plan
    day: str
    start_slot: int  # 0..143 (day_boundary_hour 기준)
    end_slot: int  # exclusive
    checked: bool
    planned_sec: float
    actual_sec: float  # 그 구간에서 (상속 포함) 카테고리로 계측된 초
    achievement: float  # 0.0~1.0
    dominant_actual: str | None  # 그 구간에서 실제로 가장 많았던 카테고리
    # ── v2 신규 필드 (계약서 §3) ──────────────────────────────────────
    instance_id: int  # plan_instance.id — /done N 등 상태 조작의 실제 대상
    status: str  # todo|doing|done|partial|deferred|canceled
    carried_depth: int  # 이월 체인에서 이 인스턴스 자신의 깊이(0=원본)


@dataclass
class PlanInstanceRow:
    """`plan_instance` 테이블 행 그대로 (계약서 §3). `list_instances` 가 반환한다."""

    id: int
    day: str
    plan_id: int | None
    title: str
    subject_id: int | None
    category: str | None
    start_min: int
    end_min: int
    status: str
    priority: str
    ordinal: int
    planned_min: int | None
    note: str | None
    carried_from: int | None
    source: str
    archived: bool


_VALID_KINDS = frozenset({"recurring", "oneoff"})
_VALID_STATUSES = frozenset({"todo", "doing", "done", "partial", "deferred", "canceled"})
_VALID_PRIORITIES = frozenset({"low", "normal", "high"})
_VALID_SOURCES = frozenset({"template", "manual", "carry", "chat"})


def _row_to_plan(row: sqlite3.Row) -> Plan:
    return Plan(
        id=int(row["id"]),
        title=row["title"],
        category=row["category"],
        start_min=int(row["start_min"]),
        end_min=int(row["end_min"]),
        kind=row["kind"],
        weekdays=row["weekdays"],
        day=row["day"],
        active_from=row["active_from"],
        active_to=row["active_to"],
        color=row["color"],
        sort_order=int(row["sort_order"]),
        enabled=bool(row["enabled"]),
    )


def _validate_time_range(start_min: int, end_min: int) -> None:
    """자정을 넘기는 계획은 지원하지 않는다 — 플래너는 하루 단위다."""
    if not (0 <= start_min < end_min <= 1440):
        raise ValueError(
            f"잘못된 시간 범위입니다: start_min={start_min}, end_min={end_min} "
            "(0 <= start_min < end_min <= 1440 이어야 하며, 자정을 넘기는 계획은 지원하지 않습니다)"
        )


# ─────────────────────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────────────────────


def create_plan(
    conn: sqlite3.Connection,
    *,
    title: str,
    start_min: int,
    end_min: int,
    category: str | None = None,
    kind: str = "recurring",
    weekdays: str = "1234567",
    day: str | None = None,
    active_from: str | None = None,
    active_to: str | None = None,
    color: str | None = None,
    sort_order: int = 0,
) -> int:
    """계획을 만든다. 성공하면 새 `plan.id` 를 반환한다."""
    _validate_time_range(start_min, end_min)
    if kind not in _VALID_KINDS:
        raise ValueError(f"알 수 없는 계획 종류입니다: {kind!r} (recurring|oneoff)")
    if kind == "oneoff" and not day:
        raise ValueError("kind='oneoff' 계획은 day 를 지정해야 합니다")

    norm_weekdays = parse_weekdays(weekdays)
    now = timeutil.now_ts()
    with db.transaction(conn) as tx:
        cur = tx.execute(
            """
            INSERT INTO plan(
                title, category, start_min, end_min, kind, weekdays, day,
                active_from, active_to, color, sort_order, enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                title,
                category,
                start_min,
                end_min,
                kind,
                norm_weekdays,
                day,
                active_from,
                active_to,
                color,
                sort_order,
                now,
                now,
            ),
        )
        plan_id = cur.lastrowid
    return plan_id


_UPDATABLE_FIELDS = frozenset(
    {
        "title",
        "category",
        "start_min",
        "end_min",
        "kind",
        "weekdays",
        "day",
        "active_from",
        "active_to",
        "color",
        "sort_order",
        "enabled",
    }
)


def update_plan(conn: sqlite3.Connection, plan_id: int, **fields) -> None:
    """지정된 필드만 부분 갱신한다. `start_min`/`end_min` 중 하나만 바꿔도 전체 범위를 재검증한다."""
    if not fields:
        return

    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"알 수 없는 필드입니다: {sorted(unknown)}")

    existing = get_plan(conn, plan_id)
    if existing is None:
        raise ValueError(f"계획을 찾을 수 없습니다: id={plan_id}")

    to_write = dict(fields)

    if "start_min" in to_write or "end_min" in to_write:
        start_min = int(to_write.get("start_min", existing.start_min))
        end_min = int(to_write.get("end_min", existing.end_min))
        _validate_time_range(start_min, end_min)
        to_write["start_min"] = start_min
        to_write["end_min"] = end_min

    if "kind" in to_write and to_write["kind"] not in _VALID_KINDS:
        raise ValueError(f"알 수 없는 계획 종류입니다: {to_write['kind']!r} (recurring|oneoff)")

    if "weekdays" in to_write:
        to_write["weekdays"] = parse_weekdays(to_write["weekdays"])

    if "enabled" in to_write:
        to_write["enabled"] = 1 if to_write["enabled"] else 0

    set_clause = ", ".join(f"{k} = ?" for k in to_write)
    values = list(to_write.values())
    now = timeutil.now_ts()
    with db.transaction(conn) as tx:
        tx.execute(
            f"UPDATE plan SET {set_clause}, updated_at = ? WHERE id = ?",
            (*values, now, plan_id),
        )


def delete_plan(conn: sqlite3.Connection, plan_id: int, *, from_day: str | None = None) -> int:
    """계획을 지운다. `plan_skip`/`plan_check` 는 FK CASCADE 로 함께 지워진다.

    ★ **인스턴스도 같이 정리해야 한다.** `plan_instance.plan_id` 는 `ON DELETE SET NULL`
    이라, 계획만 지우면 그날의 인스턴스가 `plan_id=NULL` 로 살아남는다. 표시 기준이
    `archived_at IS NULL` 이므로 **지운 계획이 화면에 계속 뜬다** — 실제로 그랬다.

    `from_day` 이후의 인스턴스만 보관 처리한다. **과거는 남긴다** — 이미 지나간 날의
    달성 기록까지 지우면 주간 통계가 소급해서 바뀐다. 지운다는 것은 "앞으로 안 한다"
    이지 "그때도 안 했다"가 아니다.

    반환: 보관 처리한 인스턴스 수.
    """
    now = time.time()
    with db.transaction(conn) as tx:
        archived = 0
        if from_day is not None:
            cur = tx.execute(
                "UPDATE plan_instance SET archived_at = ?, updated_at = ? "
                "WHERE plan_id = ? AND day >= ? AND archived_at IS NULL",
                (now, now, plan_id, from_day),
            )
            archived = cur.rowcount or 0
        tx.execute("DELETE FROM plan WHERE id = ?", (plan_id,))
    return archived


def get_plan(conn: sqlite3.Connection, plan_id: int) -> Plan | None:
    row = conn.execute("SELECT * FROM plan WHERE id = ?", (plan_id,)).fetchone()
    return _row_to_plan(row) if row is not None else None


def list_plans(conn: sqlite3.Connection, *, enabled_only: bool = True) -> list[Plan]:
    if enabled_only:
        rows = conn.execute(
            "SELECT * FROM plan WHERE enabled = 1 ORDER BY sort_order, start_min"
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM plan ORDER BY sort_order, start_min").fetchall()
    return [_row_to_plan(r) for r in rows]


def skip_plan(conn: sqlite3.Connection, plan_id: int, day: str) -> None:
    """반복 계획을 그 날짜만 건너뛴다 (휴가·공휴일 등)."""
    with db.transaction(conn) as tx:
        tx.execute(
            "INSERT INTO plan_skip(plan_id, day) VALUES (?, ?) "
            "ON CONFLICT(plan_id, day) DO NOTHING",
            (plan_id, day),
        )


def unskip_plan(conn: sqlite3.Connection, plan_id: int, day: str) -> None:
    with db.transaction(conn) as tx:
        tx.execute("DELETE FROM plan_skip WHERE plan_id = ? AND day = ?", (plan_id, day))


def set_check(conn: sqlite3.Connection, plan_id: int, day: str, checked: bool) -> None:
    """레퍼런스 플래너의 체크박스 — `plan_instance` 로 위임하는 얇은 래퍼(계약서 §3 요구사항 5).

    시그니처를 바꾸지 않는다: `web/app.py`, `slackio/app.py`, `cli.py` 가 여전히
    `(conn, plan_id, day, checked)` 4개 인자로 이 함수를 부른다.

    1. `plan_check` 에도 그대로 기록한다 — 아직 그 날짜가 `materialize_day` 되지
       않았을 수 있고(예: 방금 만든 계획을 그날이 열리기 전에 체크), 그 경우
       `materialize_day` 가 나중에 인스턴스를 만들 때 이 값을 읽어 초기 상태를
       `done` 으로 잡아준다(db.py 마이그레이션 주석의 "이관"이 이 경로다).
    2. 이미 그 (plan_id, day) 의 `plan_instance` 가 있으면 `set_status` 로 즉시
       위임해 실시간으로 반영한다.
    """
    now = timeutil.now_ts()
    with db.transaction(conn) as tx:
        tx.execute(
            "INSERT INTO plan_check(plan_id, day, checked, checked_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(plan_id, day) DO UPDATE SET "
            "checked = excluded.checked, checked_at = excluded.checked_at",
            (plan_id, day, 1 if checked else 0, now),
        )
        row = tx.execute(
            "SELECT id FROM plan_instance WHERE plan_id = ? AND day = ? AND archived_at IS NULL",
            (plan_id, day),
        ).fetchone()
    if row is not None:
        # cfg 는 'done'/'todo' 전환 경로에서 쓰이지 않으므로(§3, set_status 참고) None 으로
        # 넘긴다 — set_check 는 옛 4-인자 시그니처라 cfg 를 받을 자리가 없다.
        set_status(conn, None, int(row["id"]), "done" if checked else "todo", actor="user")


# ─────────────────────────────────────────────────────────────
# 계획 인스턴스 — 전개 / 상태 / 이월 / 소프트 삭제
# ─────────────────────────────────────────────────────────────


def _snap_to_slot(minutes: int, slot_minutes: int) -> int:
    """분 단위 시각을 슬롯 경계로 반올림한다 (`cli.py` 의 `_snap_to_slot` 과 동일 규칙).

    달성률은 `slot_breakdown` 을 슬롯 단위로 집계하므로, 인스턴스 구간이 슬롯
    경계에 어긋나면 부정확해진다. 인스턴스를 만드는 모든 경로(템플릿 전개·수동
    추가·이월)가 이 함수를 거쳐 항상 슬롯에 맞춰 저장되게 한다.
    """
    return int(round(minutes / slot_minutes)) * slot_minutes


def _next_day(day: str) -> str:
    """'YYYY-MM-DD' 다음 날. 논리적 하루 문자열은 이미 경계 보정이 끝난 값이므로
    달력 날짜를 하루 더하는 것으로 충분하다(경계 시각과 무관)."""
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def _iso_weekday(day: str) -> str:
    """'YYYY-MM-DD' -> ISO 요일 문자('1'=월 .. '7'=일)."""
    return str(date.fromisoformat(day).isoweekday())


def _row_to_instance(row: sqlite3.Row) -> PlanInstanceRow:
    return PlanInstanceRow(
        id=int(row["id"]),
        day=row["day"],
        plan_id=row["plan_id"],
        title=row["title"],
        subject_id=row["subject_id"],
        category=row["category"],
        start_min=int(row["start_min"]),
        end_min=int(row["end_min"]),
        status=row["status"],
        priority=row["priority"],
        ordinal=int(row["ordinal"]),
        planned_min=row["planned_min"],
        note=row["note"],
        carried_from=row["carried_from"],
        source=row["source"],
        archived=row["archived_at"] is not None,
    )


# achieve.py 의 옛 _PLANS_FOR_DAY_SQL 과 동일한 판정(요일·활성구간·plan_skip) —
# "그 날짜에 뜨는 반복/일회성 템플릿"을 고르는 단 하나의 원본은 이제 여기다.
_PLAN_TEMPLATES_FOR_DAY_SQL = """
SELECT * FROM plan
WHERE enabled = 1
  AND (
        (kind = 'oneoff' AND day = :day)
        OR (
            kind = 'recurring'
            AND instr(weekdays, :iso_wd) > 0
            AND (active_from IS NULL OR active_from <= :day)
            AND (active_to IS NULL OR active_to >= :day)
            AND id NOT IN (SELECT plan_id FROM plan_skip WHERE day = :day)
        )
      )
ORDER BY sort_order ASC, start_min ASC
"""

_INSERT_INSTANCE_SQL = """
INSERT INTO plan_instance(
    day, plan_id, title, subject_id, category, start_min, end_min,
    status, priority, ordinal, planned_min, note, carried_from, source,
    created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
"""


def materialize_day(conn: sqlite3.Connection, cfg, day: str) -> int:
    """그 날짜에 뜨는 반복/일회성 계획 템플릿을 `plan_instance` 로 전개한다.

    **멱등**: (plan_id, day) 조합이 이미 `plan_instance` 에 있으면(소프트 삭제된
    것 포함 — 그래야 사용자가 지운 인스턴스가 10분마다 도는 롤업/조회에 의해
    되살아나지 않는다) 다시 만들지 않는다. `plan_skip` 은 애초에 후보 쿼리에서
    빠지므로 별도 처리가 필요 없다.

    기존 `plan_check.checked=1` 마킹이 있으면 새 인스턴스를 `status='done'` 으로
    시작한다 — 구 체계(`plan_check`) -> 신 체계(`plan_instance`) 이관은 여기서
    이뤄진다(`db.py` 마이그레이션 주석 참고. 그 마이그레이션 자체는 손대지 않는다).

    반환값: 이번 호출에서 새로 만든 인스턴스 수(멱등이므로 두 번째 호출부터는 0).
    """
    slot_minutes = cfg.rollup.slot_minutes
    iso_wd = _iso_weekday(day)
    templates = conn.execute(
        _PLAN_TEMPLATES_FOR_DAY_SQL, {"day": day, "iso_wd": iso_wd}
    ).fetchall()

    existing_plan_ids = {
        row["plan_id"]
        for row in conn.execute(
            "SELECT plan_id FROM plan_instance WHERE day = ? AND plan_id IS NOT NULL", (day,)
        ).fetchall()
    }

    now = timeutil.now_ts()
    created = 0
    with db.transaction(conn) as tx:
        for row in templates:
            plan_id = int(row["id"])
            if plan_id in existing_plan_ids:
                continue

            start_min = _snap_to_slot(int(row["start_min"]), slot_minutes)
            end_min = _snap_to_slot(int(row["end_min"]), slot_minutes)

            checked_row = tx.execute(
                "SELECT checked FROM plan_check WHERE plan_id = ? AND day = ?", (plan_id, day)
            ).fetchone()
            initial_status = "done" if checked_row is not None and bool(checked_row["checked"]) else "todo"

            tx.execute(
                _INSERT_INSTANCE_SQL,
                (
                    day, plan_id, row["title"], None, row["category"], start_min, end_min,
                    initial_status, "normal", end_min - start_min, None, None, "template",
                    now, now,
                ),
            )
            created += 1

    if created:
        renumber(conn, day)
    return created


def sync_instances_from_plan(conn: sqlite3.Connection, cfg, plan_id: int, *, from_day: str) -> int:
    """계획 템플릿이 바뀌면 **아직 안 지난 인스턴스**를 따라가게 한다.

    ## 왜 필요한가

    `materialize_day` 는 `(plan_id, day)` 기준으로 **멱등**이다 — 한 번 만들어진
    인스턴스는 다시 만들지 않는다(사용자가 지운 것이 10분마다 되살아나면 안 되므로).
    그 결과 **계획을 고쳐도 이미 만들어진 오늘 인스턴스는 옛 값을 그대로 들고 있었다.**
    화면은 `plan_instance` 를 읽으므로 사용자에게는 "수정이 저장이 안 된다" 로 보인다 —
    실제로는 `plan` 은 바뀌고 `plan_instance` 만 안 바뀐 것이었다(실측 사고 2026-08-23).

    ## 무엇을 건드리고 무엇을 안 건드리나

    - **`from_day` 이후만.** 과거는 기록이다 — `delete_plan` 과 같은 원칙으로,
      "앞으로 이렇게 한다" 이지 "그때도 그랬다" 가 아니다
    - **`source='template'` 만.** 사용자가 손으로 만든/옮긴 인스턴스(`manual`)를
      템플릿으로 덮으면 그 사람의 편집이 조용히 사라진다
    - **`status`·`note`·`ordinal` 은 그대로.** 시간을 옮겼다고 완료 표시가 풀리면 안 된다
    - 보관된 것(`archived_at IS NOT NULL`)은 건드리지 않는다

    반환: 갱신한 인스턴스 수.
    """
    plan = get_plan(conn, plan_id)
    if plan is None:
        return 0

    slot_minutes = cfg.rollup.slot_minutes
    start_min = _snap_to_slot(int(plan.start_min), slot_minutes)
    end_min = _snap_to_slot(int(plan.end_min), slot_minutes)
    now = timeutil.now_ts()

    with db.transaction(conn) as tx:
        cur = tx.execute(
            "UPDATE plan_instance SET title = ?, category = ?, start_min = ?, end_min = ?, "
            "planned_min = ?, updated_at = ? "
            "WHERE plan_id = ? AND day >= ? AND archived_at IS NULL AND source = 'template'",
            (
                plan.title, plan.category, start_min, end_min,
                end_min - start_min, now, plan_id, from_day,
            ),
        )
        changed = cur.rowcount or 0
    if changed:
        renumber(conn, from_day)
    return changed


def add_instance(
    conn: sqlite3.Connection,
    cfg,
    day: str,
    *,
    title: str,
    start_min: int,
    end_min: int,
    subject_id: int | None = None,
    category: str | None = None,
    priority: str = "normal",
    source: str = "manual",
    note: str | None = None,
) -> int:
    """수동으로(Slack `/plan`, 채팅 등) 인스턴스를 하나 만든다. 새 `plan_instance.id` 반환."""
    slot_minutes = cfg.rollup.slot_minutes
    start_min = _snap_to_slot(start_min, slot_minutes)
    end_min = _snap_to_slot(end_min, slot_minutes)
    _validate_time_range(start_min, end_min)
    if priority not in _VALID_PRIORITIES:
        raise ValueError(f"알 수 없는 우선순위입니다: {priority!r} ({'|'.join(sorted(_VALID_PRIORITIES))})")
    if source not in _VALID_SOURCES:
        raise ValueError(f"알 수 없는 출처입니다: {source!r} ({'|'.join(sorted(_VALID_SOURCES))})")

    now = timeutil.now_ts()
    with db.transaction(conn) as tx:
        cur = tx.execute(
            _INSERT_INSTANCE_SQL,
            (
                day, None, title, subject_id, category, start_min, end_min,
                "todo", priority, end_min - start_min, note, None, source,
                now, now,
            ),
        )
        instance_id = cur.lastrowid
    renumber(conn, day)
    return instance_id


def set_status(conn: sqlite3.Connection, cfg, instance_id: int, status: str, *, actor: str = "user") -> None:
    """인스턴스의 상태를 바꾼다. 모든 전환은 `plan_instance_event` 에 감사 로그로 남는다.

    `status='deferred'` 가 이월의 핵심이다: 다음 날짜의 새 인스턴스를 자동으로
    만들고(`source='carry'`), 새 인스턴스의 `carried_from` 에 이 인스턴스의 id 를
    넣는다. 그 체인의 깊이가 미루기의 정도이고 `v_carry_debt` 뷰가 2단계 이상을
    잡는다. 같은 인스턴스를 두 번 `deferred` 로 만들어도(재시도 등) 이미 이월된
    다음 인스턴스가 있으면 또 만들지 않는다(멱등).

    `cfg` 는 이 함수의 상태 전환 로직 자체에서는 쓰이지 않는다(다음 날짜는 달력
    산술만으로 정해진다) — 다만 이월 시 새 인스턴스의 시간 구간을 슬롯 경계에
    맞춰 재확인하는 데 `cfg.rollup.slot_minutes` 를 쓴다. 항상 `conn, cfg` 를
    인자로 받는다는 프로젝트 관례(계약서 §0)를 따른다.
    """
    if status not in _VALID_STATUSES:
        raise ValueError(f"알 수 없는 상태입니다: {status!r} ({'|'.join(sorted(_VALID_STATUSES))})")

    row = conn.execute("SELECT * FROM plan_instance WHERE id = ?", (instance_id,)).fetchone()
    if row is None:
        raise ValueError(f"계획 인스턴스를 찾을 수 없습니다: id={instance_id}")

    from_status = row["status"]
    now = timeutil.now_ts()
    next_day: str | None = None

    with db.transaction(conn) as tx:
        tx.execute(
            "UPDATE plan_instance SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, instance_id),
        )
        tx.execute(
            "INSERT INTO plan_instance_event(instance_id, at, from_status, to_status, actor) "
            "VALUES (?, ?, ?, ?, ?)",
            (instance_id, now, from_status, status, actor),
        )

        if status == "deferred":
            already = tx.execute(
                "SELECT id FROM plan_instance WHERE carried_from = ?", (instance_id,)
            ).fetchone()
            if already is None:
                next_day = _next_day(row["day"])
                slot_minutes = cfg.rollup.slot_minutes if cfg is not None else 10
                start_min = _snap_to_slot(int(row["start_min"]), slot_minutes)
                end_min = _snap_to_slot(int(row["end_min"]), slot_minutes)
                tx.execute(
                    _INSERT_INSTANCE_SQL,
                    (
                        next_day, None, row["title"], row["subject_id"], row["category"],
                        start_min, end_min, "todo", row["priority"], end_min - start_min,
                        row["note"], instance_id, "carry", now, now,
                    ),
                )

    if next_day is not None:
        renumber(conn, next_day)


def archive_instance(conn: sqlite3.Connection, instance_id: int) -> None:
    """소프트 삭제 — `archived_at` 을 채운다. `slot_breakdown` 등 실적 데이터는 건드리지 않는다."""
    now = timeutil.now_ts()
    with db.transaction(conn) as tx:
        tx.execute(
            "UPDATE plan_instance SET archived_at = ?, updated_at = ? WHERE id = ? AND archived_at IS NULL",
            (now, now, instance_id),
        )


def unarchive_instance(conn: sqlite3.Connection, instance_id: int) -> None:
    """소프트 삭제 실행취소 (Slack `/del` 의 15초 실행취소 버튼이 이걸 쓴다)."""
    now = timeutil.now_ts()
    with db.transaction(conn) as tx:
        tx.execute(
            "UPDATE plan_instance SET archived_at = NULL, updated_at = ? WHERE id = ?",
            (now, instance_id),
        )


def list_instances(
    conn: sqlite3.Connection, day: str, *, include_archived: bool = False, include_done: bool = True
) -> list[PlanInstanceRow]:
    """그 날짜의 인스턴스 목록. 기본은 삭제되지 않은 것 전부(완료 포함)."""
    clauses = ["day = ?"]
    params: list = [day]
    if not include_archived:
        clauses.append("archived_at IS NULL")
    if not include_done:
        clauses.append("status != 'done'")
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"SELECT * FROM plan_instance WHERE {where} ORDER BY ordinal ASC, id ASC", params
    ).fetchall()
    return [_row_to_instance(r) for r in rows]


def renumber(conn: sqlite3.Connection, day: str) -> None:
    """그 날짜의(삭제되지 않은) 인스턴스에 시작 시각 순으로 ordinal 1..N 을 다시 매긴다.

    `ordinal` 은 `/done 3`, `/del 3` 같은 슬래시 명령의 "3" 이자 목록 표시 순서다.
    """
    rows = conn.execute(
        "SELECT id FROM plan_instance WHERE day = ? AND archived_at IS NULL "
        "ORDER BY start_min ASC, id ASC",
        (day,),
    ).fetchall()
    now = timeutil.now_ts()
    with db.transaction(conn) as tx:
        for ordinal, row in enumerate(rows, start=1):
            tx.execute(
                "UPDATE plan_instance SET ordinal = ?, updated_at = ? WHERE id = ?",
                (ordinal, now, row["id"]),
            )


def carry_debt(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """`v_carry_debt` 뷰 그대로: 이월 체인이 2단계 이상인 (원본 인스턴스 id, 깊이) 목록."""
    rows = conn.execute("SELECT root, depth FROM v_carry_debt ORDER BY depth DESC, root ASC").fetchall()
    return [(int(r["root"]), int(r["depth"])) for r in rows]


# ─────────────────────────────────────────────────────────────
# 파서
# ─────────────────────────────────────────────────────────────

_TIME_RANGE_RE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


def parse_time_range(s: str) -> tuple[int, int]:
    """'09:00-12:00' -> (540, 720). 자정을 넘기거나 형식이 어긋나면 ValueError."""
    text = s.strip()
    m = _TIME_RANGE_RE.match(text)
    if not m:
        raise ValueError(f"시간 범위를 해석할 수 없습니다: {s!r} (형식: 'HH:MM-HH:MM')")

    h1, mi1, h2, mi2 = (int(g) for g in m.groups())
    if not (0 <= mi1 < 60 and 0 <= mi2 < 60):
        raise ValueError(f"시간 범위를 해석할 수 없습니다: {s!r}")
    if not (0 <= h1 <= 24 and 0 <= h2 <= 24):
        raise ValueError(f"시간 범위를 해석할 수 없습니다: {s!r}")
    if (h1 == 24 and mi1 != 0) or (h2 == 24 and mi2 != 0):
        raise ValueError(f"시간 범위를 해석할 수 없습니다: {s!r}")

    start_min = h1 * 60 + mi1
    end_min = h2 * 60 + mi2
    _validate_time_range(start_min, end_min)  # 자정을 넘기면(start >= end) 여기서 ValueError
    return start_min, end_min


# ISO 요일: 1=월 .. 7=일
_EN_DAY_MAP: dict[str, int] = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7}
_KO_DAY_MAP: dict[str, int] = {"월": 1, "화": 2, "수": 3, "목": 4, "금": 5, "토": 6, "일": 7}

_WEEKDAY_ALIASES: dict[str, str] = {
    "평일": "12345",
    "weekday": "12345",
    "weekdays": "12345",
    "주말": "67",
    "weekend": "67",
    "weekends": "67",
    "매일": "1234567",
    "daily": "1234567",
    "everyday": "1234567",
    "every day": "1234567",
}

_WEEKDAY_RANGE_RE = re.compile(r"^([1-7])\s*-\s*([1-7])$")


def _token_to_iso_day(token: str) -> int:
    """영문 약어('mon') 또는 한글 요일('월') 토큰 하나를 ISO 요일 숫자로."""
    lowered = token.lower()
    if lowered in _EN_DAY_MAP:
        return _EN_DAY_MAP[lowered]
    if token in _KO_DAY_MAP:
        return _KO_DAY_MAP[token]
    raise ValueError(f"요일 토큰을 해석할 수 없습니다: {token!r}")


def parse_weekdays(s: str) -> str:
    """다양한 형태의 요일 표현을 ISO 문자열('12345' 등, 1=월..7=일)로 정규화한다.

    받아들이는 형태: '평일'/'weekday' -> '12345', '주말'/'weekend' -> '67',
    '매일'/'daily' -> '1234567', 'mon,wed,fri' -> '135', '월수금' -> '135',
    '1-5' -> '12345'. 이미 ISO 문자열('135' 등)이 들어와도 그대로 정규화되어
    나오므로(멱등) create_plan/update_plan 이 항상 이 함수를 거쳐도 안전하다.
    실패하면 ValueError.
    """
    text = s.strip()
    if not text:
        raise ValueError("빈 요일 문자열입니다")

    lower = text.lower()
    if lower in _WEEKDAY_ALIASES:
        return _WEEKDAY_ALIASES[lower]

    range_m = _WEEKDAY_RANGE_RE.match(text)
    if range_m:
        a, b = int(range_m.group(1)), int(range_m.group(2))
        if a > b:
            raise ValueError(f"요일 범위가 거꾸로입니다: {s!r}")
        return "".join(str(d) for d in range(a, b + 1))

    # 콤마로 구분된 토큰 (영문 약어 또는 한글 요일 혼용 가능)
    tokens = [t.strip() for t in text.split(",") if t.strip()]
    if len(tokens) > 1:
        days = {_token_to_iso_day(t) for t in tokens}
        return "".join(str(d) for d in sorted(days))

    single = tokens[0] if tokens else text
    if single.lower() in _EN_DAY_MAP:
        return str(_EN_DAY_MAP[single.lower()])

    # 붙어 있는 한글 요일('월수금') 또는 붙어 있는 ISO 숫자('135')
    compact = "".join(ch for ch in text if not ch.isspace())
    if compact and all(ch in _KO_DAY_MAP for ch in compact):
        days = {_KO_DAY_MAP[ch] for ch in compact}
        return "".join(str(d) for d in sorted(days))
    if compact and all(ch in "1234567" for ch in compact):
        days = {int(ch) for ch in compact}
        return "".join(str(d) for d in sorted(days))

    raise ValueError(f"요일 문자열을 해석할 수 없습니다: {s!r}")
