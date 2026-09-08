"""lifetrainer.web.auth / web/app.py 의 인증·마스킹 테스트 (docs/contracts.md Part III §4).

Flask `test_client()` 만 쓴다 — 실제 서버는 절대 띄우지 않는다.
`cfg.data_dir` 을 항상 `tmp_path` 로 격리한다 — 그러지 않으면 `ensure_session_secret`
이 실제 프로젝트의 `data/websecret` 파일을 건드리는 부작용이 생긴다.
"""

from __future__ import annotations

import dataclasses
import stat
import time
from urllib.parse import urlsplit

import pytest

from lifetrainer import db, timeutil
from lifetrainer.config import load_config
from lifetrainer.web import auth
from lifetrainer.web.app import create_app

DAY = "2026-08-17"


# ── 픽스처 ──────────────────────────────────────────────────────────────


@pytest.fixture()
def cfg(tmp_path):
    """기본 설정: `external=False`. db/데이터 디렉터리를 tmp 로 격리한다."""
    base = load_config()
    return dataclasses.replace(base, db_path=tmp_path / "lt.db", data_dir=tmp_path / "data")


@pytest.fixture()
def external_cfg(cfg):
    return dataclasses.replace(cfg, web=dataclasses.replace(cfg.web, external=True))


@pytest.fixture()
def client(cfg):
    return create_app(cfg).test_client()


def _insert_slot_with_title(
    cfg, day: str, slot: int, category: str, app_name: str, title: str, seconds: float = 600.0
) -> None:
    """`slot`/`slot_breakdown` 에 top_app/top_title 이 있는 "실제" 데이터를 만든다."""
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    conn.execute(
        "INSERT INTO slot(day, slot, start_ts, category, top_app, top_title, active_sec, updated_at) "
        "VALUES (?, ?, 0, ?, ?, ?, ?, 0) "
        "ON CONFLICT(day, slot) DO UPDATE SET category=excluded.category, top_app=excluded.top_app, "
        "top_title=excluded.top_title, active_sec=excluded.active_sec",
        (day, slot, category, app_name, title, seconds),
    )
    conn.execute(
        "INSERT INTO slot_breakdown(day, slot, category, app, seconds) VALUES (?, ?, ?, ?, ?)",
        (day, slot, category, app_name, seconds),
    )
    conn.commit()
    conn.close()


def _insert_unclassified(cfg, day: str, app_name: str, title: str, fingerprint: str, seconds: float = 300.0) -> None:
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    now = time.time()
    conn.execute(
        "INSERT INTO unclassified(fingerprint, app, title_sample, seconds_total, hits, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, 1, ?, ?)",
        (fingerprint, app_name, title, seconds, now, now),
    )
    conn.execute(
        "INSERT INTO unclassified_day(day, fingerprint, seconds, hits) VALUES (?, ?, ?, 1)",
        (day, fingerprint, seconds),
    )
    conn.commit()
    conn.close()


def _login(cfg, user_id: str = "U123"):
    """`/auth/enter` 를 실제로 왕복해 로그인된 test_client 를 돌려준다."""
    c = create_app(cfg).test_client()
    token = auth.make_link_token(cfg, user_id)
    r = c.get(f"/auth/enter?u={user_id}&t={token}")
    assert r.status_code == 302, r.get_data(as_text=True)
    return c


# ── 서명 링크: 생성 → 검증 ─────────────────────────────────────────────────


def test_make_and_verify_link_token_roundtrip(cfg):
    token = auth.make_link_token(cfg, "U123")
    assert auth.verify_link_token(cfg, token) == "U123"


def test_verify_link_token_expired_is_rejected(cfg):
    now = 1_700_000_000.0
    token = auth.make_link_token(cfg, "U123", now=now)
    with pytest.raises(auth.AuthError):
        auth.verify_link_token(cfg, token, now=now + cfg.web.link_ttl_sec + 1)


def test_verify_link_token_tampered_is_rejected(cfg):
    token = auth.make_link_token(cfg, "U123")
    payload_b64, sig = token.split(".", 1)
    tampered = payload_b64 + "x" + "." + sig  # 서명은 그대로, 페이로드만 위조
    with pytest.raises(auth.AuthError):
        auth.verify_link_token(cfg, tampered)


def test_verify_link_token_wrong_secret_is_rejected(cfg):
    token = auth.make_link_token(cfg, "U123")
    other = dataclasses.replace(cfg, web=dataclasses.replace(cfg.web, session_secret="다른-비밀키"))
    with pytest.raises(auth.AuthError):
        auth.verify_link_token(other, token)


def test_verify_link_token_rejects_session_cookie(cfg):
    """세션 쿠키를 링크 토큰으로 검증하면 실패해야 한다 — 종류(kind) 혼용 방지."""
    cookie = auth.make_session_cookie(cfg, "U123")
    with pytest.raises(auth.AuthError):
        auth.verify_link_token(cfg, cookie)


def test_verify_session_cookie_rejects_link_token(cfg):
    token = auth.make_link_token(cfg, "U123")
    assert auth.verify_session_cookie(cfg, token) is None


def test_verify_session_cookie_returns_none_for_garbage(cfg):
    assert auth.verify_session_cookie(cfg, "not-a-token") is None
    assert auth.verify_session_cookie(cfg, "") is None


# ── 1회용 소비 ────────────────────────────────────────────────────────────


def test_link_token_consume_is_one_time_use(cfg):
    conn = db.open_db(cfg)
    try:
        token = auth.make_link_token(cfg, "U123")
        assert auth.consume_link_token(conn, cfg, token) == "U123"
        with pytest.raises(auth.AuthError):
            auth.consume_link_token(conn, cfg, token)
    finally:
        conn.close()


def test_auth_enter_route_rejects_reused_token(external_cfg):
    token = auth.make_link_token(external_cfg, "U123")

    r1 = create_app(external_cfg).test_client().get(f"/auth/enter?u=U123&t={token}")
    assert r1.status_code == 302

    # 같은 토큰을 다른(쿠키 없는) 클라이언트가 다시 쓴다 — DB 는 파일로 공유되므로
    # jti 기록이 이미 남아 있어 실패해야 한다.
    r2 = create_app(external_cfg).test_client().get(f"/auth/enter?u=U123&t={token}")
    assert r2.status_code == 403
    assert r2.get_json()["ok"] is False


def test_auth_enter_route_rejects_expired_token(external_cfg):
    stale = time.time() - external_cfg.web.link_ttl_sec - 5
    token = auth.make_link_token(external_cfg, "U123", now=stale)
    r = create_app(external_cfg).test_client().get(f"/auth/enter?u=U123&t={token}")
    assert r.status_code == 403


def test_auth_enter_route_rejects_tampered_token(external_cfg):
    token = auth.make_link_token(external_cfg, "U123")
    bad = token[:-1] + ("Z" if not token.endswith("Z") else "Y")
    r = create_app(external_cfg).test_client().get(f"/auth/enter?u=U123&t={bad}")
    assert r.status_code == 403


def test_auth_enter_rejects_mismatched_user_param(external_cfg):
    token = auth.make_link_token(external_cfg, "U123")
    r = create_app(external_cfg).test_client().get(f"/auth/enter?u=SOMEONE_ELSE&t={token}")
    assert r.status_code == 403


# ── 쿠키 교환 → 보호 라우트 ───────────────────────────────────────────────


def test_cookie_from_auth_enter_grants_access_to_protected_routes(external_cfg):
    c = _login(external_cfg)
    assert c.get(f"/api/day/{DAY}").status_code == 200
    assert c.get(f"/d/{DAY}").status_code == 200


def test_auth_enter_redirects_to_requested_day(external_cfg):
    c = create_app(external_cfg).test_client()
    token = auth.make_link_token(external_cfg, "U123")
    r = c.get(f"/auth/enter?u=U123&t={token}&d={DAY}")
    assert r.status_code == 302
    assert r.headers["Location"].endswith(f"/d/{DAY}")


def test_auth_enter_defaults_to_today_without_day_param(external_cfg):
    c = create_app(external_cfg).test_client()
    token = auth.make_link_token(external_cfg, "U123")
    r = c.get(f"/auth/enter?u=U123&t={token}")
    # 논리적 하루(06:00 경계)로 비교한다. `date.today()` 로 두면 00:00~06:00 에만 깨진다.
    today = timeutil.day_str(
        timeutil.now_ts(), external_cfg.tz, boundary_hour=external_cfg.rollup.day_boundary_hour
    )
    assert r.headers["Location"].endswith(f"/d/{today}")


def test_session_cookie_attributes_httponly_samesite_secure(external_cfg):
    c = create_app(external_cfg).test_client()
    token = auth.make_link_token(external_cfg, "U123")
    r = c.get(f"/auth/enter?u=U123&t={token}")
    set_cookie = r.headers.get("Set-Cookie", "")
    assert "HttpOnly" in set_cookie
    assert "SameSite=Lax" in set_cookie
    assert "Secure" in set_cookie  # external=True


# ── 인터넷 노출 경계 (2026-08-22) ─────────────────────────────────────────
# ★ 예전에는 `cfg.web.external` **하나**가 인증을 켰다. 터널만 열고 그 플래그를
#   깜빡하면 플래너가 통째로 공개되는 구조였다. 이제 외부 호스트로 들어온 요청은
#   플래그와 무관하게 세션을 요구한다.


def _external(cfg, **over):
    return dataclasses.replace(
        cfg,
        web=dataclasses.replace(
            cfg.web, external_host="lt.example.org", url_prefix="/planner", **over
        ),
    )


def test_접두사가_페이지에_실려_나간다(cfg):
    """★ JS 가 절대 경로(`/api/...`)로 부르면 접두사 밖으로 나가 터널이 404 로 막는다.

    실제 사고: `/planner` 로 열었을 때 계획 수정·삭제·체크·칸 보정이 **전부 조용히
    실패했다.** 페이지는 서버가 그려서 멀쩡히 보이는데 버튼만 안 먹었다.
    """
    c = create_app(_external(cfg)).test_client()

    plain = c.get("/d/2026-08-19").get_data(as_text=True)
    assert 'data-base=""' in plain, "tailnet 직결인데 접두사가 붙었다"

    prefixed = c.get("/planner/d/2026-08-19").get_data(as_text=True)
    assert 'data-base="/planner"' in prefixed, "접두사가 페이지에 안 실렸다"


def test_주간_화면도_접두사를_받는다(cfg):
    c = create_app(_external(cfg)).test_client()
    assert 'data-base="/planner"' in c.get("/planner/w/2026-08-19").get_data(as_text=True)


def test_외부_호스트_요청은_세션_없이_거절된다(cfg):
    c = create_app(_external(cfg)).test_client()
    r = c.get("/planner/d/2026-08-19", headers={"Host": "lt.example.org"})
    assert r.status_code == 401


def test_tailnet_직결은_예전처럼_통과한다(cfg):
    """같은 프로세스가 둘을 동시에 서빙한다. 안쪽 경로까지 막으면 쓰던 게 깨진다."""
    c = create_app(_external(cfg)).test_client()
    assert c.get("/d/2026-08-19").status_code == 200
    assert c.get("/planner/d/2026-08-19").status_code == 200


def test_외부_호스트라도_ingest_는_본문서명으로_간다(cfg):
    """폰에는 브라우저 세션이 없다. 면제는 '인증 없음'이 아니라 '다른 인증'이다."""
    c = create_app(_external(cfg)).test_client()
    r = c.post("/ingest/aw", headers={"Host": "lt.example.org"}, data=b"{}")
    assert r.status_code in (401, 404)  # 서명 없음 / 수신 꺼짐 — 세션 401 이 아니다


def test_접두사_밖의_경로는_그대로_남는다(cfg):
    """접두사는 **터널·Access 를 한 줄로 막기 위한 것**이다. 앱은 둘 다 받는다."""
    c = create_app(_external(cfg)).test_client()
    assert c.get("/healthz").status_code == 200
    assert c.get("/planner/healthz").status_code == 200


def test_session_cookie_is_secure_over_the_tunnel(cfg):
    """★ 같은 프로세스가 터널(HTTPS)과 tailnet(HTTP)을 동시에 서빙한다.

    설정 하나로 Secure 를 정하면 한쪽이 깨진다 — 켜면 tailnet 브라우저가 쿠키를
    저장하지 않고, 끄면 인터넷 구간에서 평문으로 다닌다. 그래서 요청 단위로 정한다.
    """
    c = create_app(cfg).test_client()
    token = auth.make_link_token(cfg, "U123")
    r = c.get(f"/auth/enter?u=U123&t={token}", headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in r.headers.get("Set-Cookie", "")


def test_session_cookie_not_secure_when_not_external(cfg):
    c = create_app(cfg).test_client()
    token = auth.make_link_token(cfg, "U123")
    r = c.get(f"/auth/enter?u=U123&t={token}")
    set_cookie = r.headers.get("Set-Cookie", "")
    assert "HttpOnly" in set_cookie
    assert "Secure" not in set_cookie


# ── external 스위치 ────────────────────────────────────────────────────


def test_external_false_allows_access_without_any_auth(client):
    """기존 동작 보존: external=False(기본) 면 인증 없이 그대로 쓸 수 있어야 한다."""
    assert client.get(f"/api/day/{DAY}").status_code == 200
    assert client.get(f"/d/{DAY}").status_code == 200
    assert client.get("/healthz").status_code == 200


def test_external_true_blocks_protected_routes_without_cookie(external_cfg):
    c = create_app(external_cfg).test_client()
    assert c.get(f"/api/day/{DAY}").status_code == 401
    assert c.get(f"/d/{DAY}").status_code == 401


def test_external_true_still_allows_healthz_and_static_without_cookie(external_cfg):
    c = create_app(external_cfg).test_client()
    assert c.get("/healthz").status_code == 200
    # /auth/enter 자체는 인증 검사에서 면제된다(토큰이 나쁘면 403, 401 이 아니다).
    r = c.get("/auth/enter?u=U1&t=garbage")
    assert r.status_code == 403


# ── 외부 모드 프라이버시 마스킹 ───────────────────────────────────────────


def test_masking_removes_titles_and_app_names_when_external(external_cfg):
    secret_app = "Signal.exe"
    secret_title = "은밀한-검색어-12345"
    unclassified_app = "SneakyDatingApp.exe"
    unclassified_title = "미분류-은밀한-제목-67890"

    _insert_slot_with_title(external_cfg, DAY, 10, "coding", secret_app, secret_title)
    _insert_unclassified(external_cfg, DAY, unclassified_app, unclassified_title, fingerprint="fp1")

    c = _login(external_cfg)
    r = c.get(f"/api/day/{DAY}")
    assert r.status_code == 200

    body = r.get_data(as_text=True)
    for leaked in (secret_app, secret_title, unclassified_app, unclassified_title):
        assert leaked not in body, f"{leaked!r} 가 external=True 응답에 노출됨"

    js = r.get_json()
    assert "top_app" not in js["slots"][10]
    assert "top_title" not in js["slots"][10]
    assert "top_apps" not in js["stats"]
    for item in js["unclassified"]:
        assert "app" not in item
        assert "title" not in item


def test_masking_applies_to_html_planner_page_too(external_cfg):
    secret_app = "Signal.exe"
    secret_title = "은밀한-검색어-12345"
    _insert_slot_with_title(external_cfg, DAY, 10, "coding", secret_app, secret_title)

    c = _login(external_cfg)
    r = c.get(f"/d/{DAY}")
    assert r.status_code == 200
    html = r.data.decode("utf-8")
    assert secret_app not in html
    assert secret_title not in html


def test_no_masking_when_not_external_control_group(cfg):
    """대조군: 마스킹 함수가 실제로 뭔가를 지운다는 것을 증명한다(원래 없던 게 아니라).

    원문 그대로의 raw body 문자열 검색은 한글(비 ASCII) 값에는 안 맞는다 —
    Flask 의 기본 JSON 인코더가 `ensure_ascii=True` 라 응답 와이어 포맷에서는
    `\\uXXXX` 로 이스케이프되기 때문이다(파싱된 값은 동일하다). 그래서 ASCII 인
    앱 이름은 raw body 로, 한글 제목은 파싱된 JSON 값으로 검증한다.
    """
    secret_app = "Signal.exe"
    secret_title = "은밀한-검색어-12345"
    _insert_slot_with_title(cfg, DAY, 10, "coding", secret_app, secret_title)

    r = create_app(cfg).test_client().get(f"/api/day/{DAY}")
    assert secret_app in r.get_data(as_text=True)

    js = r.get_json()
    assert js["slots"][10]["top_app"] == secret_app
    assert js["slots"][10]["top_title"] == secret_title


# ── ensure_session_secret ────────────────────────────────────────────────


def test_ensure_session_secret_creates_0600_file(cfg):
    path = cfg.data_dir / "websecret"
    assert not path.exists()

    secret = auth.ensure_session_secret(cfg)
    assert secret
    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_ensure_session_secret_reuses_existing_file(cfg):
    first = auth.ensure_session_secret(cfg)
    second = auth.ensure_session_secret(cfg)
    assert first == second


def test_ensure_session_secret_prefers_explicit_cfg_value(cfg):
    explicit = dataclasses.replace(cfg, web=dataclasses.replace(cfg.web, session_secret="내-비밀키"))
    assert auth.ensure_session_secret(explicit) == "내-비밀키"
    assert not (explicit.data_dir / "websecret").exists()


# ── signed_planner_url ────────────────────────────────────────────────────


def test_signed_planner_url_is_none_without_base_url(cfg):
    without_base = dataclasses.replace(
        cfg,
        web=dataclasses.replace(cfg.web, base_url=""),
    )

    assert without_base.web.base_url == ""
    assert auth.signed_planner_url(without_base, "U123", DAY) is None


def test_signed_planner_url_roundtrips_through_auth_enter(external_cfg):
    with_base = dataclasses.replace(
        external_cfg, web=dataclasses.replace(external_cfg.web, base_url="https://lt.example.com")
    )
    url = auth.signed_planner_url(with_base, "U123", DAY)
    assert url is not None
    assert url.startswith("https://lt.example.com/auth/enter?")

    parts = urlsplit(url)
    r = create_app(with_base).test_client().get(f"{parts.path}?{parts.query}")
    assert r.status_code == 302
    assert r.headers["Location"].endswith(f"/d/{DAY}")
