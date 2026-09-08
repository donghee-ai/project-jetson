"""lifetrainer.report.timeline 테스트.

네트워크 없음. `slot` 에 직접 INSERT 해서 픽스처를 만든다 (rollup(B) 미의존).
이미지 픽셀 비교는 하지 않는다 — PNG 가 실제로 생성됐는지, 매직바이트가
맞는지, 죽지 않는지만 확인한다.
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from lifetrainer import db, timeutil
from lifetrainer.config import load_config
from lifetrainer.report import timeline as timeline_mod

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class FakeClassifier:
    """color/label/order 만 있는 최소 가짜 Classifier."""

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


def _insert_slot(conn, cfg, day: str, slot: int, category: str, *, active_sec: float = 0.0) -> None:
    start_ts, _ = timeutil.slot_bounds(day, slot, cfg.tz, cfg.rollup.slot_minutes)
    conn.execute(
        "INSERT INTO slot(day, slot, start_ts, category, active_sec, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (day, slot, start_ts, category, active_sec, time.time()),
    )


def _fill_day(conn, cfg, day: str) -> None:
    for s in range(0, 100):
        _insert_slot(conn, cfg, day, s, "off")
    for s in range(100, 110):
        _insert_slot(conn, cfg, day, s, "coding", active_sec=600.0)
    for s in range(110, 113):
        _insert_slot(conn, cfg, day, s, "research", active_sec=600.0)
    _insert_slot(conn, cfg, day, 113, "away")
    for s in range(114, 144):
        _insert_slot(conn, cfg, day, s, "off")


DAY = "2026-08-12"


# ── render_day ────────────────────────────────────────────────────────


def test_render_day_creates_nonempty_png(conn, cfg):
    _fill_day(conn, cfg, DAY)
    out = timeline_mod.render_day(conn, cfg, FakeClassifier(), DAY)

    assert out.exists()
    assert out.stat().st_size > 0
    assert out == cfg.report.png_dir / f"{DAY}-timeline.png"

    with open(out, "rb") as f:
        assert f.read(8) == _PNG_MAGIC


def test_render_day_respects_out_path(conn, cfg, tmp_path):
    _fill_day(conn, cfg, DAY)
    custom = tmp_path / "custom" / "day.png"
    out = timeline_mod.render_day(conn, cfg, FakeClassifier(), DAY, out_path=custom)

    assert out == custom
    assert custom.exists()
    assert custom.stat().st_size > 0


def test_render_day_empty_day_does_not_crash(conn, cfg):
    """롤업이 아예 안 된 날(슬롯 0개)도 죽지 않고 144칸(off)짜리 PNG 를 만든다."""
    out = timeline_mod.render_day(conn, cfg, FakeClassifier(), "2099-01-01")
    assert out.exists()
    assert out.stat().st_size > 0
    with open(out, "rb") as f:
        assert f.read(8) == _PNG_MAGIC


def test_render_day_partial_slots_does_not_crash(conn, cfg):
    """144개 슬롯이 다 채워지지 않은 날(예: 오늘, 진행 중)도 죽지 않는다."""
    _insert_slot(conn, cfg, DAY, 10, "coding", active_sec=600.0)
    out = timeline_mod.render_day(conn, cfg, FakeClassifier(), DAY)
    assert out.exists()
    assert out.stat().st_size > 0


# ── render_week ───────────────────────────────────────────────────────


def test_render_week_creates_nonempty_png(conn, cfg):
    for offset in range(7):
        day = timeutil.day_range("2026-08-10", "2026-08-16")[offset]
        _fill_day(conn, cfg, day)

    out = timeline_mod.render_week(conn, cfg, FakeClassifier(), "2026-08-16")

    assert out.exists()
    assert out.stat().st_size > 0
    with open(out, "rb") as f:
        assert f.read(8) == _PNG_MAGIC


def test_render_week_missing_days_does_not_crash(conn, cfg):
    """7일 중 일부만 데이터가 있어도(나머지는 롤업 전) 죽지 않는다."""
    _fill_day(conn, cfg, "2026-08-16")
    out = timeline_mod.render_week(conn, cfg, FakeClassifier(), "2026-08-16")
    assert out.exists()
    assert out.stat().st_size > 0


def test_render_week_respects_out_path(conn, cfg, tmp_path):
    _fill_day(conn, cfg, "2026-08-16")
    custom = tmp_path / "custom" / "week.png"
    out = timeline_mod.render_week(conn, cfg, FakeClassifier(), "2026-08-16", out_path=custom)
    assert out == custom
    assert custom.exists()
    assert custom.stat().st_size > 0


# ── 폰트 ──────────────────────────────────────────────────────────────


def test_resolve_font_picks_available_font(cfg):
    """폰트 폴백 체인이 실제로 설치된 폰트 이름을 반환한다 (두부 방지 확인)."""
    family = timeline_mod._resolve_font(cfg)
    assert family in {cfg.report.font_family, *timeline_mod._FONT_FALLBACKS, "DejaVu Sans"}
