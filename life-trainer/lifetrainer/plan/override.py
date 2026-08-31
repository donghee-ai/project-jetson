"""수동 보정(override) — 격자에서 사람이 직접 고친 칸 (계약서 §3).

`slot_override` 는 사람이 넣은 입력이라 롤업이 지우지 않는다. 롤업의 마지막
단계(`apply_overrides`)가 이 값을 `slot.category` 에 덮어써 `source='override'`,
`confidence=1.0` 으로 표시한다. `slot_breakdown`(실측 원본)은 절대 건드리지
않는다 — 그래서 오버라이드는 격자 표시만 바꾸고 "이번 주 코딩 몇 시간" 같은
집계 숫자는 바꾸지 않는다. 이게 의도된 동작이다.
"""

from __future__ import annotations

import sqlite3

from lifetrainer import db, timeutil

_UPSERT_SQL = (
    "INSERT INTO slot_override(day, slot, category, note, actor, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(day, slot) DO UPDATE SET "
    "category = excluded.category, note = excluded.note, "
    "actor = excluded.actor, created_at = excluded.created_at"
)


def set_override(
    conn: sqlite3.Connection, day: str, slot: int, category: str, *, note: str | None = None, actor: str = "web"
) -> None:
    """칸 하나를 수동으로 고친다. 이미 그 칸에 오버라이드가 있으면 덮어쓴다."""
    now = timeutil.now_ts()
    with db.transaction(conn) as tx:
        tx.execute(_UPSERT_SQL, (day, slot, category, note, actor, now))


def set_override_range(
    conn: sqlite3.Connection, day: str, start_slot: int, end_slot: int, category: str, *, actor: str = "web"
) -> int:
    """[start_slot, end_slot) 범위 전체를 한 카테고리로 고친다. 건드린 칸 수를 반환한다."""
    if end_slot <= start_slot:
        raise ValueError(f"잘못된 슬롯 범위입니다: start_slot={start_slot}, end_slot={end_slot}")
    now = timeutil.now_ts()
    rows = [(day, s, category, None, actor, now) for s in range(start_slot, end_slot)]
    with db.transaction(conn) as tx:
        tx.executemany(_UPSERT_SQL, rows)
    return len(rows)


def clear_override(conn: sqlite3.Connection, day: str, slot: int) -> None:
    """칸 하나의 오버라이드를 해제한다 (다음 롤업부터는 다시 자동 분류값을 쓴다)."""
    with db.transaction(conn) as tx:
        tx.execute("DELETE FROM slot_override WHERE day = ? AND slot = ?", (day, slot))


def clear_override_range(conn: sqlite3.Connection, day: str, start_slot: int, end_slot: int) -> int:
    """[start_slot, end_slot) 범위의 오버라이드를 전부 해제한다. 지운 행 수를 반환한다."""
    with db.transaction(conn) as tx:
        cur = tx.execute(
            "DELETE FROM slot_override WHERE day = ? AND slot >= ? AND slot < ?",
            (day, start_slot, end_slot),
        )
        return cur.rowcount


def list_overrides(conn: sqlite3.Connection, day: str) -> dict[int, dict]:
    """그 날짜의 오버라이드를 `{slot: {category, note, actor, created_at}}` 로."""
    rows = conn.execute(
        "SELECT slot, category, note, actor, created_at FROM slot_override WHERE day = ?",
        (day,),
    ).fetchall()
    return {
        int(r["slot"]): {
            "category": r["category"],
            "note": r["note"],
            "actor": r["actor"],
            "created_at": r["created_at"],
        }
        for r in rows
    }


def apply_overrides(conn: sqlite3.Connection, day: str) -> int:
    """`slot_override` 를 `slot.category` 에 반영한다. 건드린 행 수를 반환한다.

    `source='override'`, `confidence=1.0` 으로 표시하고 `slot_breakdown` 은
    건드리지 않는다 (실측 원본 보존 — 집계 숫자는 바뀌지 않아야 한다).
    `rollup_day` 의 맨 마지막에서 호출된다. `slot_override` 자체는 절대 지우지
    않는다 (사람이 넣은 입력이므로 롤업 재실행과 무관하게 살아남아야 한다).
    """
    now = timeutil.now_ts()
    with db.transaction(conn) as tx:
        rows = tx.execute(
            "SELECT slot, category FROM slot_override WHERE day = ?", (day,)
        ).fetchall()
        touched = 0
        for row in rows:
            cur = tx.execute(
                "UPDATE slot SET category = ?, source = 'override', confidence = 1.0, updated_at = ? "
                "WHERE day = ? AND slot = ?",
                (row["category"], now, day, row["slot"]),
            )
            touched += cur.rowcount
    return touched
