"""lifetrainer.report.stats 테스트.

네트워크 없음. `slot`/`slot_breakdown` 에 직접 INSERT 해서 픽스처를 만든다
(rollup(B) 모듈에 의존하지 않는다). classifier 가 필요한 render_*_text 테스트는
color/label/order 만 있는 최소 가짜 객체를 쓴다.
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from lifetrainer import db, timeutil
from lifetrainer.config import load_config
from lifetrainer.report import stats as stats_mod


# ── 픽스처 ────────────────────────────────────────────────────────────


@pytest.fixture()
def cfg(tmp_path):
    base = load_config()
    # db_path/png_dir 는 이 테스트에서 쓰지 않지만, 실수로 실 DB를 건드리지 않도록 tmp_path 로 돌린다.
    return replace(base, db_path=tmp_path / "lt.db", data_dir=tmp_path)


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "lt.db")
    db.init_db(c)
    yield c
    c.close()


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


def _insert_slot(
    conn,
    cfg,
    day: str,
    slot: int,
    category: str,
    *,
    active_sec: float = 0.0,
    afk_sec: float = 0.0,
) -> None:
    start_ts, _ = timeutil.slot_bounds(day, slot, cfg.tz, cfg.rollup.slot_minutes)
    conn.execute(
        "INSERT INTO slot(day, slot, start_ts, category, active_sec, afk_sec, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (day, slot, start_ts, category, active_sec, afk_sec, time.time()),
    )


def _insert_breakdown(conn, day: str, slot: int, category: str, app: str, seconds: float) -> None:
    conn.execute(
        "INSERT INTO slot_breakdown(day, slot, category, app, seconds) VALUES (?, ?, ?, ?, ?)",
        (day, slot, category, app, seconds),
    )


def _fill_day(conn, cfg, day: str) -> None:
    """144슬롯 하루를 채운다.

    - 슬롯 0..99, 114..143: off (활동 없음)
    - 슬롯 100..109 (10칸): coding, Code.exe, 슬롯당 600초
    - 슬롯 110..112 (3칸):  research, chrome.exe, 슬롯당 600초
    - 슬롯 113 (1칸):       away (자리비움)
    """
    slot_sec = cfg.rollup.slot_minutes * 60
    for s in range(0, 100):
        _insert_slot(conn, cfg, day, s, "off")
    for s in range(100, 110):
        _insert_slot(conn, cfg, day, s, "coding", active_sec=slot_sec)
        _insert_breakdown(conn, day, s, "coding", "Code.exe", slot_sec)
    for s in range(110, 113):
        _insert_slot(conn, cfg, day, s, "research", active_sec=slot_sec)
        _insert_breakdown(conn, day, s, "research", "chrome.exe", slot_sec)
    _insert_slot(conn, cfg, day, 113, "away", afk_sec=slot_sec)
    for s in range(114, 144):
        _insert_slot(conn, cfg, day, s, "off")


DAY = "2026-08-12"


# ── compute_daily ─────────────────────────────────────────────────────


def test_by_category_matches_sql_and_excludes_away_off(conn, cfg):
    _fill_day(conn, cfg, DAY)
    result = stats_mod.compute_daily(conn, cfg, DAY)

    # SQL 로 직접 재계산해서 대조한다 (파이썬 재순회로 나온 값이 아님을 검증).
    raw = dict(
        conn.execute(
            "SELECT category, SUM(seconds) AS sec FROM slot_breakdown "
            "WHERE day = ? GROUP BY category",
            (DAY,),
        ).fetchall()
    )
    assert raw == {"coding": 6000.0, "research": 1800.0}

    by_cat = {c.category: c.seconds for c in result.by_category}
    assert by_cat == {"coding": 6000.0, "research": 1800.0}
    # away/off 는 활동 카테고리가 아니므로 by_category 에서 제외된다.
    assert "away" not in by_cat and "off" not in by_cat


def test_by_category_share_sums_to_one(conn, cfg):
    _fill_day(conn, cfg, DAY)
    result = stats_mod.compute_daily(conn, cfg, DAY)
    assert sum(c.share for c in result.by_category) == pytest.approx(1.0)
    # 내림차순 정렬 확인
    seconds = [c.seconds for c in result.by_category]
    assert seconds == sorted(seconds, reverse=True)


def test_active_afk_off_and_coverage(conn, cfg):
    _fill_day(conn, cfg, DAY)
    result = stats_mod.compute_daily(conn, cfg, DAY)

    assert result.active_sec == pytest.approx(7800.0)  # 10+3 슬롯 * 600초
    assert result.afk_sec == pytest.approx(600.0)
    # off 슬롯 130개 * 600초
    assert result.off_sec == pytest.approx(130 * 600.0)
    assert result.coverage == pytest.approx((144 - 130) / 144)


def test_first_last_activity_and_span(conn, cfg):
    _fill_day(conn, cfg, DAY)
    result = stats_mod.compute_daily(conn, cfg, DAY)

    expect_first, _ = timeutil.slot_bounds(DAY, 100, cfg.tz, cfg.rollup.slot_minutes)
    expect_last_start, expect_last_end = timeutil.slot_bounds(DAY, 112, cfg.tz, cfg.rollup.slot_minutes)

    assert result.first_activity_ts == pytest.approx(expect_first)
    assert result.last_activity_ts == pytest.approx(expect_last_end)
    assert result.total_span_sec == pytest.approx(expect_last_end - expect_first)


def test_longest_focus_excludes_away_off(conn, cfg):
    _fill_day(conn, cfg, DAY)
    result = stats_mod.compute_daily(conn, cfg, DAY)
    assert result.longest_focus == ("coding", 100, 10)


def test_top_apps(conn, cfg):
    _fill_day(conn, cfg, DAY)
    result = stats_mod.compute_daily(conn, cfg, DAY)
    apps = dict(result.top_apps)
    assert apps["Code.exe"] == pytest.approx(6000.0)
    assert apps["chrome.exe"] == pytest.approx(1800.0)
    # 내림차순
    assert result.top_apps[0][0] == "Code.exe"


def test_slot_categories_length_and_gap_fill(conn, cfg):
    _fill_day(conn, cfg, DAY)
    result = stats_mod.compute_daily(conn, cfg, DAY)
    assert len(result.slot_categories) == 144
    assert result.slot_categories[100] == "coding"
    assert result.slot_categories[113] == "away"
    assert result.slot_categories[0] == "off"


def test_compute_daily_empty_day_returns_zeroed_stats(conn, cfg):
    """slot/slot_breakdown 에 아무 행도 없는 날 -> 예외 없이 전부 0으로 채운 통계."""
    result = stats_mod.compute_daily(conn, cfg, "2099-01-01")

    assert result.day == "2099-01-01"
    assert result.total_span_sec == 0.0
    assert result.active_sec == 0.0
    assert result.afk_sec == 0.0
    assert result.off_sec == 0.0
    assert result.coverage == 0.0
    assert result.by_category == []
    assert result.top_apps == []
    assert result.first_activity_ts is None
    assert result.last_activity_ts is None
    assert result.longest_focus is None
    assert len(result.slot_categories) == 144
    assert all(c == cfg.rollup.no_data_category for c in result.slot_categories)


def test_slot_gap_filled_with_off_when_partial_day(conn, cfg):
    """하루의 일부 슬롯만 롤업되어 있어도(예: 오늘, 아직 안 지난 시간) 죽지 않는다."""
    _insert_slot(conn, cfg, DAY, 5, "coding", active_sec=600.0)
    _insert_breakdown(conn, DAY, 5, "coding", "Code.exe", 600.0)

    result = stats_mod.compute_daily(conn, cfg, DAY)
    assert len(result.slot_categories) == 144
    assert result.slot_categories[5] == "coding"
    assert result.slot_categories[6] == "off"  # 롤업 안 된 슬롯은 off 로 채워짐
    assert result.active_sec == pytest.approx(600.0)


# ── compute_weekly ────────────────────────────────────────────────────


def test_compute_weekly_delta_sign(conn, cfg):
    end_day = "2026-08-16"
    # 이번 주(8/10~8/16): coding 5400초, research 1800초
    _insert_slot(conn, cfg, "2026-08-12", 0, "coding", active_sec=3600.0)
    _insert_breakdown(conn, "2026-08-12", 0, "coding", "Code.exe", 3600.0)
    _insert_breakdown(conn, "2026-08-12", 1, "research", "chrome.exe", 1800.0)
    _insert_slot(conn, cfg, "2026-08-14", 0, "coding", active_sec=1800.0)
    _insert_breakdown(conn, "2026-08-14", 0, "coding", "Code.exe", 1800.0)

    # 지난 주(8/03~8/09): coding 900초, research 3600초
    _insert_breakdown(conn, "2026-08-05", 0, "coding", "Code.exe", 900.0)
    _insert_breakdown(conn, "2026-08-06", 0, "research", "chrome.exe", 3600.0)

    result = stats_mod.compute_weekly(conn, cfg, end_day)

    assert result.end_day == end_day
    assert result.days[0] == "2026-08-10"
    assert result.days[-1] == "2026-08-16"
    assert len(result.days) == 7

    this_map = {c.category: c.seconds for c in result.this_week}
    last_map = {c.category: c.seconds for c in result.last_week}
    assert this_map["coding"] == pytest.approx(5400.0)
    assert this_map["research"] == pytest.approx(1800.0)
    assert last_map["coding"] == pytest.approx(900.0)
    assert last_map["research"] == pytest.approx(3600.0)

    assert result.delta["coding"] == pytest.approx(4500.0)  # 늘었다 -> 양수
    assert result.delta["coding"] > 0
    assert result.delta["research"] == pytest.approx(-1800.0)  # 줄었다 -> 음수
    assert result.delta["research"] < 0


def test_compute_weekly_best_day_and_daily_active(conn, cfg):
    end_day = "2026-08-16"
    _insert_slot(conn, cfg, "2026-08-12", 0, "coding", active_sec=7200.0)
    _insert_slot(conn, cfg, "2026-08-14", 0, "coding", active_sec=1800.0)

    result = stats_mod.compute_weekly(conn, cfg, end_day)

    assert len(result.daily_active) == 7
    active_map = dict(result.daily_active)
    assert active_map["2026-08-12"] == pytest.approx(7200.0)
    assert active_map["2026-08-13"] == pytest.approx(0.0)  # 데이터 없는 날은 0
    assert result.best_day is not None
    assert result.best_day[0] == "2026-08-12"
    assert result.best_day[1] == pytest.approx(7200.0)


def test_compute_weekly_no_data_at_all(conn, cfg):
    result = stats_mod.compute_weekly(conn, cfg, "2099-01-07")
    assert result.this_week == []
    assert result.last_week == []
    assert result.delta == {}
    assert all(sec == 0.0 for _, sec in result.daily_active)
    assert result.best_day is not None
    assert result.best_day[1] == 0.0


# ── format_hm ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (4230, "1시간 10분"),
        (600, "10분"),
        (0, "0분"),
        (3600, "1시간"),
        (59, "0분"),
    ],
)
def test_format_hm(seconds, expected):
    assert stats_mod.format_hm(seconds) == expected


# ── render_*_text ─────────────────────────────────────────────────────


def test_render_daily_text_contains_labels(conn, cfg):
    _fill_day(conn, cfg, DAY)
    result = stats_mod.compute_daily(conn, cfg, DAY)
    text = stats_mod.render_daily_text(result, FakeClassifier())
    assert DAY in text
    assert "코딩" in text
    assert "리서치" in text


def test_render_daily_text_empty_day_no_exception(conn, cfg):
    result = stats_mod.compute_daily(conn, cfg, "2099-01-01")
    text = stats_mod.render_daily_text(result, FakeClassifier())
    assert "기록된 활동이 없습니다" in text


def test_render_weekly_text_no_exception(conn, cfg):
    end_day = "2026-08-16"
    _insert_breakdown(conn, "2026-08-12", 0, "coding", "Code.exe", 3600.0)
    result = stats_mod.compute_weekly(conn, cfg, end_day)
    text = stats_mod.render_weekly_text(result, FakeClassifier())
    assert end_day in text
    assert "코딩" in text
