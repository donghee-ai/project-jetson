"""lifetrainer.web.app 테스트 (계약서 §4).

Flask `test_client()` 만 쓴다 — 실제 서버는 절대 띄우지 않는다.
DB 는 매 테스트 tmp_path 에 새로 만든다(요청마다 새 커넥션을 여는 앱 설계와 맞춰서
테스트도 매번 깨끗한 DB로 시작한다).
"""

from __future__ import annotations

import dataclasses

import pytest

from lifetrainer import db, timeutil
from lifetrainer.config import load_config
from lifetrainer.web.app import create_app

# 2026-08-17 은 월요일 -> '평일'(12345) 반복 계획이 뜨는 날로 픽스처에서 쓴다.
DAY = "2026-08-17"
SAT = "2026-08-15"  # 토요일 -> 평일 계획이 안 뜨는 날


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


# ── 접두사 아래에서도 내부 링크가 살아 있는가 (2026-08-25) ──────────────
#
# `url_prefix="/planner"` 는 터널(lt.example.com)로 들어온 요청을 경로 하나로
# 막기 위한 것이다. 그런데 **템플릿의 내부 링크가 접두사를 안 붙이면** tailnet
# 직결에서는 멀쩡하고 터널에서만 404 가 난다 — 실제로 주간 버튼이 그랬다.
# JS 는 `data-base` 로 접두사를 쓰고 있었는데 HTML `href` 4개만 빠져 있었다.


@pytest.fixture()
def prefixed_client(cfg):
    prefixed = dataclasses.replace(cfg, web=dataclasses.replace(cfg.web, url_prefix="/planner"))
    return create_app(prefixed).test_client()


def test_day_page_week_link_keeps_the_prefix(prefixed_client):
    html = prefixed_client.get(f"/planner/d/{DAY}", follow_redirects=True).data.decode("utf-8")
    assert f'href="/planner/w/{DAY}"' in html
    # 접두사 없는 링크가 하나라도 남아 있으면 터널에서 404 가 된다.
    assert 'href="/w/' not in html


def test_week_page_links_keep_the_prefix(prefixed_client):
    html = prefixed_client.get(f"/planner/w/{DAY}").data.decode("utf-8")
    assert f'href="/planner/d/{DAY}"' in html          # 일간으로 돌아가기
    assert 'href="/planner/w/2026-08-10"' in html      # 이전 주
    assert 'href="/planner/w/2026-08-24"' in html      # 다음 주
    assert 'href="/w/' not in html and 'href="/d/' not in html


def test_links_have_no_prefix_when_none_configured(client):
    """접두사가 없으면 예전 그대로여야 한다 — tailnet 직결이 깨지면 안 된다."""
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    assert f'href="/w/{DAY}"' in html


def _insert_breakdown(cfg, day: str, slot: int, category: str, seconds: float, app_name: str = "") -> None:
    """slot/slot_breakdown 에 직접 값을 넣어 "실제" 데이터가 있는 날을 만든다."""
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


# ── 기본 라우트 ──────────────────────────────────────────────────────────


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}


def test_index_redirects_to_today(client, cfg):
    r = client.get("/")
    assert r.status_code == 302
    # ★ `date.today()` 를 쓰면 안 된다. 논리적 하루는 06:00 경계라 00:00~06:00 사이엔
    # 벽시계 날짜와 하루가 다르다 — 그 시간대에만 깨지는 테스트가 된다(실제로 깨졌다).
    # 파생값은 한 곳에서만 계산한다: 코드가 쓰는 그 함수를 테스트도 쓴다.
    today = timeutil.day_str(
        timeutil.now_ts(), cfg.tz, boundary_hour=cfg.rollup.day_boundary_hour
    )
    assert r.headers["Location"].endswith(f"/d/{today}")


def test_day_page_renders_html(client):
    r = client.get(f"/d/{DAY}")
    assert r.status_code == 200
    assert r.content_type.startswith("text/html")
    html = r.data.decode("utf-8")
    # 빈 페이지가 아니어야 한다 — 격자/헤더/스크립트가 실제로 렌더링됐는지 확인
    assert "<html" in html.lower()
    assert DAY in html
    assert 'id="grid"' in html
    assert "lt-initial-data" in html
    assert len(html) > 2000


def test_day_page_invalid_date_is_400(client):
    # 빈 문자열은 라우트 자체가 매칭되지 않아 404 다 (별도 케이스로 취급).
    for bad in ["not-a-date", "2026-13-40", "2026-8-1", "20260817"]:
        r = client.get(f"/d/{bad}")
        assert r.status_code == 400, bad


def test_api_day_shape(client):
    r = client.get(f"/api/day/{DAY}")
    assert r.status_code == 200
    js = r.get_json()
    for key in ("slots", "plans", "stats", "palette"):
        assert key in js
    assert isinstance(js["slots"], list)
    assert isinstance(js["plans"], list)
    assert len(js["slots"]) == js["slots_per_day"] == 144


def test_api_day_invalid_date_is_400(client):
    r = client.get("/api/day/nope")
    assert r.status_code == 400
    js = r.get_json()
    assert js["ok"] is False


def test_api_day_empty_day_returns_empty_grid_without_crashing(client):
    """데이터가 전혀 없는 날짜도 죽지 않고 빈 격자를 줘야 한다."""
    r = client.get("/api/day/2099-01-01")
    assert r.status_code == 200
    js = r.get_json()
    assert len(js["slots"]) == 144
    assert all(s["category"] == "off" for s in js["slots"])
    assert js["plans"] == []
    assert js["stats"]["achievement"] == 0.0
    assert js["stats"]["total_count"] == 0
    assert js["stats"]["coverage"] == 0.0


# ── 계획 CRUD 왕복 ───────────────────────────────────────────────────────


def test_plan_create_get_update_delete_roundtrip(client):
    r = client.post(
        "/api/plan",
        json={"title": "코딩", "category": "coding", "start": "09:00", "end": "12:00", "weekdays": "평일"},
    )
    assert r.status_code == 201
    plan = r.get_json()["plan"]
    plan_id = plan["id"]
    assert plan["title"] == "코딩"
    assert plan["weekdays"] == "12345"

    # 조회: 월요일(DAY)에 떠야 한다
    r = client.get(f"/api/day/{DAY}")
    titles = [p["title"] for p in r.get_json()["plans"]]
    assert "코딩" in titles

    # 조회: 토요일에는 안 떠야 한다 (평일 계획)
    r = client.get(f"/api/day/{SAT}")
    titles = [p["title"] for p in r.get_json()["plans"]]
    assert "코딩" not in titles

    # 수정
    r = client.patch(f"/api/plan/{plan_id}", json={"title": "코딩 집중", "category": "research"})
    assert r.status_code == 200
    updated = r.get_json()["plan"]
    assert updated["title"] == "코딩 집중"
    assert updated["category"] == "research"

    # 삭제
    r = client.delete(f"/api/plan/{plan_id}")
    assert r.status_code == 200
    assert r.get_json()["deleted"] is True

    r = client.get(f"/api/day/{DAY}")
    titles = [p["title"] for p in r.get_json()["plans"]]
    assert "코딩 집중" not in titles


def test_plan_create_snaps_to_10min_boundary(client):
    """09:04-12:03 처럼 슬롯과 어긋나는 입력은 가장 가까운 10분 경계로 스냅되어야 한다."""
    r = client.post("/api/plan", json={"title": "운동", "start": "09:04", "end": "12:03"})
    assert r.status_code == 201
    plan = r.get_json()["plan"]
    assert plan["start_min"] % 10 == 0
    assert plan["end_min"] % 10 == 0
    assert plan["start_min"] == 540  # 09:04 -> 09:00 (가장 가까운 10분)
    assert plan["end_min"] == 720  # 12:03 -> 12:00 (가장 가까운 10분)


def test_plan_create_oneoff_via_day_field(client):
    r = client.post("/api/plan", json={"title": "병원", "start": "14:00", "end": "15:00", "day": DAY})
    assert r.status_code == 201
    plan = r.get_json()["plan"]
    assert plan["kind"] == "oneoff"
    assert plan["day"] == DAY

    # 다른 날짜에는 안 뜬다
    other = "2026-08-18"
    r = client.get(f"/api/day/{other}")
    assert "병원" not in [p["title"] for p in r.get_json()["plans"]]

    r = client.get(f"/api/day/{DAY}")
    assert "병원" in [p["title"] for p in r.get_json()["plans"]]


def test_plan_create_missing_title_is_400(client):
    r = client.post("/api/plan", json={"start": "09:00", "end": "10:00"})
    assert r.status_code == 400


def test_plan_create_missing_time_is_400(client):
    r = client.post("/api/plan", json={"title": "제목만"})
    assert r.status_code == 400


def test_plan_update_unknown_id_is_404(client):
    r = client.patch("/api/plan/999999", json={"title": "x"})
    assert r.status_code == 404


def test_plan_delete_unknown_id_is_404(client):
    r = client.delete("/api/plan/999999")
    assert r.status_code == 404


# ── 체크 / 건너뛰기 ─────────────────────────────────────────────────────


def test_plan_check_toggle(client):
    r = client.post("/api/plan", json={"title": "독서", "start": "21:00", "end": "22:00", "weekdays": "평일"})
    plan_id = r.get_json()["plan"]["id"]

    r = client.post(f"/api/plan/{plan_id}/check", json={"day": DAY, "checked": True})
    assert r.status_code == 200
    assert r.get_json()["checked"] is True

    r = client.get(f"/api/day/{DAY}")
    instance = next(p for p in r.get_json()["plans"] if p["id"] == plan_id)
    assert instance["checked"] is True

    r = client.post(f"/api/plan/{plan_id}/check", json={"day": DAY, "checked": False})
    assert r.status_code == 200
    r = client.get(f"/api/day/{DAY}")
    instance = next(p for p in r.get_json()["plans"] if p["id"] == plan_id)
    assert instance["checked"] is False


def test_plan_check_missing_day_is_400(client):
    r = client.post("/api/plan", json={"title": "x", "start": "09:00", "end": "10:00"})
    plan_id = r.get_json()["plan"]["id"]
    r = client.post(f"/api/plan/{plan_id}/check", json={"checked": True})
    assert r.status_code == 400


def test_plan_check_unknown_id_is_404(client):
    r = client.post("/api/plan/999999/check", json={"day": DAY, "checked": True})
    assert r.status_code == 404


def test_plan_skip_removes_from_that_day_only(client):
    r = client.post("/api/plan", json={"title": "요가", "start": "07:00", "end": "08:00", "weekdays": "매일"})
    plan_id = r.get_json()["plan"]["id"]

    r = client.post(f"/api/plan/{plan_id}/skip", json={"day": DAY})
    assert r.status_code == 200
    assert r.get_json()["skipped"] is True

    r = client.get(f"/api/day/{DAY}")
    assert "요가" not in [p["title"] for p in r.get_json()["plans"]]

    other_day = "2026-08-18"
    r = client.get(f"/api/day/{other_day}")
    assert "요가" in [p["title"] for p in r.get_json()["plans"]]


def test_plan_skip_unknown_id_is_404(client):
    r = client.post("/api/plan/999999/skip", json={"day": DAY})
    assert r.status_code == 404


# ── 수동 보정(오버라이드) 왕복 ───────────────────────────────────────────


def test_slot_override_set_get_clear_roundtrip(client, cfg):
    _insert_breakdown(cfg, DAY, 20, "off", 0)

    r = client.get(f"/api/day/{DAY}")
    assert r.get_json()["slots"][20]["category"] == "off"

    r = client.post("/api/slot", json={"day": DAY, "start_slot": 20, "end_slot": 23, "category": "research"})
    assert r.status_code == 200
    assert r.get_json()["touched"] == 3

    r = client.get(f"/api/day/{DAY}")
    slots = r.get_json()["slots"]
    assert slots[20]["category"] == "research"
    assert slots[20]["overridden"] is True
    assert slots[22]["category"] == "research"
    assert slots[23]["category"] != "research"  # 범위 밖 (end_slot 은 exclusive)

    r = client.delete("/api/slot", json={"day": DAY, "start_slot": 20, "end_slot": 23})
    assert r.status_code == 200
    assert r.get_json()["touched"] == 3

    r = client.get(f"/api/day/{DAY}")
    slots = r.get_json()["slots"]
    assert slots[20]["overridden"] is False


def test_slot_set_missing_category_is_400(client):
    r = client.post("/api/slot", json={"day": DAY, "start_slot": 1, "end_slot": 2})
    assert r.status_code == 400


def test_slot_set_invalid_range_is_400(client):
    r = client.post("/api/slot", json={"day": DAY, "start_slot": 5, "end_slot": 5, "category": "coding"})
    assert r.status_code == 400
    r = client.post("/api/slot", json={"day": DAY, "start_slot": 5, "end_slot": 999, "category": "coding"})
    assert r.status_code == 400


def test_slot_set_invalid_day_is_400(client):
    r = client.post("/api/slot", json={"day": "bad", "start_slot": 1, "end_slot": 2, "category": "coding"})
    assert r.status_code == 400


# ── read_only 모드 ───────────────────────────────────────────────────────


def test_read_only_blocks_all_write_routes(ro_client):
    endpoints = [
        ("POST", "/api/plan", {"title": "x", "start": "09:00", "end": "10:00"}),
        ("PATCH", "/api/plan/1", {"title": "x"}),
        ("DELETE", "/api/plan/1", None),
        ("POST", "/api/plan/1/check", {"day": DAY, "checked": True}),
        ("POST", "/api/plan/1/skip", {"day": DAY}),
        ("POST", "/api/slot", {"day": DAY, "start_slot": 1, "end_slot": 2, "category": "coding"}),
        ("DELETE", "/api/slot", {"day": DAY, "start_slot": 1, "end_slot": 2}),
    ]
    for method, url, body in endpoints:
        r = ro_client.open(url, method=method, json=body)
        assert r.status_code == 405, f"{method} {url} 은 read_only 에서 405 여야 합니다"


def test_read_only_allows_read_routes(ro_client):
    assert ro_client.get("/healthz").status_code == 200
    assert ro_client.get(f"/d/{DAY}").status_code == 200
    assert ro_client.get(f"/api/day/{DAY}").status_code == 200


def test_read_only_day_page_hides_write_affordances(ro_client):
    html = ro_client.get(f"/d/{DAY}").data.decode("utf-8")
    assert 'id="add-plan"' not in html
    assert 'id="category-chooser"' not in html


# ── 팔레트 / 2차 인코딩 ──────────────────────────────────────────────────


def test_api_day_palette_matches_config_source(client, cfg):
    from lifetrainer.report.palette import load_palette

    r = client.get(f"/api/day/{DAY}")
    pal_json = r.get_json()["palette"]
    expected = load_palette(cfg.root / "config" / "palette.yaml", "light")
    assert pal_json["order"] == expected.order
    assert pal_json["categories"] == expected.categories
    assert pal_json["labels"] == expected.labels


def test_day_page_shows_legend_labels(client, cfg):
    """2차 인코딩: 범례는 항상 떠 있어야 한다 (색만으로 구분 금지)."""
    from lifetrainer.report.palette import load_palette

    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    pal = load_palette(cfg.root / "config" / "palette.yaml", "light")
    for label in pal.labels.values():
        assert label in html


def test_day_page_labels_long_blocks_of_same_category(client, cfg):
    """3칸(30분) 이상 연속 블록에는 카테고리 이름을 직접 적어야 한다."""
    for slot in range(0, 6):
        _insert_breakdown(cfg, DAY, slot, "coding", 600)
    # winner-takes-all 슬롯 테이블도 채워야 렌더러가 그 값을 읽는다
    conn = db.connect(cfg.db_path)
    for slot in range(0, 6):
        conn.execute(
            "UPDATE slot SET category = 'coding' WHERE day = ? AND slot = ?", (DAY, slot)
        )
    conn.commit()
    conn.close()

    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    assert "cell-label" in html


# ── 앱 팩토리 자체 ───────────────────────────────────────────────────────


def test_create_app_returns_flask_app(app):
    from flask import Flask

    assert isinstance(app, Flask)


def test_each_request_opens_and_closes_its_own_connection(client, cfg):
    """요청마다 새 커넥션을 여닫으므로, 연속 요청 사이에 DB 파일 잠금이 남지 않아야 한다."""
    for _ in range(5):
        r = client.get(f"/api/day/{DAY}")
        assert r.status_code == 200
    # 다른 커넥션으로 즉시 쓰기가 가능해야 한다 (잠긴 채로 남아있지 않음)
    conn = db.connect(cfg.db_path)
    conn.execute("SELECT 1")
    conn.close()


def test_초기_데이터에_프라이빗_초가_실린다(client):
    """★ 차트가 프라이빗을 그릴 수 있는 유일한 경로다 (2026-09-04).

    `by_category` 는 정의상 "away/off 제외 = 분류된 시간" 이라 프라이빗이 들어갈
    자리가 **없다.** 격자는 서버가 렌더한 슬롯에서 구조 상태를 직접 그리므로 보였고,
    차트·원그래프는 이 값만 보므로 **통째로 사라졌다.** 안 보이는 것보다 나쁜 건
    분모에서도 빠져 나머지 비율이 부풀었다는 것이다.

    이 키가 없어지면 그 사고가 그대로 재발하고, 화면은 멀쩡해 보인다.
    """
    import json
    import re

    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    m = re.search(r'id="lt-initial-data"[^>]*>(.*?)</script>', html, re.S)
    assert m, "초기 데이터 블록을 못 찾았다"
    stats = json.loads(m.group(1))["stats"]

    assert "private_sec" in stats, "차트가 프라이빗을 볼 방법이 없어졌다"
    assert isinstance(stats["private_sec"], (int, float))


# ── 격자에서 계획을 정하고 지운다 (2026-09-05) ────────────────────────
#
# 고르개는 **보고 있는 층**을 고친다 — 계획 ON 이면 계획을, OFF 면 실제 보정을.
# 같은 버튼이 화면에 안 보이는 층을 고치면 사람이 무엇을 바꿨는지 모른다.


def _live_plans(cfg, day):
    import sqlite3

    c = sqlite3.connect(cfg.db_path)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT category, start_min, end_min FROM plan_instance "
        "WHERE day = ? AND archived_at IS NULL ORDER BY start_min",
        (day,),
    ).fetchall()
    c.close()
    return [(r["category"], r["start_min"], r["end_min"]) for r in rows]


def test_격자에서_고른_구간이_계획이_된다(client, cfg):
    r = client.post(
        "/api/plan-slot",
        json={"day": DAY, "start_slot": 60, "end_slot": 63, "category": "learning"},
    )
    assert r.status_code == 200, r.get_json()
    # slot 60 = 06:00 + 10h = 16:00 = 960분 (자정 기준). 하루 경계 기준이 아니다.
    assert _live_plans(cfg, DAY) == [("learning", 960, 990)]


def test_계획을_지우면_원래대로_돌아온다(client, cfg):
    """★ 계획은 **겹쳐 그리는 층**이다. 지우면 그 시간은 다시 실제 기록만 남는다."""
    client.post("/api/plan-slot",
                json={"day": DAY, "start_slot": 60, "end_slot": 63, "category": "learning"})
    r = client.delete("/api/plan-slot", json={"day": DAY, "start_slot": 60, "end_slot": 63})

    assert r.status_code == 200 and r.get_json()["removed"] == 1
    assert _live_plans(cfg, DAY) == []


def test_같은_시간에_계획이_둘_쌓이지_않는다(client, cfg):
    """★ 겹치면 화면은 하나만 보여주는데(`plan_cats` 는 나중 것이 이긴다) 달성률에는
    둘 다 들어간다 — **안 보이는 계획**이 숫자를 움직이는 상태가 된다."""
    client.post("/api/plan-slot",
                json={"day": DAY, "start_slot": 60, "end_slot": 66, "category": "learning"})
    r = client.post("/api/plan-slot",
                    json={"day": DAY, "start_slot": 62, "end_slot": 64, "category": "coding"})

    assert r.get_json()["replaced"] == 1
    assert _live_plans(cfg, DAY) == [("coding", 980, 1000)]


def test_거꾸로된_구간은_400_이다(client):
    r = client.post("/api/plan-slot",
                    json={"day": DAY, "start_slot": 10, "end_slot": 5, "category": "coding"})
    assert r.status_code == 400


def test_계획이_격자에서_고른_그_시간에_찍힌다(client, cfg):
    """★ 6시간 밀리던 버그. 슬롯은 06:00 기준, `start_min` 은 자정 기준이다."""
    client.post("/api/plan-slot",
                json={"day": DAY, "start_slot": 0, "end_slot": 6, "category": "coding"})
    assert _live_plans(cfg, DAY) == [("coding", 360, 420)], "slot 0 은 06:00 이어야 한다"


def test_자정을_넘는_구간은_둘로_나뉜다(client, cfg):
    """계획 모델은 자정 넘김을 안 받는다(하루 단위). 그런데 격자의 하루는 06:00~06:00 이라
    저녁~새벽 선택이 자연스럽게 자정을 넘는다 — 거절하는 대신 경계에서 나눈다."""
    r = client.post("/api/plan-slot",
                    json={"day": DAY, "start_slot": 100, "end_slot": 120, "category": "coding"})

    assert r.status_code == 200, r.get_json()
    plans = _live_plans(cfg, DAY)
    assert len(plans) == 2, plans
    assert plans[0] == ("coding", 0, 120), plans      # 00:00~02:00
    assert plans[1] == ("coding", 1360, 1440), plans  # 22:40~24:00


def test_계획이_없어도_계획보기_합계가_비지_않는다(client, cfg):
    """★ 사람이 짚은 것 — 계획 없는 날 계획 보기를 켜면 **차트가 통째로 비었다.**

    격자는 그대로 활동을 보여주는데 차트만 "집계할 활동이 없습니다" 였다 —
    같은 토글이 두 화면에서 다른 뜻이었다. 이제 합성이라 실제와 같아진다.
    """
    import json
    import re

    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    stats = json.loads(re.search(r'id="lt-initial-data"[^>]*>(.*?)</script>', html, re.S).group(1))["stats"]
    assert stats["by_category_plan"] == [
        {"category": c["category"], "seconds": c["seconds"]} for c in stats["by_category"]
    ], "계획이 없으면 계획 보기 합계는 실제와 같아야 한다"


def test_계획이_있으면_그_시간이_계획_카테고리로_들어간다(client, cfg):
    """계획이 있던 시간은 **계획으로**, 나머지는 실제로. 격자의 겹쳐 그리기와 같은 셈이다."""
    import json
    import re

    client.post("/api/plan-slot",
                json={"day": DAY, "start_slot": 0, "end_slot": 6, "category": "learning"})
    html = client.get(f"/d/{DAY}").data.decode("utf-8")
    stats = json.loads(re.search(r'id="lt-initial-data"[^>]*>(.*?)</script>', html, re.S).group(1))["stats"]

    plan = {c["category"]: c["seconds"] for c in stats["by_category_plan"]}
    assert plan.get("learning", 0) >= 3600, plan  # 6칸 = 1시간이 계획으로 실린다
