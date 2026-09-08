"""lifetrainer.report.daily 테스트.

네트워크 없음. `slot`/`slot_breakdown` 에 직접 INSERT 해서 픽스처를 만든다
(rollup(B) 모듈에 의존하지 않는다 — test_stats.py/test_timeline.py 와 같은 패턴).
"""

from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest

from lifetrainer import db, timeutil
from lifetrainer.config import load_config
from lifetrainer.report import daily as daily_mod

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class FakeClassifier:
    """color/label/order 만 있는 최소 가짜 Classifier (rollup(B) 모듈 미의존)."""

    _LABELS = {
        "coding": "코딩",
        "research": "리서치",
        "browsing": "웹",
        "away": "자리비움",
        "off": "꺼짐",
    }
    _COLORS = {
        "coding": "#3b82f6",
        "research": "#8b5cf6",
        "browsing": "#94a3b8",
        "away": "#475569",
        "off": "#1e293b",
    }
    _ORDER = ["coding", "research", "browsing", "away", "off"]

    def label(self, category: str) -> str:
        return self._LABELS.get(category, category)

    def color(self, category: str) -> str:
        return self._COLORS.get(category, "#64748b")

    def order(self) -> list[str]:
        return list(self._ORDER)


@pytest.fixture()
def cfg(tmp_path):
    base = load_config()
    return replace(
        base,
        db_path=tmp_path / "lt.db",
        data_dir=tmp_path,
        report=replace(base.report, png_dir=tmp_path / "png"),
    )


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "lt.db")
    db.init_db(c)
    yield c
    c.close()


def _insert_slot(
    conn, cfg, day: str, slot: int, category: str, *, active_sec: float = 0.0, app: str = ""
) -> None:
    start_ts, _ = timeutil.slot_bounds(day, slot, cfg.tz, cfg.rollup.slot_minutes)
    conn.execute(
        "INSERT INTO slot(day, slot, start_ts, category, active_sec, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (day, slot, start_ts, category, active_sec, time.time()),
    )
    if active_sec > 0:
        conn.execute(
            "INSERT INTO slot_breakdown(day, slot, category, app, seconds) VALUES (?, ?, ?, ?, ?)",
            (day, slot, category, app, active_sec),
        )


def _fill_day(conn, cfg, day: str) -> None:
    for s in range(0, 100):
        _insert_slot(conn, cfg, day, s, "off")
    for s in range(100, 110):
        _insert_slot(conn, cfg, day, s, "coding", active_sec=600.0, app="Code.exe")
    for s in range(110, 113):
        _insert_slot(conn, cfg, day, s, "research", active_sec=600.0, app="chrome.exe")
    _insert_slot(conn, cfg, day, 113, "away")
    for s in range(114, 144):
        _insert_slot(conn, cfg, day, s, "off")


DAY = "2026-08-12"


# ── build_daily ───────────────────────────────────────────────────────


def test_build_daily_creates_report_row(conn, cfg):
    _fill_day(conn, cfg, DAY)
    built = daily_mod.build_daily(conn, cfg, FakeClassifier(), DAY)

    assert built.kind == "daily"
    assert built.day == DAY
    assert DAY in built.text
    assert built.blocks  # 비어있지 않음
    assert built.report_id > 0

    row = conn.execute("SELECT * FROM report WHERE id = ?", (built.report_id,)).fetchone()
    assert row is not None
    assert row["kind"] == "daily"
    assert row["day"] == DAY
    assert row["text"] == built.text
    assert json.loads(row["blocks_json"]) == built.blocks


def test_build_daily_generates_nonempty_png(conn, cfg):
    _fill_day(conn, cfg, DAY)
    built = daily_mod.build_daily(conn, cfg, FakeClassifier(), DAY)

    assert built.png_path is not None
    assert built.png_path.exists()
    assert built.png_path.stat().st_size > 0
    with open(built.png_path, "rb") as f:
        assert f.read(8) == _PNG_MAGIC

    row = conn.execute("SELECT png_path FROM report WHERE id = ?", (built.report_id,)).fetchone()
    assert row["png_path"] == str(built.png_path)


def test_build_daily_upsert_is_idempotent(conn, cfg):
    """같은 (kind, day) 를 두 번 빌드해도 report 행이 하나만 있어야 한다 (덮어쓰기)."""
    _fill_day(conn, cfg, DAY)
    first = daily_mod.build_daily(conn, cfg, FakeClassifier(), DAY)
    second = daily_mod.build_daily(conn, cfg, FakeClassifier(), DAY)

    assert first.report_id == second.report_id
    count = conn.execute("SELECT COUNT(*) FROM report WHERE kind='daily' AND day=?", (DAY,)).fetchone()[0]
    assert count == 1


def test_build_daily_empty_day_does_not_crash(conn, cfg):
    """롤업이 안 된 날도 죽지 않고 빈 통계로 리포트를 만든다."""
    built = daily_mod.build_daily(conn, cfg, FakeClassifier(), "2099-01-01")
    assert built.report_id > 0
    assert built.text
    assert built.png_path is not None and built.png_path.exists()


# ── build_weekly ──────────────────────────────────────────────────────


def test_build_weekly_creates_report_row(conn, cfg):
    for day in timeutil.day_range("2026-08-10", "2026-08-16"):
        _fill_day(conn, cfg, day)

    built = daily_mod.build_weekly(conn, cfg, FakeClassifier(), "2026-08-16")

    assert built.kind == "weekly"
    assert built.day == "2026-08-16"
    assert built.text
    assert built.blocks
    assert built.report_id > 0

    row = conn.execute("SELECT * FROM report WHERE id = ?", (built.report_id,)).fetchone()
    assert row["kind"] == "weekly"
    assert row["day"] == "2026-08-16"


def test_build_weekly_generates_nonempty_png(conn, cfg):
    for day in timeutil.day_range("2026-08-10", "2026-08-16"):
        _fill_day(conn, cfg, day)

    built = daily_mod.build_weekly(conn, cfg, FakeClassifier(), "2026-08-16")

    assert built.png_path is not None
    assert built.png_path.exists()
    assert built.png_path.stat().st_size > 0
    with open(built.png_path, "rb") as f:
        assert f.read(8) == _PNG_MAGIC


def test_build_weekly_upsert_is_idempotent(conn, cfg):
    for day in timeutil.day_range("2026-08-10", "2026-08-16"):
        _fill_day(conn, cfg, day)

    first = daily_mod.build_weekly(conn, cfg, FakeClassifier(), "2026-08-16")
    second = daily_mod.build_weekly(conn, cfg, FakeClassifier(), "2026-08-16")

    assert first.report_id == second.report_id
    count = conn.execute(
        "SELECT COUNT(*) FROM report WHERE kind='weekly' AND day=?", ("2026-08-16",)
    ).fetchone()[0]
    assert count == 1


def test_daily_and_weekly_reports_do_not_collide(conn, cfg):
    """(kind, day) 가 UNIQUE 라 같은 day 라도 kind 가 다르면 별개 행이어야 한다."""
    _fill_day(conn, cfg, DAY)
    daily_built = daily_mod.build_daily(conn, cfg, FakeClassifier(), DAY)
    weekly_built = daily_mod.build_weekly(conn, cfg, FakeClassifier(), DAY)

    assert daily_built.report_id != weekly_built.report_id
    count = conn.execute("SELECT COUNT(*) FROM report WHERE day = ?", (DAY,)).fetchone()[0]
    assert count == 2


def test_build_daily_png_failure_does_not_break_report(conn, cfg, monkeypatch):
    """PNG 렌더가 실패해도 텍스트/블록은 살아남고 report 행은 만들어져야 한다."""
    _fill_day(conn, cfg, DAY)

    def _boom(*args, **kwargs):
        raise RuntimeError("가짜 렌더 실패")

    monkeypatch.setattr(daily_mod.timeline_mod, "render_day", _boom)

    built = daily_mod.build_daily(conn, cfg, FakeClassifier(), DAY)
    assert built.png_path is None
    assert built.text
    assert built.blocks
    row = conn.execute("SELECT png_path FROM report WHERE id = ?", (built.report_id,)).fetchone()
    assert row["png_path"] is None
