"""lifetrainer.report.palette / lifetrainer.report.planner 테스트.

`slot`/`slot_breakdown`/`plan` 에 직접 INSERT 해서 픽스처를 만든다
(rollup.py 에 의존하지 않는다 — test_plan.py/test_timeline.py 와 같은 패턴).
네트워크 호출 없음. 픽셀 비교는 하지 않는다 — PNG 가 실제로 생성됐는지,
매직바이트가 맞는지, 죽지 않는지만 확인한다.
"""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path

import pytest

from lifetrainer import db, timeutil
from lifetrainer.config import load_config
from lifetrainer.plan.models import create_plan, set_check
from lifetrainer.report.palette import Palette, css_variables, load_palette
from lifetrainer.report.planner import render_planner_day

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PALETTE_PATH = Path(__file__).resolve().parent.parent / "config" / "palette.yaml"

DAY = "2026-01-05"  # 월요일


@pytest.fixture()
def cfg(tmp_path):
    base = load_config()
    return dataclasses.replace(
        base,
        db_path=tmp_path / "lt.db",
        report=dataclasses.replace(base.report, png_dir=tmp_path / "png"),
    )


@pytest.fixture()
def conn(cfg):
    c = db.connect(cfg.db_path)
    db.init_db(c)
    yield c
    c.close()


def _insert_slot(conn, cfg, day: str, slot: int, category: str, *, active_sec: float = 300.0) -> None:
    start_ts, _ = timeutil.slot_bounds(day, slot, cfg.tz, cfg.rollup.slot_minutes)
    conn.execute(
        "INSERT INTO slot(day, slot, start_ts, category, active_sec, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (day, slot, start_ts, category, active_sec, time.time()),
    )


def _insert_breakdown(conn, day: str, slot: int, category: str, seconds: float, app: str = "") -> None:
    conn.execute(
        "INSERT INTO slot_breakdown(day, slot, category, app, seconds) VALUES (?, ?, ?, ?, ?)",
        (day, slot, category, app, seconds),
    )


def _fill_realistic_day(conn, cfg, day: str) -> None:
    """09~12시(슬롯 54~89)에 코딩 6칸 이상 연속 블록을 포함한 현실적인 하루를 만든다."""
    for slot in range(0, 144):
        conn.execute("DELETE FROM slot WHERE day = ? AND slot = ?", (day, slot))
    # 00~06시(슬롯 0~35): off (기본값, 굳이 넣지 않아도 grid 가 off 로 채움)
    # 09:00~12:00 (슬롯 54~89): 코딩 — 3칸 이상 연속 블록 라벨링 대상
    for slot in range(54, 90):
        _insert_slot(conn, cfg, day, slot, "coding")
        _insert_breakdown(conn, day, slot, "coding", 500.0, app="Code.exe")
    # 13:00~14:00 (슬롯 78은 이미 coding 대역이라 겹치지 않게 15~16시로): 리서치
    for slot in range(90, 96):
        _insert_slot(conn, cfg, day, slot, "research")
        _insert_breakdown(conn, day, slot, "research", 400.0, app="chrome.exe")
    # 21:00~22:00 (슬롯 126~131): 자리비움
    for slot in range(126, 132):
        _insert_slot(conn, cfg, day, slot, "away", active_sec=0.0)
    conn.commit()


def _unclassified(conn, day: str, fingerprint: str, app: str, seconds: float) -> None:
    conn.execute(
        "INSERT INTO unclassified_day(day, fingerprint, seconds, hits) VALUES (?, ?, ?, 1)",
        (day, fingerprint, seconds),
    )
    conn.execute(
        "INSERT INTO unclassified(fingerprint, app, title_sample, seconds_total, hits, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, 1, 0, 0)",
        (fingerprint, app, app, seconds),
    )
    conn.commit()


# ── palette.py ────────────────────────────────────────────────────────


def test_load_palette_light_has_9_categories_and_fixed_order():
    pal = load_palette(_PALETTE_PATH, theme="light")
    assert isinstance(pal, Palette)
    assert pal.theme == "light"
    assert len(pal.categories) == 9
    assert pal.order == [
        "coding", "research", "writing", "sns", "learning", "ops", "browsing",
        "entertainment", "gaming",
    ]
    assert set(pal.labels) == set(pal.categories)
    # 2026-09-01: `private`(재지 않기로 한 시간)이 네 번째 구조 상태로 들어왔다.
    # `sleep` 은 2026-09-05 에 더했다 — 자리비움·결측이 3시간 이상 이어지면 잠으로 본다.
    # 구조 상태이지 활동이 아니다(위의 `len(pal.categories) == 9` 가 그대로인 이유).
    assert set(pal.structural) == {"away", "off", "unknown", "private", "sleep"}
    # 색과 이름이 **같은 집합**이어야 한다 — 한쪽만 더하면 화면에 색은 나오는데
    # 이름이 카테고리 id 로 뜬다(그 반대도 마찬가지).
    assert set(pal.structural_labels) == set(pal.structural)
    for hexval in pal.categories.values():
        assert hexval.startswith("#")


def test_load_palette_light_and_dark_differ():
    light = load_palette(_PALETTE_PATH, theme="light")
    dark = load_palette(_PALETTE_PATH, theme="dark")
    assert light.surface != dark.surface
    assert light.categories["coding"] != dark.categories["coding"]
    assert light.ink["primary"] != dark.ink["primary"]
    # 카테고리 집합/순서 자체는 테마와 무관하게 동일해야 한다
    assert light.order == dark.order


def test_load_palette_invalid_theme_raises():
    with pytest.raises(ValueError):
        load_palette(_PALETTE_PATH, theme="sepia")


def test_css_variables_contains_expected_keys():
    pal = load_palette(_PALETTE_PATH, theme="light")
    css = css_variables(pal)
    assert ":root {" in css
    assert "--surface:" in css
    assert "--cat-coding:" in css
    assert "--structural-off:" in css
    assert "--plan-stroke:" in css


# ── planner.py ────────────────────────────────────────────────────────


def test_render_planner_day_light_creates_valid_png(conn, cfg):
    _fill_realistic_day(conn, cfg, DAY)
    plan_id = create_plan(
        conn, title="코딩", start_min=9 * 60, end_min=12 * 60, category="coding", kind="oneoff", day=DAY,
    )
    set_check(conn, plan_id, DAY, True)
    create_plan(conn, title="운동", start_min=18 * 60, end_min=19 * 60, category=None, kind="oneoff", day=DAY)

    out = render_planner_day(conn, cfg, DAY, theme="light")

    assert out.exists()
    assert out.name == f"{DAY}-planner.png"
    data = out.read_bytes()
    assert len(data) > 0
    assert data[:8] == _PNG_MAGIC


def test_render_planner_day_dark_creates_valid_png_with_different_path(conn, cfg):
    _fill_realistic_day(conn, cfg, DAY)

    out = render_planner_day(conn, cfg, DAY, theme="dark")

    assert out.exists()
    assert out.name == f"{DAY}-planner-dark.png"
    assert out.read_bytes()[:8] == _PNG_MAGIC


def test_render_planner_day_custom_out_path(conn, cfg, tmp_path):
    _fill_realistic_day(conn, cfg, DAY)
    custom = tmp_path / "custom" / "my-planner.png"

    out = render_planner_day(conn, cfg, DAY, out_path=custom, theme="light")

    assert out == custom
    assert out.exists()
    assert out.read_bytes()[:8] == _PNG_MAGIC


def test_render_planner_day_no_plans_does_not_crash(conn, cfg):
    """계획이 0개인 날에도 안내 문구와 함께 빈 격자를 그리고 죽지 않아야 한다."""
    _fill_realistic_day(conn, cfg, DAY)

    out = render_planner_day(conn, cfg, DAY, theme="light")

    assert out.exists()
    assert out.read_bytes()[:8] == _PNG_MAGIC


def test_render_planner_day_no_data_at_all_does_not_crash(conn, cfg, tmp_path):
    """slot/slot_breakdown/plan 이 전부 비어 있는 날에도 빈 격자를 그리고 살아남는다."""
    empty_day = "2026-02-14"

    out = render_planner_day(conn, cfg, empty_day, theme="light")

    assert out.exists()
    data = out.read_bytes()
    assert len(data) > 0
    assert data[:8] == _PNG_MAGIC


def test_render_planner_day_with_unclassified_top3(conn, cfg):
    _fill_realistic_day(conn, cfg, DAY)
    _unclassified(conn, DAY, "fp1", "mystery1.exe", 1200.0)
    _unclassified(conn, DAY, "fp2", "mystery2.exe", 900.0)
    _unclassified(conn, DAY, "fp3", "mystery3.exe", 600.0)
    _unclassified(conn, DAY, "fp4", "mystery4.exe", 300.0)

    out = render_planner_day(conn, cfg, DAY, theme="light")

    assert out.exists()
    assert out.read_bytes()[:8] == _PNG_MAGIC


def test_render_planner_day_many_plans_does_not_crash(conn, cfg):
    """플랜이 많아도(레이아웃이 접히더라도) 죽지 않아야 한다."""
    _fill_realistic_day(conn, cfg, DAY)
    for i in range(20):
        h = i % 20
        create_plan(
            conn, title=f"할일{i}", start_min=h * 60, end_min=h * 60 + 30, kind="oneoff", day=DAY,
        )

    out = render_planner_day(conn, cfg, DAY, theme="light")

    assert out.exists()
    assert out.read_bytes()[:8] == _PNG_MAGIC


# ── 슬롯 -> 격자 (행, 열) 매핑 ────────────────────────────────────────────
#
# 이 파일의 다른 테스트들은 "PNG 가 생성되는가"만 본다. 그 사이로 **밤 활동이 낮
# 칸에 그려지는** 버그가 통과했다 (HISTORY/2026-08-18-planner-row-offset.md):
# 슬롯 0 이 06:00 인데 거기서 또 6을 빼서 6시간(36슬롯)만큼 통째로 밀렸다.
# 그래서 좌표 계산을 픽셀이 아니라 함수 수준에서 못박는다.

from lifetrainer.report.planner import _GRID_ROWS, _row_segments, _slot_to_rowcol

_COLS = 6  # 10분 슬롯 -> 시간당 6칸
_BOUNDARY = 6


def _row_label_hour(row: int) -> int:
    """`_draw_row_labels` 와 같은 규칙 — 행 번호를 벽시계 시(hour)로."""
    return (_BOUNDARY + row) % 24


@pytest.mark.parametrize(
    "wall_min, expected_hour",
    [
        (360, 6),  # 06:00 = 슬롯 0 = 첫 행
        (540, 9),
        (810, 13),  # 13:30
        (1290, 21),
        (1400, 23),  # 23:20
        (60, 1),  # 01:00 — 자정을 넘어 다음 날 (마지막 행 쪽)
        (350, 5),  # 05:50 — 논리적 하루의 마지막 행
    ],
)
def test_슬롯이_제_시각_행에_들어간다(wall_min, expected_hour):
    slot = timeutil.wallclock_min_to_slot(wall_min, 10, boundary_hour=_BOUNDARY)
    row, col = _slot_to_rowcol(slot, _COLS, _BOUNDARY)
    assert 0 <= row < _GRID_ROWS
    assert 0 <= col < _COLS
    assert _row_label_hour(row) == expected_hour, f"{wall_min//60:02d}시가 {_row_label_hour(row)}시 행에 그려짐"


def test_첫_슬롯은_첫_행_첫_열():
    """슬롯 0 은 자정이 아니라 06:00 이다. 여기서 경계를 또 빼면 안 된다."""
    assert _slot_to_rowcol(0, _COLS, _BOUNDARY) == (0, 0)


def test_마지막_슬롯은_마지막_행_마지막_열():
    assert _slot_to_rowcol(143, _COLS, _BOUNDARY) == (_GRID_ROWS - 1, _COLS - 1)


def test_144슬롯이_24행_6열을_정확히_한_번씩_채운다():
    seen = {(_slot_to_rowcol(s, _COLS, _BOUNDARY)) for s in range(144)}
    assert len(seen) == _GRID_ROWS * _COLS


def test_경계_설정을_바꿔도_슬롯0은_첫_행이다():
    """슬롯은 이미 경계 기준이라 grid_start_hour 는 라벨에만 영향을 줘야 한다."""
    for boundary in (0, 4, 6, 9):
        assert _slot_to_rowcol(0, _COLS, boundary) == (0, 0)
        assert _slot_to_rowcol(6, _COLS, boundary) == (1, 0)


def test_계획_구간이_행을_넘으면_행마다_쪼개진다():
    # 09:00~12:00 = 슬롯 18~36 (3시간) -> 09,10,11 세 행
    start = timeutil.wallclock_min_to_slot(540, 10, boundary_hour=_BOUNDARY)
    end = timeutil.wallclock_min_to_slot(720, 10, boundary_hour=_BOUNDARY)
    segs = _row_segments(start, end, _COLS, _BOUNDARY)
    assert [_row_label_hour(r) for r, _, _ in segs] == [9, 10, 11]
    assert all(c0 == 0 and c1 == _COLS for _, c0, c1 in segs)  # 정시~정시라 행을 꽉 채운다


def test_계획_구간과_슬롯_매핑이_같은_행을_가리킨다():
    """두 함수가 각자 계산하면 언젠가 갈라진다 — 같은 행을 주는지 못박는다."""
    start = timeutil.wallclock_min_to_slot(1290, 10, boundary_hour=_BOUNDARY)  # 21:30
    row_from_slot, _ = _slot_to_rowcol(start, _COLS, _BOUNDARY)
    row_from_seg = _row_segments(start, start + 1, _COLS, _BOUNDARY)[0][0]
    assert row_from_slot == row_from_seg == 15
