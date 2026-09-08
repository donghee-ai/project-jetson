"""lifetrainer.collect.http (PoliteSession) 테스트.

네트워크를 전혀 타지 않는다 — `requests.Session` 자리에 FakeSession 을 주입한다.
시간이 관련된 검증(레이트리밋 지연, 백오프)은 `time.sleep` 을 monkeypatch 해서 확인한다.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from lifetrainer import db
from lifetrainer.collect import http
from lifetrainer.config import (
    AWConfig,
    CollectConfig,
    Config,
    LLMConfig,
    ReportConfig,
    RollupConfig,
    SlackConfig,
)


# ── 테스트용 Config / 가짜 세션 ─────────────────────────────────────────


def make_cfg(tmp_path: Path, **collect_overrides) -> Config:
    """테스트에 필요한 최소 필드만 채운 Config. 레이트리밋 간격은 기본 0(즉시)으로 둔다."""
    collect_defaults = dict(
        user_agent="LifeTrainer/0.1 (+test; contact@example.com)",
        per_domain_min_interval_sec=0.0,
        per_domain_concurrency=1,
        arxiv_min_interval_sec=0.0,
        max_fail_before_disable=10,
        sources_path=tmp_path / "sources.yaml",
        respect_robots=True,
    )
    collect_defaults.update(collect_overrides)
    return Config(
        root=tmp_path,
        timezone="Asia/Seoul",
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "lt.db",
        log_level="INFO",
        aw=AWConfig(
            base_url="http://127.0.0.1:5600", api_key="", timeout_sec=10.0,
            poll_interval_sec=600, overlap_sec=900, backfill_days=7, hosts=(),
        ),
        rollup=RollupConfig(
            slot_minutes=10, afk_category="away", no_data_category="off",
            min_active_ratio=0.05, rules_path=tmp_path / "rules.yaml",
        ),
        report=ReportConfig(
            png_dir=tmp_path / "png", font_family="DejaVu Sans",
            daily_at="23:30", weekly_at="22:00", morning_at="07:30",
        ),
        slack=SlackConfig(
            mode="notify", bot_token="", app_token="", default_channel="",
            openclaw_config=tmp_path / "openclaw.json",
        ),
        llm=LLMConfig(
            base_url="http://127.0.0.1:8080/v1", model="test", timeout_sec=30.0,
            max_input_tokens=4000, enable_thinking=False,
        ),
        collect=CollectConfig(**collect_defaults),
    )


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "lt.db")
    db.init_db(c)
    yield c
    c.close()


class FakeResponse:
    def __init__(self, status_code=200, headers=None, content=b"", text=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content
        self.text = text if text is not None else content.decode("utf-8", errors="replace")


class FakeSession:
    """requests.Session 대역. `responder(url, headers, timeout) -> FakeResponse` 로 동작을 정의한다."""

    def __init__(self, responder):
        self._responder = responder
        self.calls: list[dict] = []

    def get(self, url, *, headers=None, timeout=None, allow_redirects=True):
        # `allow_redirects` 는 실제 `requests.Session.get` 이 받는 인자다. 가짜가
        # 안 받으면 실물보다 **더 엄격해져서** 통과하는 코드를 테스트가 막는다.
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers or {}),
                "timeout": timeout,
                "allow_redirects": allow_redirects,
            }
        )
        return self._responder(url, headers or {}, timeout)


def _allow_all_robots(url, headers, timeout):
    """robots.txt 를 항상 404(=규칙 없음, 전체 허용)로 응답하는 기본 responder."""
    return FakeResponse(status_code=404)


def _url_state_row(conn, url):
    return conn.execute("SELECT * FROM url_state WHERE url = ?", (url,)).fetchone()


# ── 도메인별 레이트리밋 ──────────────────────────────────────────────────


def test_rate_limit_sleeps_between_consecutive_same_domain_requests(tmp_path, conn, monkeypatch):
    cfg = make_cfg(tmp_path, per_domain_min_interval_sec=5.0, respect_robots=False)

    def responder(url, headers, timeout):
        return FakeResponse(status_code=200, content=b"hello")

    session = FakeSession(responder)
    sleeps: list[float] = []
    monkeypatch.setattr(http.time, "sleep", lambda s: sleeps.append(s))

    ps = http.PoliteSession(cfg, conn, session=session)
    ps.get("https://example.com/a")
    ps.get("https://example.com/b")  # 같은 도메인, 바로 이어서 요청

    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(5.0, abs=0.5)


def test_rate_limit_does_not_apply_across_different_domains(tmp_path, conn, monkeypatch):
    cfg = make_cfg(tmp_path, per_domain_min_interval_sec=5.0, respect_robots=False)

    def responder(url, headers, timeout):
        return FakeResponse(status_code=200, content=b"hello")

    session = FakeSession(responder)
    sleeps: list[float] = []
    monkeypatch.setattr(http.time, "sleep", lambda s: sleeps.append(s))

    ps = http.PoliteSession(cfg, conn, session=session)
    ps.get("https://one.example.com/a")
    ps.get("https://two.example.com/a")  # 다른 도메인 -> 대기 없음

    assert sleeps == []


def test_arxiv_domain_uses_arxiv_min_interval(tmp_path, conn, monkeypatch):
    cfg = make_cfg(
        tmp_path, per_domain_min_interval_sec=0.0, arxiv_min_interval_sec=3.0, respect_robots=False
    )

    def responder(url, headers, timeout):
        return FakeResponse(status_code=200, content=b"<feed></feed>")

    session = FakeSession(responder)
    sleeps: list[float] = []
    monkeypatch.setattr(http.time, "sleep", lambda s: sleeps.append(s))

    ps = http.PoliteSession(cfg, conn, session=session)
    ps.get("http://export.arxiv.org/api/query?search_query=cat:cs.CL")
    ps.get("http://export.arxiv.org/api/query?search_query=cat:cs.LG")

    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(3.0, abs=0.5)


# ── 조건부 GET / 304 ─────────────────────────────────────────────────────


def test_304_response_has_no_body_and_increments_unchanged_streak(tmp_path, conn):
    cfg = make_cfg(tmp_path, respect_robots=False)
    url = "https://example.com/feed.xml"

    # 1차: 200 으로 etag 를 저장시킨다.
    def first_responder(u, headers, timeout):
        return FakeResponse(status_code=200, headers={"ETag": '"abc"'}, content=b"content-v1")

    ps = http.PoliteSession(cfg, conn, session=FakeSession(first_responder))
    r1 = ps.get(url)
    assert r1.status == 200
    assert r1.from_cache is False

    # 2차: 조건부 GET 헤더가 실제로 전송되는지 + 304 응답 처리.
    sent_headers: dict = {}

    def second_responder(u, headers, timeout):
        sent_headers.update(headers)
        return FakeResponse(status_code=304)

    session2 = FakeSession(second_responder)
    ps2 = http.PoliteSession(cfg, conn, session=session2)
    r2 = ps2.get(url)

    assert sent_headers.get("If-None-Match") == '"abc"'
    assert r2.status == 304
    assert r2.body is None
    assert r2.text is None
    assert r2.from_cache is True
    assert r2.error is None

    row = _url_state_row(conn, url)
    assert row["unchanged_streak"] == 1
    assert row["fail_count"] == 0


def test_use_state_false_skips_conditional_get_and_state_tracking(tmp_path, conn):
    cfg = make_cfg(tmp_path, respect_robots=False)
    url = "https://example.com/no-state.xml"

    def responder(u, headers, timeout):
        assert "If-None-Match" not in headers
        return FakeResponse(status_code=200, content=b"x")

    ps = http.PoliteSession(cfg, conn, session=FakeSession(responder))
    result = ps.get(url, use_state=False)

    assert result.status == 200
    assert _url_state_row(conn, url) is None


# ── 적응형 주기 ──────────────────────────────────────────────────────────


def test_adaptive_interval_doubles_after_three_unchanged(tmp_path, conn):
    cfg = make_cfg(tmp_path, respect_robots=False)
    url = "https://example.com/stable.xml"

    def changed_responder(u, headers, timeout):
        return FakeResponse(status_code=200, content=b"initial-content")

    ps = http.PoliteSession(cfg, conn, session=FakeSession(changed_responder))
    ps.get(url)
    row = _url_state_row(conn, url)
    interval_after_first = row["interval_sec"]
    assert interval_after_first == max(http.DEFAULT_URL_INTERVAL_SEC // 2, http.ADAPTIVE_MIN_INTERVAL_SEC)

    def not_modified_responder(u, headers, timeout):
        return FakeResponse(status_code=304)

    ps2 = http.PoliteSession(cfg, conn, session=FakeSession(not_modified_responder))
    for _ in range(3):
        ps2.get(url)

    row = _url_state_row(conn, url)
    assert row["unchanged_streak"] == 3
    assert row["interval_sec"] == min(interval_after_first * 2, http.ADAPTIVE_MAX_INTERVAL_SEC)


def test_adaptive_interval_halves_when_content_changes(tmp_path, conn):
    cfg = make_cfg(tmp_path, respect_robots=False)
    url = "https://example.com/changing.xml"

    ps1 = http.PoliteSession(
        cfg, conn, session=FakeSession(lambda u, h, t: FakeResponse(status_code=200, content=b"v1"))
    )
    ps1.get(url)
    row = _url_state_row(conn, url)
    interval_after_v1 = row["interval_sec"]

    ps2 = http.PoliteSession(
        cfg, conn, session=FakeSession(lambda u, h, t: FakeResponse(status_code=200, content=b"v2-different"))
    )
    ps2.get(url)
    row = _url_state_row(conn, url)

    assert row["unchanged_streak"] == 0
    assert row["interval_sec"] == max(interval_after_v1 // 2, http.ADAPTIVE_MIN_INTERVAL_SEC)


# ── 실패 시 지수 백오프 ───────────────────────────────────────────────────


def test_failure_backoff_grows_with_consecutive_failures(tmp_path, conn):
    cfg = make_cfg(tmp_path, respect_robots=False)
    url = "https://example.com/broken.xml"

    ps = http.PoliteSession(
        cfg, conn, session=FakeSession(lambda u, h, t: FakeResponse(status_code=500))
    )

    r1 = ps.get(url)
    assert r1.status == 500
    assert r1.error == "http_500"
    row1 = _url_state_row(conn, url)
    assert row1["fail_count"] == 1
    delay1 = row1["next_fetch_at"] - row1["last_fetched"]

    r2 = ps.get(url)
    assert r2.status == 500
    row2 = _url_state_row(conn, url)
    assert row2["fail_count"] == 2
    delay2 = row2["next_fetch_at"] - row2["last_fetched"]

    assert delay2 > delay1


def test_connection_exception_is_captured_not_raised(tmp_path, conn):
    cfg = make_cfg(tmp_path, respect_robots=False)
    url = "https://example.com/timeout.xml"

    def raising_responder(u, headers, timeout):
        raise TimeoutError("simulated timeout")

    ps = http.PoliteSession(cfg, conn, session=FakeSession(raising_responder))
    result = ps.get(url)  # 예외를 올리면 안 된다

    assert result.status == 0
    assert result.body is None
    assert result.error is not None
    assert "timeout" in result.error.lower()

    row = _url_state_row(conn, url)
    assert row["fail_count"] == 1


def test_max_fail_before_disable_pushes_next_fetch_far_out(tmp_path, conn):
    cfg = make_cfg(tmp_path, max_fail_before_disable=2, respect_robots=False)
    url = "https://example.com/dead.xml"

    ps = http.PoliteSession(
        cfg, conn, session=FakeSession(lambda u, h, t: FakeResponse(status_code=503))
    )

    for _ in range(3):  # max_fail_before_disable(2) 를 넘긴다
        ps.get(url)

    row = _url_state_row(conn, url)
    assert row["fail_count"] == 3
    # 사실상 비활성화 -> 다음 시도가 몇 달 뒤로 밀려야 한다.
    assert row["next_fetch_at"] - time.time() > 30 * 86400


# ── robots.txt ───────────────────────────────────────────────────────────


def test_robots_disallowed_url_is_never_requested(tmp_path, conn):
    cfg = make_cfg(tmp_path, respect_robots=True)
    robots_body = "User-agent: *\nDisallow: /private/\n"

    def responder(url, headers, timeout):
        if url.endswith("/robots.txt"):
            return FakeResponse(status_code=200, content=robots_body.encode(), text=robots_body)
        return FakeResponse(status_code=200, content=b"secret content")

    session = FakeSession(responder)
    ps = http.PoliteSession(cfg, conn, session=session)

    result = ps.get("https://example.com/private/secret")

    assert result.error == "robots_disallowed"
    assert result.body is None
    # robots.txt 요청 한 번만 있어야 하고, 실제 컨텐츠 URL 은 절대 요청되지 않아야 한다.
    requested_urls = [c["url"] for c in session.calls]
    assert requested_urls == ["https://example.com/robots.txt"]


def test_robots_allowed_url_is_requested_normally(tmp_path, conn):
    cfg = make_cfg(tmp_path, respect_robots=True)
    robots_body = "User-agent: *\nDisallow: /private/\n"

    def responder(url, headers, timeout):
        if url.endswith("/robots.txt"):
            return FakeResponse(status_code=200, content=robots_body.encode(), text=robots_body)
        return FakeResponse(status_code=200, content=b"public content")

    session = FakeSession(responder)
    ps = http.PoliteSession(cfg, conn, session=session)

    result = ps.get("https://example.com/public/page")

    assert result.status == 200
    assert result.body == b"public content"
    requested_urls = [c["url"] for c in session.calls]
    assert "https://example.com/public/page" in requested_urls


def test_robots_missing_defaults_to_allowed(tmp_path, conn):
    cfg = make_cfg(tmp_path, respect_robots=True)
    ps = http.PoliteSession(cfg, conn, session=FakeSession(_allow_all_robots))

    # responder 는 robots.txt 에 404 를 주므로(=파일 없음), 전체 허용으로 취급되어야 한다.
    assert ps.allowed("https://example.com/anything") is True


def test_robots_cache_reused_within_ttl(tmp_path, conn):
    cfg = make_cfg(tmp_path, respect_robots=True)
    robots_calls = {"count": 0}
    robots_body = "User-agent: *\nDisallow: /blocked/\n"

    def responder(url, headers, timeout):
        if url.endswith("/robots.txt"):
            robots_calls["count"] += 1
            return FakeResponse(status_code=200, content=robots_body.encode(), text=robots_body)
        return FakeResponse(status_code=200, content=b"ok")

    session = FakeSession(responder)
    ps = http.PoliteSession(cfg, conn, session=session)

    ps.get("https://example.com/page1")
    ps.get("https://example.com/page2")

    # 두 번 다른 URL 을 요청해도 robots.txt 는 캐시 TTL 안에서 한 번만 가져온다.
    assert robots_calls["count"] == 1


def test_respect_robots_false_skips_robots_check(tmp_path, conn):
    cfg = make_cfg(tmp_path, respect_robots=False)

    def responder(url, headers, timeout):
        assert not url.endswith("/robots.txt")  # robots.txt 요청 자체가 없어야 한다
        return FakeResponse(status_code=200, content=b"ok")

    session = FakeSession(responder)
    ps = http.PoliteSession(cfg, conn, session=session)

    result = ps.get("https://example.com/anything")
    assert result.status == 200


# ── get() 의 소스 단위 respect_robots 오버라이드 (arXiv 공식 API 예외) ──────────


def test_get_respect_robots_false_override_bypasses_global_block(tmp_path, conn):
    """전역 respect_robots=True 라도, 호출부가 respect_robots=False 를 넘기면
    (예: arXiv 공식 API 소스) robots.txt 에 막힌 URL 도 실제로 요청되어야 한다."""
    cfg = make_cfg(tmp_path, respect_robots=True)  # 전역 기본값은 그대로 유지
    robots_body = "User-agent: *\nDisallow: /\n"  # export.arxiv.org 처럼 전체 차단

    def responder(url, headers, timeout):
        if url.endswith("/robots.txt"):
            return FakeResponse(status_code=200, content=robots_body.encode(), text=robots_body)
        return FakeResponse(status_code=200, content=b"arxiv atom feed body")

    session = FakeSession(responder)
    ps = http.PoliteSession(cfg, conn, session=session)

    result = ps.get("https://export.arxiv.org/api/query?search_query=cat:cs.CL", respect_robots=False)

    assert result.status == 200
    assert result.body == b"arxiv atom feed body"
    assert result.error is None
    # 오버라이드가 적용되면 robots.txt 자체를 확인할 필요도 없다 -> 콘텐츠 URL 만 요청된다.
    requested_urls = [c["url"] for c in session.calls]
    assert requested_urls == ["https://export.arxiv.org/api/query?search_query=cat:cs.CL"]


def test_get_respect_robots_none_still_enforces_global_block(tmp_path, conn):
    """`respect_robots` 를 지정하지 않으면(None) 기존처럼 전역 설정을 따라 차단해야 한다
    (회귀 방지 — 오버라이드 기능 추가가 기본 동작을 바꾸면 안 된다)."""
    cfg = make_cfg(tmp_path, respect_robots=True)
    robots_body = "User-agent: *\nDisallow: /\n"

    def responder(url, headers, timeout):
        if url.endswith("/robots.txt"):
            return FakeResponse(status_code=200, content=robots_body.encode(), text=robots_body)
        return FakeResponse(status_code=200, content=b"should not be fetched")

    session = FakeSession(responder)
    ps = http.PoliteSession(cfg, conn, session=session)

    result = ps.get("https://export.arxiv.org/api/query?search_query=cat:cs.CL")  # respect_robots 미지정

    assert result.error == "robots_disallowed"
    assert result.body is None
    requested_urls = [c["url"] for c in session.calls]
    assert requested_urls == ["https://export.arxiv.org/robots.txt"]


def test_get_respect_robots_true_override_forces_check_even_if_global_false(tmp_path, conn):
    """반대 방향 오버라이드도 동작해야 한다: 전역이 False 라도 호출부가 True 를 주면 검사한다."""
    cfg = make_cfg(tmp_path, respect_robots=False)
    robots_body = "User-agent: *\nDisallow: /\n"

    def responder(url, headers, timeout):
        if url.endswith("/robots.txt"):
            return FakeResponse(status_code=200, content=robots_body.encode(), text=robots_body)
        return FakeResponse(status_code=200, content=b"should not be fetched")

    session = FakeSession(responder)
    ps = http.PoliteSession(cfg, conn, session=session)

    result = ps.get("https://example.com/anything", respect_robots=True)

    assert result.error == "robots_disallowed"


# ── allow_redirects (llm/webfetch 의 홉별 SSRF 검사를 위해 추가) ──────────


def test_기본은_리다이렉트를_따라간다(tmp_path, conn):
    """수집 경로의 동작은 바뀌면 안 된다 — allow_redirects 는 새로 추가된 인자일 뿐이다."""
    cfg = make_cfg(tmp_path, respect_robots=False)
    session = FakeSession(lambda u, h, t: FakeResponse(status_code=200, content=b"x"))
    http.PoliteSession(cfg, conn, session=session).get("https://example.com/a")
    assert session.calls[-1]["allow_redirects"] is True


def test_리다이렉트를_끌_수_있다(tmp_path, conn):
    """`llm/webfetch` 가 홉마다 SSRF 검사를 하려면 자동 추적을 꺼야 한다."""
    cfg = make_cfg(tmp_path, respect_robots=False)
    session = FakeSession(lambda u, h, t: FakeResponse(status_code=302, headers={"Location": "https://x/"}))
    http.PoliteSession(cfg, conn, session=session).get("https://example.com/a", allow_redirects=False)
    assert session.calls[-1]["allow_redirects"] is False
