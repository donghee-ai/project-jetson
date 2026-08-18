"""사용자 정의 '과목'(subject) CRUD (계약서 §3, v2).

고정 카테고리(규칙 기반 자동 분류, 예: coding/research)와는 다른 축이다.
'코딩'은 기계가 계측하고, '논문 3장 쓰기' 같은 subject 는 사람이 정의한다.
`subject.category` 가 있으면 그 subject 를 참조하는 계획 인스턴스의 달성률
계산에 그 카테고리의 실측 시간을 쓴다(`plan.achieve.plans_for_day` 의 상속 규칙).

★ `color` 는 `config/palette.yaml` 의 검증된 8슬롯 hex 중 하나여야 한다.
임의 hex 를 허용하면 색각 분리가 깨진다(이 프로젝트가 실제로 겪은 문제 —
`report/palette.py` 모듈독스트링, `config/palette.yaml` 상단 주석 참고).
색은 이 모듈이 만들어내지 않는다 — 전부 `report.palette.load_palette` 를
거쳐 읽어온다.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from lifetrainer import db, timeutil
from lifetrainer.report.palette import load_palette

_UPDATABLE_FIELDS = frozenset({"name", "color", "category", "ordinal", "archived"})

# subject.color 검증에 쓰는 팔레트 테마. 웹/PNG 렌더러 대부분의 기본값(light)과
# 맞춘다 — "8슬롯 hex" 는 이 한 테마의 8개를 가리킨다(계약서 §3).
_VALIDATION_THEME = "light"


def _palette_path(cfg) -> Path:
    return cfg.root / "config" / "palette.yaml"


def _valid_colors(cfg) -> dict[str, str]:
    """{hex(소문자): category_id} — 검증된 8슬롯. `load_palette` 가 유일한 원본이다."""
    pal = load_palette(_palette_path(cfg), _VALIDATION_THEME)
    return {hexval.lower(): cat_id for cat_id, hexval in pal.categories.items()}


def _validate_color(cfg, color: str) -> None:
    if not isinstance(color, str) or color.lower() not in _valid_colors(cfg):
        raise ValueError(
            f"허용되지 않은 색상입니다: {color!r} "
            f"(config/palette.yaml 의 검증된 8슬롯 hex 중 하나여야 합니다 — palette_choices() 참고)"
        )


def _row_to_subject(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "color": row["color"],
        "category": row["category"],
        "ordinal": int(row["ordinal"]),
        "archived": bool(row["archived"]),
        "created_at": row["created_at"],
    }


def create_subject(
    conn: sqlite3.Connection, cfg, *, name: str, color: str, category: str | None = None, ordinal: int = 0
) -> int:
    """subject 를 만든다. 성공하면 새 `subject.id` 를 반환한다."""
    _validate_color(cfg, color)
    now = timeutil.now_ts()
    try:
        with db.transaction(conn) as tx:
            cur = tx.execute(
                "INSERT INTO subject(name, color, category, ordinal, archived, created_at) "
                "VALUES (?, ?, ?, ?, 0, ?)",
                (name, color, category, ordinal, now),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"이미 있는 subject 이름입니다: {name!r}") from exc


def update_subject(conn: sqlite3.Connection, cfg, subject_id: int, **fields) -> None:
    """지정된 필드만 부분 갱신한다. `color` 를 바꾸면 다시 팔레트 검증을 거친다."""
    if not fields:
        return

    unknown = set(fields) - _UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"알 수 없는 필드입니다: {sorted(unknown)}")

    to_write = dict(fields)
    if "color" in to_write:
        _validate_color(cfg, to_write["color"])
    if "archived" in to_write:
        to_write["archived"] = 1 if to_write["archived"] else 0

    set_clause = ", ".join(f"{k} = ?" for k in to_write)
    values = list(to_write.values())
    try:
        with db.transaction(conn) as tx:
            cur = tx.execute(f"UPDATE subject SET {set_clause} WHERE id = ?", (*values, subject_id))
            if cur.rowcount == 0:
                raise ValueError(f"subject 를 찾을 수 없습니다: id={subject_id}")
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"이미 있는 subject 이름입니다: {to_write.get('name')!r}") from exc


def delete_subject(conn: sqlite3.Connection, subject_id: int) -> None:
    """소프트 삭제(`archived=1`). 이 subject 를 참조하는 계획 인스턴스는 그대로 남는다."""
    with db.transaction(conn) as tx:
        tx.execute("UPDATE subject SET archived = 1 WHERE id = ?", (subject_id,))


def list_subjects(conn: sqlite3.Connection, *, include_archived: bool = False) -> list[dict]:
    """subject 목록을 `ordinal, id` 순으로. 기본은 삭제되지 않은 것만."""
    if include_archived:
        rows = conn.execute("SELECT * FROM subject ORDER BY ordinal, id").fetchall()
    else:
        rows = conn.execute("SELECT * FROM subject WHERE archived = 0 ORDER BY ordinal, id").fetchall()
    return [_row_to_subject(r) for r in rows]


def palette_choices(cfg) -> list[dict]:
    """subject 색상 선택기가 쓸 검증된 8슬롯 목록: `[{id, hex, label}]` (슬롯 고정 순서)."""
    pal = load_palette(_palette_path(cfg), _VALIDATION_THEME)
    return [{"id": cat_id, "hex": pal.categories[cat_id], "label": pal.labels[cat_id]} for cat_id in pal.order]
