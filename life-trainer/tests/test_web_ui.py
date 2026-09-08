"""lifetrainer.web.templates / static 마크업 테스트 (계약서 v2 §6, V5 웹 UI).

Flask `test_client()` 로 렌더된 HTML 문자열만 검사한다 — 픽셀 비교는 하지 않는다
(계약서 지시: "픽셀 비교 금지"). 실제 브라우저 캡처로 눈으로 본 확인은 별도로
수행했고(스크린샷), 여기서는 필수 마크업 요소가 항상 나오는지만 회귀 검증한다.

`lifetrainer/web/app.py` 는 다른 담당(V3) 소유라 건드리지 않는다. `/w/<end_day>`
라우트가 아직 없으므로 week.html 은 `render_template()` 을 직접 호출해 검증한다
(미래 라우트가 넘겨줄 컨텍스트를 그대로 흉내낸다 — 정확한 키 목록은 최종 보고 참고).
"""

from __future__ import annotations

import dataclasses
import re

import pytest
from flask import render_template

from lifetrainer import db
from lifetrainer.config import load_config
from lifetrainer.report.palette import css_variables, load_palette
from lifetrainer.web.app import create_app

DAY = "2026-08-17"  # 월요일


@pytest.fixture()
def cfg(tmp_path):
    base = load_config()
    return dataclasses.replace(base, db_path=tmp_path / "lt.db")


@pytest.fixture()
def ro_cfg(cfg):
    return dataclasses.replace(cfg, web=dataclasses.replace(cfg.web, read_only=True))


@pytest.fixture()
def app(cfg):
    return create_app(cfg)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def ro_client(ro_cfg):
    return create_app(ro_cfg).test_client()


def _insert_breakdown(cfg, day: str, slot: int, category: str, seconds: float, app_name: str = "") -> None:
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    conn.execute(
        "INSERT INTO slot(day, slot, start_ts, category, active_sec, updated_at) "
        "VALUES (?, ?, 0, ?, ?, 0) "
        "ON CONFLICT(day, slot) DO UPDATE SET category = excluded.category, "
        "active_sec = excluded.active_sec",
        (day, slot, category, seconds),
    )
    conn.execute(
        "INSERT INTO slot_breakdown(day, slot, category, app, seconds) VALUES (?, ?, ?, ?, ?)",
        (day, slot, category, app_name, seconds),
    )
    conn.commit()
    conn.close()


# ── 격자 / 범례 (그리드 144칸, 06:00 경계 유지) ─────────────────────────


def test_grid_has_144_cells(client):
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    assert len(re.findall(r'data-slot="\d+"', html)) == 144


def test_grid_is_24_rows_of_6(client):
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    # 24개의 시간 행 라벨(00~23, 06시부터 회전)이 있어야 한다.
    hours = re.findall(r'data-hour="(\d+)"', html)
    assert len(hours) == 24
    assert set(int(h) for h in hours) == set(range(24))
    # slot 0 은 06:00 시작이므로 첫 행(문서 순서상)이 06시여야 한다.
    assert hours[0] == "6"


def test_legend_shows_only_categories_used_that_day(client, cfg):
    _insert_breakdown(cfg, DAY, 0, "coding", 600)
    _insert_breakdown(cfg, DAY, 1, "away", 600)
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    pal = load_palette(cfg.root / "config" / "palette.yaml", "light")
    assert 'id="legend"' in html
    legend_html = html.split('<div class="legend" id="legend"', 1)[1].split("</div>", 1)[0]
    assert pal.labels["coding"] in legend_html
    assert pal.labels["gaming"] not in legend_html
    # 구조 상태는 범례에서 옅게(별도 클래스) 구분되어야 한다 — 활동이 아니다.
    assert 'class="legend-item structural"' in html


def test_grid_has_readable_ten_minute_column_headers(client):
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    assert len(re.findall(r'class="grid-minute"', html)) == 6
    for minute in (10, 20, 30, 40, 50, 60):
        assert f'class="grid-minute">{minute}</div>' in html


def test_live_clock_and_exact_now_chip_present(client):
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    assert 'id="live-clock-time"' in html
    assert 'id="now-time-chip"' in html


# ── harugyeol 흡수 요소: NOW 라인 / 완료율 링 / 지금 집중 카드 / eyebrow ──


def test_now_line_placeholder_present(client):
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    assert 'id="now-line"' in html
    assert 'id="now-line-dot"' in html
    assert "now-time-chip" in html


def test_number_first_metric_strip_present(client):
    """★ 2026-09-05 에 두 칸을 바꿨다.

    · 「계획 달성」 — 옆의 "오늘의 계획" 카드가 같은 것을 더 자세히 말한다
    · 「수집 커버리지」 — 하루는 어차피 24시간이고 안 잡힌 시간은 자거나 쉰 것이다.
      "덜 재어졌다" 로 읽히던 숫자를 **무엇을 했는지**(수면)로 바꿨다
    """
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    assert 'class="metric-strip"' in html
    assert "실제 활동" in html
    assert "오늘의 수면" in html
    assert "계획 달성" not in html
    assert "수집 커버리지" not in html


def test_now_card_placeholder_present(client):
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    assert 'id="now-card"' in html
    assert 'id="now-card-title"' in html
    assert 'id="now-card-kicker"' in html
    # 서버 렌더 시점에는 지어낸 문구가 없어야 한다 — js 가 실제 계획으로 채운다.
    assert "Qwen" not in html
    assert "DEMO MODE" not in html


def test_eyebrow_present(client):
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    assert 'class="eyebrow"' in html


def test_app_card_wraps_timetable_and_sidebar_panels(client):
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    assert 'class="grid-panel app-card"' in html
    assert 'class="plan-panel app-card"' in html
    assert 'class="activity-panel app-card"' in html


def test_plan_mutations_are_tucked_into_overflow_menu(client, cfg):
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    from lifetrainer.plan.models import create_plan

    create_plan(conn, title="테스트 계획", start_min=540, end_min=600, weekdays="1234567")
    conn.close()
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    assert '<details class="plan-actions">' in html
    assert 'class="plan-percent"' in html


def test_body_carries_today_boundary_for_now_line_js(client):
    """js 가 NOW 라인/지금 카드를 '오늘'에만 켜려면 boundary_hour 기준 today 가 필요하다."""
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    assert re.search(r'data-today="\d{4}-\d{2}-\d{2}"', html)


def test_redesign_uses_palette_surface_without_old_paper_override(client):
    """새 Apple/Toss 표면은 팔레트 surface를 그대로 쓰고 옛 크림색 예외를 제거한다."""
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    assert "--surface:" in html
    assert "--paper:" not in html


def test_dayflow_activity_feed_merges_contiguous_slots(client, cfg):
    for slot in range(3):
        _insert_breakdown(cfg, DAY, slot, "coding", 600, app_name="Code")
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    assert 'class="activity-feed"' in html
    assert "06:00–06:30" in html
    assert "30분" in html


def test_dayflow_activity_feed_omits_structural_away(client, cfg):
    _insert_breakdown(cfg, DAY, 0, "away", 600)
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    feed_html = html.split('<ol class="activity-feed">', 1)[1].split("</ol>", 1)[0]

    assert "자리비움" not in feed_html


# ── 바텀시트(카테고리 선택) — read_only 에서는 완전히 빠져야 한다 ───────


def test_category_chooser_is_bottom_sheet_with_handle(client):
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    assert 'id="category-chooser"' in html
    assert "sheet-handle" in html


def test_read_only_hides_now_card_actions_and_chooser(ro_client):
    html = ro_client.get(f"/d/{DAY}").data.decode("utf-8")
    assert 'id="category-chooser"' not in html
    assert "sheet-handle" not in html
    assert 'id="add-plan"' not in html
    # now-card 자체(표시)는 읽기 동작이라 남아 있어야 하지만, 완료 기록 버튼은 없어야 한다.
    assert 'id="now-card"' in html
    assert 'id="now-card-check"' not in html


def test_read_only_still_shows_grid_and_legend(ro_client):
    """read_only 여도 144칸 격자와 범례는 그대로 봐야 한다 — 이건 쓰기 동작이 아니다."""
    html = ro_client.get(f"/d/{DAY}").data.decode("utf-8")
    assert len(re.findall(r'data-slot="\d+"', html)) == 144
    assert 'id="legend"' in html


# ── 2차 인코딩(팔레트 계약)이 리디자인 후에도 유지되는지 ────────────────


def test_long_block_still_gets_inline_label(client, cfg):
    for slot in range(0, 6):
        _insert_breakdown(cfg, DAY, slot, "coding", 600)
    conn = db.connect(cfg.db_path)
    for slot in range(0, 6):
        conn.execute("UPDATE slot SET category = 'coding' WHERE day = ? AND slot = ?", (DAY, slot))
    conn.commit()
    conn.close()

    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    assert "cell-label" in html


def test_structural_off_never_gets_inline_label(client, cfg):
    """빈칸(off)은 몇 칸이 이어지든 라벨을 붙이면 안 된다 — 구조 상태는 활동이 아니다."""
    html = client.get("/d/2099-06-01").data.decode("utf-8")  # 데이터 없는 날 = 전부 off
    assert "cell-label" not in html


# ── 정적 파일이 실제로 서빙되는지(신규 week.js 포함) ────────────────────


def test_planner_static_assets_are_served(client):
    for name in ("planner.css", "planner.js"):
        r = client.get(f"/static/{name}")
        assert r.status_code == 200, name


def test_week_js_is_served(client):
    r = client.get("/static/week.js")
    assert r.status_code == 200


def test_planner_css_has_no_hardcoded_hex_besides_documented_paper_exception(client):
    """색은 팔레트가 주입한 :root 변수만 참조해야 한다는 파일 내부 규칙의 회귀 테스트.
    유일한 예외(검증된 --paper)는 CSS 파일이 아니라 템플릿 <style> 안에 있어야 한다.
    주석 안에서 근거를 설명하며 hex 를 '언급'하는 것은 허용한다 — 실제 선언문(`/* */`
    밖)에 하드코딩된 색만 금지한다."""
    r = client.get("/static/planner.css")
    css = r.data.decode("utf-8")
    css_no_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    hex_colors = re.findall(r"#[0-9a-fA-F]{3,8}\b", css_no_comments)
    assert hex_colors == [], f"planner.css 에 하드코딩된 hex 색상이 있습니다: {hex_colors}"


# ── week.html — 아직 라우트가 없으므로 render_template 을 직접 호출한다 ──
# (app.py 소유자가 /w/<end_day> 를 추가하면 그대로 넘겨줄 컨텍스트를 흉내낸다.)


def _inner_decls(css_block: str) -> str:
    return "\n".join(re.findall(r"--[\w-]+\s*:\s*[^;]+;", css_block))


def _render_week(app, cfg, end_day: str = DAY, read_only: bool = False) -> str:
    palette_path = cfg.root / "config" / "palette.yaml"
    pal_light = load_palette(palette_path, "light")
    light_css = css_variables(pal_light)
    dark_css = _inner_decls(css_variables(load_palette(palette_path, "dark")))
    with app.test_request_context():
        return render_template(
            "week.html",
            end_day=end_day,
            today=DAY,
            prev_end_day="2026-08-10",
            next_end_day="2026-08-24",
            light_css_vars=light_css,
            dark_css_vars=dark_css,
            read_only=read_only,
        )


def test_week_page_renders_with_expected_scaffold(app, cfg):
    html = _render_week(app, cfg)
    assert "<html" in html.lower()
    assert DAY in html
    assert 'id="stats-grid"' in html
    assert 'id="week-chart"' in html
    assert "eyebrow" in html
    assert "paper-card" in html
    assert 'data-end-day="2026-08-17"' in html


def test_week_page_links_back_to_day_view(app, cfg):
    html = _render_week(app, cfg)
    assert f'href="/d/{DAY}"' in html


def test_week_page_navigates_prev_next_week(app, cfg):
    html = _render_week(app, cfg)
    assert 'href="/w/2026-08-10"' in html
    assert 'href="/w/2026-08-24"' in html


def test_week_page_uses_only_existing_api_day_no_new_fetch_targets(app, cfg):
    """주간 라우트가 없다는 제약: week.js 는 /api/day/ 만 호출해야 하고
    존재하지 않는 /api/week/ 를 부르면 안 된다(이 파일이 신규 라우트를 전제하면 안 됨)."""
    import pathlib

    week_js = (pathlib.Path(__file__).parent.parent / "lifetrainer" / "web" / "static" / "week.js").read_text(
        encoding="utf-8"
    )
    assert '"/api/day/' in week_js
    assert '"/api/week' not in week_js  # 주석에서의 언급은 허용, 실제 fetch 대상만 금지


def test_week_page_no_fabricated_ai_copy(app, cfg):
    html = _render_week(app, cfg)
    assert "Qwen" not in html
    assert "DEMO MODE" not in html


# ── 정적 파일 캐시 깨기 (2026-08-23) ─────────────────────────────────────
# ★ planner.js 를 고쳐도 브라우저가 옛 사본을 쓰면 화면은 멀쩡한데 버튼만 안 먹는다.
#   `/planner` 접두사 수정을 배포한 뒤에도 같은 404 가 이어졌던 실제 사고.


def test_정적_파일에_버전이_붙는다(client):
    html = client.get("/d/2026-08-19").get_data(as_text=True)
    assert re.search(r"planner\.js\?v=\d+", html), "JS 에 버전 쿼리가 없다"
    assert re.search(r"planner\.css\?v=\d+", html), "CSS 에 버전 쿼리가 없다"


def test_버전은_파일이_바뀔_때만_바뀐다(client):
    """재시작만으로 바뀌면 매번 캐시를 버려 느려진다 — mtime 이라야 한다."""
    a = re.search(r"planner\.js\?v=(\d+)", client.get("/d/2026-08-19").get_data(as_text=True)).group(1)
    b = re.search(r"planner\.js\?v=(\d+)", client.get("/d/2026-08-20").get_data(as_text=True)).group(1)
    assert a == b


# ── 시간 입력: 24시간 · 10분 단위 (2026-08-23) ──────────────────────────
# ★ `<input type="time">` 은 안드로이드 네이티브 피커가 `step` 을 무시해 09:13 을
#   만들 수 있고, 그 값은 `step="600"` 에 걸려 **브라우저가 제출을 막는다** —
#   JS submit 핸들러가 안 불려서 "계획 수정이 안 된다" 로 보인다.
#   기기 로케일이 12시간제면 오전/오후까지 붙는다. 그래서 셀렉트 두 개로 바꿨다.


def test_시간_입력에_type_time_을_쓰지_않는다(client):
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    assert 'type="time"' not in html


def test_시는_24시간_00_부터_23_까지(client):
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    for name in ("start_h", "end_h"):
        block = re.search(rf'<select name="{name}".*?</select>', html, re.S)
        assert block, name
        opts = re.findall(r'<option value="(\d{2})"', block.group(0))
        assert opts == [f"{h:02d}" for h in range(24)], name


def test_분은_10분_단위_여섯_개뿐(client):
    """13분 같은 값을 애초에 고를 수 없어야 한다."""
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    for name in ("start_m", "end_m"):
        block = re.search(rf'<select name="{name}".*?</select>', html, re.S)
        assert block, name
        opts = re.findall(r'<option value="(\d{2})"', block.group(0))
        assert opts == ["00", "10", "20", "30", "40", "50"], name


# ── 오늘의 기록 → 격자 잇기 ──────────────────────────────────────────────


def test_기록_마커는_격자_슬롯을_가리킨다(cfg, client):
    """시각 문자열에서 클라이언트가 슬롯을 다시 계산하면 06:00 경계 규칙이 두 벌이 된다."""
    _insert_breakdown(cfg, DAY, 89, "여가", 600.0)
    html = client.get(f"/d/{DAY}").data.decode("utf-8")

    markers = re.findall(r'data-slot-start="(\d+)" data-slot-end="(\d+)"', html)
    assert markers, "기록 마커가 슬롯 번호를 안 싣고 있다"

    cells = set(re.findall(r'data-slot="(\d+)"', html))
    for start, end in markers:
        assert start in cells and end in cells, f"격자에 없는 슬롯: {start}~{end}"


def test_기록_마커는_누를_수_있는_버튼이다(cfg, client):
    _insert_breakdown(cfg, DAY, 89, "여가", 600.0)
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    assert '<button type="button" class="activity-marker"' in html
    assert "격자에서 보기" in html  # 스크린리더용 설명


def test_슬롯_번호와_표시_시각이_맞는다(cfg, client):
    """슬롯 89 = 06:00 + 890분 = 20:50. 어긋나면 엉뚱한 칸이 반짝인다."""
    _insert_breakdown(cfg, DAY, 89, "여가", 600.0)
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    m = re.search(r'data-slot-start="89"[^>]*aria-label="([^"]+)"', html)
    assert m and m.group(1).startswith("20:50–21:00")


# ── 저장 뒤 화면 교체 (2026-08-23 사용자 보고: "너무 빨리 바뀌어 눈이 아프다") ──


def test_저장_뒤_새로고침은_softReload_를_거친다(client):
    """`location.reload()` 를 그냥 부르면 흰 화면이 번쩍이고 스크롤이 튄다."""
    js = client.get("/static/planner.js").data.decode("utf-8")
    # 주석 속 언급은 제외한다 — 줄 주석과 **블록 주석 둘 다**.
    # ★ 블록 주석을 안 걷어내서 2026-09-05 에 오탐이 났다. `/* … location.reload() … */`
    #   로 *왜 softReload 를 거쳐야 하는지* 설명한 주석이 호출부로 잡혔다.
    #   설명을 지우는 게 아니라 검사기를 고친다 — 주석은 이 저장소가 값을 두는 곳이다.
    body = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    body = re.sub(r"//.*", "", body)
    inside_soft = re.search(r"function softReload\(\).*?\n  \}", body, re.S)
    assert inside_soft, "softReload 가 없다"

    outside = body.replace(inside_soft.group(0), "")
    assert "location.reload()" not in outside, "softReload 를 안 거치는 호출부가 남아 있다"


def test_스크롤_복원은_한_곳에서만_한다(client):
    """브라우저 자체 복원과 겹치면 엉뚱한 위치로 튄다 (실측: 900 → 1492)."""
    js = client.get("/static/planner.js").data.decode("utf-8")
    assert 'history.scrollRestoration = "manual"' in js
    assert 'history.scrollRestoration = "auto"' in js  # 다 옮긴 뒤 되돌려준다


def test_전환은_흐려지기만_하고_밝아지지_않는다(client):
    """화면을 밝게 바꾸면 그것 자체가 또 하나의 번쩍임이 된다."""
    css = client.get("/static/planner.css").data.decode("utf-8")
    block = re.search(r"body\.page-leaving\s*\{[^}]*\}", css)
    assert block and "opacity: 0.72" in block.group(0)
    assert "opacity: 1.2" not in css and "brightness" not in css


def test_모션_최소화를_존중한다(client):
    css = client.get("/static/planner.css").data.decode("utf-8")
    reduce_blocks = re.findall(
        r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n\}", css, re.S
    )
    joined = "\n".join(reduce_blocks)
    assert "page-leaving" in joined and "page-entering" in joined


# ── 기기별 색 차이를 두지 않는다 (2026-08-25) ─────────────────────────────
#
# 08-22 에 폰 칸을 파스텔 + 점으로 구분했다. 비율은 OKLab ΔE 로 계산해서 골랐고
# 기기는 실제로 갈렸는데, **읽는 사람이 원한 구분이 아니었다** — 코딩은 어디서
# 했든 코딩이다. 격자는 "무슨 활동이었나" 하나만 답한다.


def test_grid_does_not_tint_phone_cells():
    """폰 칸에 색·점을 다시 얹으면 격자가 두 가지를 동시에 말하게 된다."""
    from pathlib import Path

    css = (Path(__file__).resolve().parent.parent / "lifetrainer" / "web" / "static" / "planner.css").read_text("utf-8")
    assert '.cell[data-device="phone"]' not in css
    assert "legend-phone-swatch" not in css


def test_phone_legend_swatch_is_gone():
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "lifetrainer" / "web" / "templates" / "planner.html").read_text("utf-8")
    assert "legend-phone-swatch" not in html


# ── 계획 테두리는 칸마다가 아니라 구간으로 (2026-08-25) ────────────────────
#
# 계획에 걸린 칸마다 점선 상자를 그렸더니, 하루 종일짜리 계획에서 격자 전체가
# 점선으로 덮여 실제 활동 색이 안 읽혔다. 사용자 지적: "계획 한곳에 선 있으면 안 이뻐".


def test_plan_edges_are_marked_only_at_the_run_boundaries():
    from pathlib import Path

    css = (Path(__file__).resolve().parent.parent / "lifetrainer" / "web" / "static" / "planner.css").read_text("utf-8")
    assert ".cell.has-plan.plan-start::after" in css
    assert ".cell.has-plan.plan-end::after" in css
    # 칸마다 사방을 두르던 옛 규칙이 되살아나면 안 된다.
    assert "border: 1.5px dashed var(--plan-stroke)" not in css


def test_plan_start_and_end_flags(client):
    """이어진 계획 칸은 시작·끝에만 표식이 붙는다."""
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    # 픽스처에 평일 계획이 있으면 최소 한 쌍이 나온다.
    if "has-plan" in html:
        assert "plan-start" in html and "plan-end" in html


# ── 계획 ON/OFF 토글 (2026-08-25) ─────────────────────────────────────────
#
# 같은 격자가 "한 일"과 "하기로 한 일" 둘 다를 보여준다. 두 값이 **한 응답에
# 같이** 실려 오고 토글은 어느 쪽을 칠할지만 바꾼다 — 서버를 다시 부르면
# 두 화면이 서로 다른 시점의 데이터를 보게 된다.


def _planner_css() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / "lifetrainer" / "web" / "static" / "planner.css").read_text("utf-8")


def test_plan_view_repaints_cells_with_the_plan_colour():
    css = _planner_css()
    assert ".grid.show-plan .cell.has-plan" in css
    assert "var(--plan-color)" in css


def test_plan_view_hides_what_belongs_to_the_actual_side():
    """계획 보기에서 실제 쪽 표식이 남으면 칸이 두 가지를 동시에 말한다.

    단 **계획이 덮은 칸에서만** 숨긴다 — 선택자가 `.has-plan` 으로 좁혀져 있다.
    """
    css = _planner_css()
    assert ".grid.show-plan .cell.has-plan::after { display: none; }" in css              # 계획 띠
    assert ".grid.show-plan .cell.has-plan .cell-label { display: none; }" in css         # 실제 라벨
    assert '.grid.show-plan .cell.has-plan[data-category="away"]' in css                  # 자리비움 빗금
    assert ".grid.show-plan .cell.has-plan.overridden { box-shadow: none; }" in css       # 보정 표식


def test_plan_view_leaves_unplanned_time_untouched(client):
    """★ 계획 ON 은 **하기로 한 시간만** 덮는다 (2026-08-25).

    처음 만들 때는 `.grid.show-plan .cell` 로 격자 전체를 계획 색으로 칠하고,
    계획이 없는 칸에는 빈칸 색(`--structural-off`)을 실어 보냈다. 그래서 계획을
    켜는 순간 **계획이 없는 시간의 측정 결과가 통째로 사라졌다** — 하루 중 계획이
    세 시간뿐이면 나머지 스물한 시간이 빈칸으로 보인다.

    계획 보기는 덮어쓰기가 아니라 겹쳐 그리기다. 두 가지를 못박는다:

    1. 계획 없는 칸은 계획 색을 아예 안 받는다 (`--plan-color: var(--cell-color)`)
    2. CSS 규칙이 `.cell` 이 아니라 `.cell.has-plan` 을 잡는다
    """
    import re

    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    # (`--structural-off` 자체는 팔레트 변수로 페이지에 정의돼 있다. 여기서
    #  막는 것은 그 값이 **칸의 계획 색으로 실려 나가는 것**이다.)
    assert "--plan-color: var(--structural-off)" not in html
    assert "--plan-color: var(--cell-color)" in html  # 자기 활동 색을 그대로 되돌려준다

    css = _planner_css()
    # `.grid.show-plan .cell` 뒤에 `.has-plan` 이 안 붙은 규칙이 하나라도 있으면
    # 계획 없는 칸까지 잡는다. `.cell,` `.cell {` `.cell[` `.cell::` 전부 해당.
    bare = re.findall(r"\.grid\.show-plan \.cell(?!\.has-plan)(?=[\s,{:\[])", css)
    assert bare == [], f"계획 없는 칸까지 잡는 선택자 {len(bare)}개가 남아 있다"


def test_toggle_persists_the_choice():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "lifetrainer" / "web" / "static" / "planner.js").read_text("utf-8")
    assert "lt-plan-view" in js and "localStorage" in js


def test_보기_이름은_차트와_원그래프다():
    """사용자에게 구현 방식인 '막대/도넛' 대신 보기의 뜻을 말한다."""
    from pathlib import Path

    import lifetrainer.web as web_pkg

    tpl = (Path(web_pkg.__file__).parent / "templates" / "planner.html").read_text(encoding="utf-8")
    assert '>차트</button>' in tpl
    assert '>원그래프</button>' in tpl
    assert '>막대</button>' not in tpl
    assert '>도넛</button>' not in tpl


# ── 건너뛰기 해제 라우트 ─────────────────────────────────────────────────


def test_건너뛰기_해제_라우트가_있다(cfg, client):
    """★ 2026-09-01 까지 되돌릴 길이 없었다.

    `unskip_plan` 은 만들어져 있었는데 CLI·웹 어디서도 안 불렀다.
    (건너뛰기 자체가 전개 뒤에는 화면을 안 바꾸는 문제는 `docs/issues/0026` 이다 —
     여기서 보는 것은 **경로가 존재하고 인증·검증을 지키는가**다.)
    """
    from lifetrainer.plan.models import create_plan

    conn = db.open_db(cfg)
    plan_id = create_plan(
        conn, title="산책", category="away", start_min=420, end_min=450, weekdays="1234567"
    )
    conn.close()

    assert client.delete(f"/api/plan/{plan_id}/skip").status_code == 400        # day 없음
    assert client.delete(f"/api/plan/{plan_id}/skip", json={"day": "엉터리"}).status_code == 400
    assert client.delete("/api/plan/99999/skip", json={"day": DAY}).status_code == 404

    resp = client.delete(f"/api/plan/{plan_id}/skip", json={"day": DAY})
    assert resp.status_code == 200 and resp.get_json()["skipped"] is False


# ── 기록 목록에서도 고치고 지운다 (2026-09-05) ────────────────────────
#
# 격자에서만 되면 "이 줄" 을 고치려고 격자에서 그 칸을 다시 찾아야 한다.
# 목록이 이미 슬롯 번호를 들고 있으므로 **같은 고르개**를 그대로 연다 —
# 좌표를 다시 계산하면 하루 경계(06:00) 규칙이 두 벌이 된다 (반복 실패 2번).


def test_기록_목록이_슬롯_번호를_들고_있다():
    """이게 없으면 목록에서 연 고르개가 **어느 구간인지 모른다.**

    ★ 렌더 결과가 아니라 템플릿을 본다 — 테스트 DB 에 활동이 없으면 목록이 비어 있어서
      `{% else %}` 가지가 나오고, 그러면 이 계약을 검사할 수가 없다.
    """
    from pathlib import Path

    import lifetrainer.web as web_pkg

    tpl = (Path(web_pkg.__file__).parent / "templates" / "planner.html").read_text(encoding="utf-8")
    assert 'class="activity-marker"' in tpl
    assert "data-slot-start=" in tpl and "data-slot-end=" in tpl


def test_목록에_우클릭과_길게누르기가_배선돼_있다(client):
    """여는 방법이 둘이어야 한다 — 마우스는 우클릭, 손가락은 꾹 누르기."""
    js = client.get("/static/planner.js").data.decode("utf-8")
    assert 'item.addEventListener("contextmenu"' in js
    assert "LONG_PRESS_MS" in js and 'item.addEventListener("pointerdown"' in js


def test_길게누르기가_스크롤에_취소된다(client):
    """★ 안 열려야 하는 쪽. 목록은 스크롤되므로, 손가락이 움직이면 **스크롤 의도**다.

    이게 없으면 목록을 넘길 때마다 고르개가 튀어나온다 — 그러면 사람이 목록을 안 쓴다.
    """
    js = client.get("/static/planner.js").data.decode("utf-8")
    assert "LONG_PRESS_SLOP" in js
    assert 'item.addEventListener("pointermove"' in js
    assert 'item.addEventListener("pointercancel"' in js


def test_고르개에_기록_지우기가_있다(client):
    """보정(색을 바꾼다)과 삭제(기록을 없앤다)는 다른 일이라 버튼도 갈라져 있어야 한다."""
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    assert 'id="chooser-purge"' in html
    assert 'id="chooser-clear"' in html
    assert 'id="undo-bar"' in html, "되돌리는 길이 화면에 없으면 되돌리기가 없는 것과 같다"
