"""lifetrainer.plan.subjects 테스트 (계약서 §3, v2).

색 검증은 `config/palette.yaml` 을 실제로 읽어서 한다(하드코딩된 hex 목록을
따로 두지 않는다 — `report/palette.py` 가 유일한 원본이라는 원칙을 테스트에서도
지킨다). 네트워크 호출 없음.
"""

from __future__ import annotations

import dataclasses

import pytest

from lifetrainer import db
from lifetrainer.config import load_config
from lifetrainer.plan.subjects import (
    create_subject,
    delete_subject,
    list_subjects,
    palette_choices,
    update_subject,
)


@pytest.fixture()
def cfg(tmp_path):
    base = load_config()
    return dataclasses.replace(base, db_path=tmp_path / "lt.db")


@pytest.fixture()
def conn(cfg):
    c = db.connect(cfg.db_path)
    db.init_db(c)
    yield c
    c.close()


# ── palette_choices ──────────────────────────────────────────────────────


def test_palette_choices_returns_9_slots_in_fixed_order(cfg):
    choices = palette_choices(cfg)
    assert len(choices) == 9
    assert [c["id"] for c in choices] == [
        "coding", "research", "writing", "sns", "learning", "ops", "browsing",
        "entertainment", "gaming",
    ]
    for c in choices:
        assert set(c) == {"id", "hex", "label"}
        assert c["hex"].startswith("#")
        assert c["label"]  # 한글 라벨이 비어있지 않다


# ── color 검증 ────────────────────────────────────────────────────────────


def test_create_subject_accepts_validated_palette_color(conn, cfg):
    coding_hex = palette_choices(cfg)[0]["hex"]
    subject_id = create_subject(conn, cfg, name="시험공부", color=coding_hex, category="coding")
    assert isinstance(subject_id, int)

    [row] = list_subjects(conn)
    assert row["name"] == "시험공부"
    assert row["color"] == coding_hex
    assert row["category"] == "coding"
    assert row["archived"] is False


def test_create_subject_rejects_arbitrary_hex(conn, cfg):
    with pytest.raises(ValueError):
        create_subject(conn, cfg, name="아무거나", color="#123456")


def test_create_subject_rejects_off_palette_but_plausible_hex(conn, cfg):
    """팔레트 안에 있는 hex 를 살짝 바꾼(대소문자 제외) 값도 거부해야 한다."""
    with pytest.raises(ValueError):
        create_subject(conn, cfg, name="아무거나", color="#2a78d7")  # coding(#2a78d6) 에서 한 끗 다름


def test_create_subject_color_is_case_insensitive(conn, cfg):
    coding_hex = palette_choices(cfg)[0]["hex"]
    subject_id = create_subject(conn, cfg, name="대문자색", color=coding_hex.upper())
    assert isinstance(subject_id, int)


def test_update_subject_revalidates_color(conn, cfg):
    coding_hex = palette_choices(cfg)[0]["hex"]
    subject_id = create_subject(conn, cfg, name="이름", color=coding_hex)

    with pytest.raises(ValueError):
        update_subject(conn, cfg, subject_id, color="#000000")

    research_hex = palette_choices(cfg)[1]["hex"]
    update_subject(conn, cfg, subject_id, color=research_hex)
    [row] = list_subjects(conn)
    assert row["color"] == research_hex


# ── CRUD 왕복 ─────────────────────────────────────────────────────────────


def test_create_duplicate_name_raises(conn, cfg):
    coding_hex = palette_choices(cfg)[0]["hex"]
    create_subject(conn, cfg, name="중복", color=coding_hex)
    with pytest.raises(ValueError):
        create_subject(conn, cfg, name="중복", color=coding_hex)


def test_update_subject_partial_fields(conn, cfg):
    coding_hex = palette_choices(cfg)[0]["hex"]
    subject_id = create_subject(conn, cfg, name="원래이름", color=coding_hex, ordinal=0)

    update_subject(conn, cfg, subject_id, name="새이름", ordinal=5)
    [row] = list_subjects(conn)
    assert row["name"] == "새이름"
    assert row["ordinal"] == 5
    assert row["color"] == coding_hex  # 안 건드린 필드는 그대로


def test_update_subject_unknown_field_raises(conn, cfg):
    coding_hex = palette_choices(cfg)[0]["hex"]
    subject_id = create_subject(conn, cfg, name="이름", color=coding_hex)
    with pytest.raises(ValueError):
        update_subject(conn, cfg, subject_id, unknown_field="x")


def test_delete_subject_is_soft_delete(conn, cfg):
    coding_hex = palette_choices(cfg)[0]["hex"]
    subject_id = create_subject(conn, cfg, name="지울과목", color=coding_hex)

    delete_subject(conn, subject_id)

    assert list_subjects(conn) == []
    archived = list_subjects(conn, include_archived=True)
    assert len(archived) == 1
    assert archived[0]["archived"] is True
    assert archived[0]["id"] == subject_id  # 실제로 지워지지 않고 남아 있다


def test_list_subjects_ordered_by_ordinal(conn, cfg):
    coding_hex = palette_choices(cfg)[0]["hex"]
    research_hex = palette_choices(cfg)[1]["hex"]
    create_subject(conn, cfg, name="둘째", color=research_hex, ordinal=2)
    create_subject(conn, cfg, name="첫째", color=coding_hex, ordinal=1)

    names = [s["name"] for s in list_subjects(conn)]
    assert names == ["첫째", "둘째"]
