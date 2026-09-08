"""lifetrainer.plan.override 테스트 + rollup.py 와의 연결부.

가장 중요한 두 가지(계약서 §3 말미)는 여기서 확인한다:
  1. 롤업을 다시 돌려도 오버라이드가 살아남는가.
  2. 오버라이드가 slot_breakdown(실측 원본)을 바꾸지 않는가.

픽스처는 aw_event/aw_bucket 에 직접 INSERT 해서 만든다 (test_rollup.py 와 같은 패턴).
네트워크 호출 없음.
"""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path

import pytest

from lifetrainer import db, timeutil
from lifetrainer.config import load_config
from lifetrainer.plan.override import (
    apply_overrides,
    clear_override,
    clear_override_range,
    list_overrides,
    set_override,
    set_override_range,
)
from lifetrainer.rollup.classify import Classifier
from lifetrainer.rollup.rollup import rollup_day

RULES_PATH = Path(__file__).resolve().parent.parent / "config" / "rules.yaml"
DAY = "2026-01-05"


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


@pytest.fixture(scope="module")
def classifier() -> Classifier:
    return Classifier.from_yaml(RULES_PATH)


def _insert_bucket(conn, bucket_id: str, btype: str) -> None:
    conn.execute(
        "INSERT INTO aw_bucket(bucket_id, host, client, type, hostname, first_seen, last_seen) "
        "VALUES (?, 'test-host', ?, ?, 'test-host', 0, 0)",
        (bucket_id, btype, btype),
    )


def _insert_event(
    conn, bucket_id: str, ts: float, ts_end: float, *, app=None, title=None, url=None, status=None
) -> None:
    conn.execute(
        """
        INSERT INTO aw_event(bucket_id, ts, ts_end, duration, event_id, app, title, url, status, data_json, synced_at)
        VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, '{}', ?)
        """,
        (bucket_id, ts, ts_end, ts_end - ts, app, title, url, status, time.time()),
    )


def _setup_buckets(conn) -> None:
    _insert_bucket(conn, "window_test", "window")
    _insert_bucket(conn, "afk_test", "afk")
    _insert_bucket(conn, "web_test", "web")


def _slot_row(conn, day: str, slot: int):
    return conn.execute("SELECT * FROM slot WHERE day = ? AND slot = ?", (day, slot)).fetchone()


def _breakdown_rows(conn, day: str, slot: int):
    return conn.execute(
        "SELECT category, app, seconds FROM slot_breakdown WHERE day = ? AND slot = ? ORDER BY category, app",
        (day, slot),
    ).fetchall()


def _make_coding_slot(conn, cfg, classifier, day: str, slot: int) -> None:
    """afk=not-afk + Code.exe 창으로 슬롯 하나를 채워 롤업하면 'coding'이 되게 한다."""
    _setup_buckets(conn)
    tz = cfg.tz
    s, e = timeutil.slot_bounds(day, slot, tz, cfg.rollup.slot_minutes)
    _insert_event(conn, "afk_test", s, e, status="not-afk")
    _insert_event(conn, "window_test", s, e, app="Code.exe", title="main.py")


# ── 핵심 1: 롤업을 다시 돌려도 오버라이드가 살아남는가 ─────────────────────


def test_override_survives_repeated_rollup(conn, cfg, classifier):
    slot = 50
    _make_coding_slot(conn, cfg, classifier, DAY, slot)

    rollup_day(conn, cfg, classifier, DAY)
    before = _slot_row(conn, DAY, slot)
    assert before["category"] == "coding"
    assert before["source"] == "rule"

    set_override(conn, DAY, slot, "ops", note="사실 회의였음", actor="web")

    rollup_day(conn, cfg, classifier, DAY)  # 재롤업 1회차
    after1 = _slot_row(conn, DAY, slot)
    assert after1["category"] == "ops"
    assert after1["source"] == "override"
    assert after1["confidence"] == pytest.approx(1.0)

    rollup_day(conn, cfg, classifier, DAY)  # 재롤업 2회차 — 여기서도 살아남아야 한다
    after2 = _slot_row(conn, DAY, slot)
    assert after2["category"] == "ops"
    assert after2["source"] == "override"

    # slot_override 테이블 자체는 절대 지워지면 안 된다 (사람의 입력).
    row = conn.execute(
        "SELECT * FROM slot_override WHERE day = ? AND slot = ?", (DAY, slot)
    ).fetchone()
    assert row is not None
    assert row["category"] == "ops"
    assert row["note"] == "사실 회의였음"


# ── 핵심 2: 오버라이드가 slot_breakdown 을 바꾸지 않는가 ───────────────────


def test_override_does_not_touch_slot_breakdown(conn, cfg, classifier):
    slot = 60
    _make_coding_slot(conn, cfg, classifier, DAY, slot)

    rollup_day(conn, cfg, classifier, DAY)
    breakdown_before = _breakdown_rows(conn, DAY, slot)
    assert breakdown_before  # coding 이 실제로 잡혔는지 확인

    set_override(conn, DAY, slot, "ops")
    rollup_day(conn, cfg, classifier, DAY)
    breakdown_after = _breakdown_rows(conn, DAY, slot)

    # 집계 원본(slot_breakdown)은 오버라이드 전후로 완전히 동일해야 한다.
    assert [tuple(r) for r in breakdown_before] == [tuple(r) for r in breakdown_after]
    assert breakdown_after[0]["category"] == "coding"  # 실측은 여전히 coding

    # 격자 표시(slot.category)만 바뀐 상태.
    row = _slot_row(conn, DAY, slot)
    assert row["category"] == "ops"


# ── apply_overrides 단독 동작 ────────────────────────────────────────────


def test_apply_overrides_returns_touched_row_count(conn, cfg, classifier):
    slot = 70
    _make_coding_slot(conn, cfg, classifier, DAY, slot)
    rollup_day(conn, cfg, classifier, DAY)

    set_override(conn, DAY, slot, "ops")
    touched = apply_overrides(conn, DAY)
    assert touched == 1

    # 아직 롤업된 적 없는 날짜(slot 행 자체가 없음)는 UPDATE 대상이 없어 0.
    set_override(conn, "2099-01-01", 0, "ops")
    assert apply_overrides(conn, "2099-01-01") == 0


def test_apply_overrides_noop_when_no_overrides(conn, cfg, classifier):
    slot = 71
    _make_coding_slot(conn, cfg, classifier, DAY, slot)
    rollup_day(conn, cfg, classifier, DAY)
    assert apply_overrides(conn, DAY) == 0  # 오버라이드가 없으면 아무 것도 안 건드림


# ── set/clear/list override (rollup 과 무관하게 단독 동작) ────────────────


def test_set_override_upserts_same_slot(conn):
    set_override(conn, DAY, 5, "ops", note="첫 기록")
    set_override(conn, DAY, 5, "learning", note="정정")

    overrides = list_overrides(conn, DAY)
    assert overrides[5]["category"] == "learning"
    assert overrides[5]["note"] == "정정"


def test_set_override_range_and_list(conn):
    n = set_override_range(conn, DAY, 10, 13, "ops", actor="cli")
    assert n == 3

    overrides = list_overrides(conn, DAY)
    assert set(overrides.keys()) == {10, 11, 12}
    assert all(v["category"] == "ops" and v["actor"] == "cli" for v in overrides.values())


def test_set_override_range_rejects_empty_range(conn):
    with pytest.raises(ValueError):
        set_override_range(conn, DAY, 10, 10, "ops")


def test_clear_override_single_and_range(conn):
    set_override(conn, DAY, 5, "ops")
    set_override_range(conn, DAY, 20, 23, "learning")

    clear_override(conn, DAY, 5)
    assert 5 not in list_overrides(conn, DAY)

    removed = clear_override_range(conn, DAY, 20, 23)
    assert removed == 3
    assert list_overrides(conn, DAY) == {}


def test_list_overrides_empty_day_returns_empty_dict(conn):
    assert list_overrides(conn, "2099-12-31") == {}
