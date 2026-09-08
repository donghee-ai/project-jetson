"""lifetrainer.collect.feeds 테스트.

네트워크를 전혀 타지 않는다. 고정 RSS XML 문자열을 가짜 세션/`requests.Session.get`
을 통해 주입해서 파싱-저장 경로만 검증한다.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import requests

from lifetrainer import db
from lifetrainer.collect import feeds, http
from lifetrainer.config import (
    AWConfig,
    CollectConfig,
    Config,
    LLMConfig,
    ReportConfig,
    RollupConfig,
    SlackConfig,
)


def make_cfg(tmp_path: Path, *, sources_path: Path | None = None, **collect_overrides) -> Config:
    collect_defaults = dict(
        user_agent="LifeTrainer/0.1 (+test; contact@example.com)",
        per_domain_min_interval_sec=0.0,
        per_domain_concurrency=1,
        arxiv_min_interval_sec=0.0,
        max_fail_before_disable=10,
        sources_path=sources_path or (tmp_path / "sources.yaml"),
        respect_robots=False,
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


RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Test Feed</title>
  <item>
    <title>First Item</title>
    <link>https://example.com/articles/1</link>
    <description>온디바이스 LLM 관련 첫 번째 글</description>
    <pubDate>Sun, 16 Aug 2026 01:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Second Item</title>
    <link>https://example.com/articles/2</link>
    <description>Jetson Orin 관련 두 번째 글</description>
    <pubDate>Sun, 16 Aug 2026 02:00:00 GMT</pubDate>
  </item>
  <item>
    <!-- link 가 없는 깨진 항목: 파싱은 되지만 url 없이 upsert 는 건너뛰어야 한다 -->
    <title>Broken Item Without Link</title>
    <description>링크 없는 깨진 항목</description>
  </item>
</channel>
</rss>
"""


class StubSession:
    """PoliteSession 대역. `.get(url, **kwargs) -> FetchResult` 만 필요하다."""

    def __init__(self, result_or_fn):
        self._result_or_fn = result_or_fn
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if callable(self._result_or_fn):
            return self._result_or_fn(url)
        return self._result_or_fn


def _fetch_result(*, status=200, body=None, error=None, from_cache=False) -> http.FetchResult:
    return http.FetchResult(
        url="https://example.com/feed.xml",
        status=status,
        body=body,
        text=body.decode("utf-8") if isinstance(body, bytes) else None,
        headers={},
        from_cache=from_cache,
        elapsed_ms=1,
        error=error,
    )


def _make_source_row(conn, *, url="https://example.com/feed.xml", kind="rss", name="테스트 피드",
                      next_fetch_at=0.0, enabled=1, interval_sec=3600, tags=""):
    now = time.time()
    conn.execute(
        "INSERT INTO source(kind, name, url, enabled, tags, interval_sec, next_fetch_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, name, url, enabled, tags, interval_sec, next_fetch_at, now),
    )
    return conn.execute("SELECT * FROM source WHERE url = ?", (url,)).fetchone()


# ── load_sources ─────────────────────────────────────────────────────────


def test_load_sources_upserts_sources_and_interests(tmp_path, conn):
    sources_yaml = tmp_path / "sources.yaml"
    sources_yaml.write_text(
        """
sources:
  - {kind: rss, name: "Feed A", url: "https://a.example.com/feed.xml", tags: "ai", interval_sec: 3600}
  - {kind: arxiv, name: "arXiv cs.CL", url: "cat:cs.CL", tags: "paper", interval_sec: 21600}
interests:
  - {term: "jetson", weight: 3.0}
  - {term: "on-device", weight: 2.5}
""",
        encoding="utf-8",
    )
    cfg = make_cfg(tmp_path, sources_path=sources_yaml)

    count = feeds.load_sources(conn, cfg)
    assert count == 2

    rows = conn.execute("SELECT kind, name, url, interval_sec FROM source ORDER BY url").fetchall()
    assert len(rows) == 2
    assert rows[0]["url"] == "cat:cs.CL"  # 'cat:...' < 'https://...' 사전순
    assert rows[0]["kind"] == "arxiv"
    assert rows[1]["kind"] == "rss"

    interests = {r["term"]: r["weight"] for r in conn.execute("SELECT term, weight FROM interest")}
    assert interests == {"jetson": 3.0, "on-device": 2.5}


def test_load_sources_missing_file_returns_zero(tmp_path, conn):
    cfg = make_cfg(tmp_path, sources_path=tmp_path / "does-not-exist.yaml")
    assert feeds.load_sources(conn, cfg) == 0


def test_load_sources_is_idempotent_and_preserves_crawl_state(tmp_path, conn):
    sources_yaml = tmp_path / "sources.yaml"
    sources_yaml.write_text(
        'sources:\n  - {kind: rss, name: "Feed A", url: "https://a.example.com/feed.xml"}\n',
        encoding="utf-8",
    )
    cfg = make_cfg(tmp_path, sources_path=sources_yaml)
    feeds.load_sources(conn, cfg)

    # 이미 수집을 한 번 했다고 가정하고 next_fetch_at 을 미래로 이동시킨다.
    future = time.time() + 999999
    conn.execute("UPDATE source SET next_fetch_at = ? WHERE url = ?", (future, "https://a.example.com/feed.xml"))

    # 다시 로드해도 next_fetch_at(수집 상태)은 건드리지 않아야 한다.
    feeds.load_sources(conn, cfg)
    row = conn.execute("SELECT next_fetch_at FROM source WHERE url = ?", ("https://a.example.com/feed.xml",)).fetchone()
    assert row["next_fetch_at"] == pytest.approx(future)


# ── due_sources ──────────────────────────────────────────────────────────


def test_due_sources_filters_by_next_fetch_at_and_enabled(conn):
    now = 1_000_000.0
    _make_source_row(conn, url="https://a.example.com", next_fetch_at=now - 10, enabled=1)
    _make_source_row(conn, url="https://b.example.com", next_fetch_at=now + 10, enabled=1)  # 아직 미도래
    _make_source_row(conn, url="https://c.example.com", next_fetch_at=now - 10, enabled=0)  # 비활성

    due = feeds.due_sources(conn, now=now)
    urls = [r["url"] for r in due]
    assert urls == ["https://a.example.com"]


# ── fetch_source ─────────────────────────────────────────────────────────


def test_fetch_source_rss_inserts_docs_and_skips_broken_item(tmp_path, conn):
    cfg = make_cfg(tmp_path)
    row = _make_source_row(conn)
    session = StubSession(_fetch_result(status=200, body=RSS_FIXTURE.encode("utf-8")))

    result = feeds.fetch_source(conn, cfg, session, row)

    assert result.error is None
    assert result.status == 200
    assert result.new_docs == 2  # 링크 없는 3번째 항목은 건너뛴다
    assert result.dup_docs == 0

    docs = conn.execute("SELECT title, url, published_at FROM doc ORDER BY url").fetchall()
    assert [d["url"] for d in docs] == ["https://example.com/articles/1", "https://example.com/articles/2"]
    assert docs[0]["published_at"] is not None  # pubDate 파싱 성공


def test_fetch_source_second_run_counts_as_dup_not_new_row(tmp_path, conn):
    cfg = make_cfg(tmp_path)
    row = _make_source_row(conn)
    body = RSS_FIXTURE.encode("utf-8")

    r1 = feeds.fetch_source(conn, cfg, StubSession(_fetch_result(status=200, body=body)), row)
    assert r1.new_docs == 2

    row2 = conn.execute("SELECT * FROM source WHERE id = ?", (row["id"],)).fetchone()
    r2 = feeds.fetch_source(conn, cfg, StubSession(_fetch_result(status=200, body=body)), row2)

    assert r2.new_docs == 0
    assert r2.dup_docs == 2

    count = conn.execute("SELECT COUNT(*) AS n FROM doc").fetchone()["n"]
    assert count == 2  # 중복 URL 로 새 행이 늘지 않는다


def test_fetch_source_304_returns_no_new_docs(tmp_path, conn):
    cfg = make_cfg(tmp_path)
    row = _make_source_row(conn)
    session = StubSession(_fetch_result(status=304, body=None, from_cache=True))

    result = feeds.fetch_source(conn, cfg, session, row)

    assert result.status == 304
    assert result.new_docs == 0
    assert result.dup_docs == 0
    assert result.error is None
    assert conn.execute("SELECT COUNT(*) AS n FROM doc").fetchone()["n"] == 0


def test_fetch_source_http_error_is_reported_not_raised(tmp_path, conn):
    cfg = make_cfg(tmp_path)
    row = _make_source_row(conn)
    session = StubSession(_fetch_result(status=500, body=None, error="http_500"))

    result = feeds.fetch_source(conn, cfg, session, row)

    assert result.error == "http_500"
    assert result.new_docs == 0
    assert result.dup_docs == 0


def test_fetch_source_malformed_feed_body_does_not_raise(tmp_path, conn):
    cfg = make_cfg(tmp_path)
    row = _make_source_row(conn)
    session = StubSession(_fetch_result(status=200, body=b"this is not xml at all {}"))

    result = feeds.fetch_source(conn, cfg, session, row)  # 예외를 올리면 테스트가 바로 실패한다

    assert result.error is None
    assert result.new_docs == 0


# ── upsert_docs 단위 테스트 (항목 하나가 깨져도 나머지는 진행) ────────────────


class _PoisonedEntry(dict):
    """`.get()` 호출 시 예외를 던져 "깨진 항목" 을 흉내낸다."""

    def get(self, key, default=None):
        raise RuntimeError("simulated broken entry")


def test_upsert_docs_survives_one_broken_entry(conn):
    entries = [
        _PoisonedEntry(),
        {"title": "Valid Title", "url": "https://example.com/valid", "author": None,
         "published_at": None, "abstract": "abstract text"},
    ]
    new_docs, dup_docs = feeds.upsert_docs(conn, source_id=None, entries=entries)

    assert new_docs == 1
    assert dup_docs == 0
    row = conn.execute("SELECT title FROM doc WHERE url = ?", ("https://example.com/valid",)).fetchone()
    assert row["title"] == "Valid Title"


def test_upsert_docs_skips_entries_without_title_or_url(conn):
    entries = [
        {"title": "", "url": "https://example.com/no-title"},
        {"title": "No URL", "url": ""},
        {"title": "Good", "url": "https://example.com/good"},
    ]
    new_docs, dup_docs = feeds.upsert_docs(conn, source_id=None, entries=entries)
    assert new_docs == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM doc").fetchone()["n"] == 1


def test_upsert_docs_marks_near_duplicate_via_dup_of_but_keeps_row(conn):
    base_entry = {
        "title": "NVIDIA Jetson Orin NX runs quantized language models locally",
        "url": "https://example.com/orig",
        "author": None,
        "published_at": None,
        "abstract": "A long article about running quantized large language models on Jetson Orin NX boards",
    }
    near_dup_entry = {
        "title": "NVIDIA Jetson Orin NX runs quantized language models locally",
        "url": "https://example.com/syndicated-copy",  # 다른 URL (신디케이션)
        "author": None,
        "published_at": None,
        "abstract": "A long article about running quantized large language models on Jetson Orin NX boards",
    }

    feeds.upsert_docs(conn, source_id=None, entries=[base_entry])
    new_docs, dup_docs = feeds.upsert_docs(conn, source_id=None, entries=[near_dup_entry])

    # URL 이 다르므로 dup_docs(정확 중복) 로는 안 잡히고 새 행으로 들어가되 dup_of 가 채워진다.
    assert new_docs == 1
    assert dup_docs == 0
    row = conn.execute("SELECT dup_of FROM doc WHERE url = ?", ("https://example.com/syndicated-copy",)).fetchone()
    assert row["dup_of"] is not None


# ── run_once (requests.Session.get 를 patch 해 실제 PoliteSession 경로까지 검증) ──


def test_run_once_never_touches_real_network_and_returns_results(tmp_path, conn, monkeypatch):
    sources_yaml = tmp_path / "sources.yaml"
    sources_yaml.write_text(
        'sources:\n  - {kind: rss, name: "Feed A", url: "https://a.example.com/feed.xml", interval_sec: 3600}\n',
        encoding="utf-8",
    )
    cfg = make_cfg(tmp_path, sources_path=sources_yaml, respect_robots=False)

    calls: list[str] = []

    class _FakeResp:
        def __init__(self, status_code, content):
            self.status_code = status_code
            self.headers: dict = {}
            self.content = content
            self.text = content.decode("utf-8")

    def fake_get(self, url, *, headers=None, timeout=None, allow_redirects=True):
        calls.append(url)
        return _FakeResp(200, RSS_FIXTURE.encode("utf-8"))

    monkeypatch.setattr(requests.Session, "get", fake_get)

    results = feeds.run_once(conn, cfg, now=time.time() + 1)

    assert len(results) == 1
    assert results[0].new_docs == 2
    assert results[0].error is None
    assert calls == ["https://a.example.com/feed.xml"]  # 실제 요청은 이 가짜 경로로만 나갔다


def test_run_once_continues_after_one_source_raises(tmp_path, conn, monkeypatch):
    _make_source_row(conn, url="https://broken.example.com/feed.xml", name="Broken")
    _make_source_row(conn, url="https://ok.example.com/feed.xml", name="OK")
    cfg = make_cfg(tmp_path)

    def fake_fetch_source(conn_, cfg_, session_, row):
        if "broken" in row["url"]:
            raise RuntimeError("boom")
        return feeds.FeedResult(source_id=row["id"], name=row["name"], status=200, new_docs=0, dup_docs=0)

    monkeypatch.setattr(feeds, "fetch_source", fake_fetch_source)

    results = feeds.run_once(conn, cfg)

    assert len(results) == 2
    errored = [r for r in results if r.error is not None]
    ok = [r for r in results if r.error is None]
    assert len(errored) == 1


# ── arXiv robots.txt 소스 단위 예외 (2026-08-16, export.arxiv.org 전체 Disallow 대응) ──

ARXIV_ROBOTS_DISALLOW_ALL = "User-agent: *\nDisallow: /\n"

ATOM_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>ArXiv Query</title>
  <entry>
    <id>http://arxiv.org/abs/2608.00001v1</id>
    <title>On-Device Quantized Inference on Jetson Orin</title>
    <summary>A study of running quantized LLMs on edge devices.</summary>
    <link href="http://arxiv.org/abs/2608.00001v1" rel="alternate" type="text/html"/>
    <published>2026-08-15T00:00:00Z</published>
    <author><name>Jane Researcher</name></author>
  </entry>
</feed>
"""


class _RobotsAwareFakeSession:
    """PoliteSession 이 감싸는 requests.Session 대역. robots.txt / 본문 URL 을 구분해 응답한다."""

    def __init__(self, *, robots_body: str, content: bytes):
        self._robots_body = robots_body
        self._content = content
        self.calls: list[str] = []

    def get(self, url, *, headers=None, timeout=None, allow_redirects=True):
        self.calls.append(url)
        if url.endswith("/robots.txt"):
            return _FakeHttpResponse(200, self._robots_body.encode(), self._robots_body)
        return _FakeHttpResponse(200, self._content, self._content.decode("utf-8"))


class _FakeHttpResponse:
    def __init__(self, status_code, content, text):
        self.status_code = status_code
        self.headers: dict = {}
        self.content = content
        self.text = text


def test_fetch_source_arxiv_with_exempt_tag_bypasses_robots_block(tmp_path, conn):
    """load_sources() 가 respect_robots:false 를 tags 에 심어둔 소스는 robots.txt 로
    호스트 전체가 막혀 있어도(export.arxiv.org 실제 상황) 정상적으로 수집돼야 한다."""
    cfg = make_cfg(tmp_path, respect_robots=True)  # 전역 설정은 그대로 true
    row = _make_source_row(
        conn, url="cat:cs.CL", kind="arxiv", name="arXiv cs.CL",
        tags=f"paper,nlp,{feeds.ROBOTS_EXEMPT_TAG}",
    )
    fake_session = _RobotsAwareFakeSession(
        robots_body=ARXIV_ROBOTS_DISALLOW_ALL, content=ATOM_FIXTURE.encode("utf-8")
    )
    ps = http.PoliteSession(cfg, conn, session=fake_session)

    result = feeds.fetch_source(conn, cfg, ps, row)

    assert result.error is None
    assert result.new_docs == 1
    # robots.txt 는 아예 확인하지 않고(예외 적용) 콘텐츠 URL 만 요청됐어야 한다.
    assert not any(c.endswith("/robots.txt") for c in fake_session.calls)
    assert any("export.arxiv.org" in c or "cat:cs.CL" in c or "search_query" in c for c in fake_session.calls)


def test_fetch_source_arxiv_without_exempt_tag_is_still_blocked(tmp_path, conn):
    """태그가 없으면(=respect_robots 기본값 true) 여전히 robots.txt 를 지켜 차단해야 한다
    (회귀 방지 — 예외는 명시적으로 표시된 소스에만 적용된다)."""
    cfg = make_cfg(tmp_path, respect_robots=True)
    row = _make_source_row(conn, url="cat:cs.CL", kind="arxiv", name="arXiv cs.CL", tags="paper,nlp")
    fake_session = _RobotsAwareFakeSession(
        robots_body=ARXIV_ROBOTS_DISALLOW_ALL, content=ATOM_FIXTURE.encode("utf-8")
    )
    ps = http.PoliteSession(cfg, conn, session=fake_session)

    result = feeds.fetch_source(conn, cfg, ps, row)

    assert result.new_docs == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM doc").fetchone()["n"] == 0
    # 콘텐츠 URL 은 robots.txt 에 막혀 절대 요청되지 않았어야 한다.
    assert all(c.endswith("/robots.txt") for c in fake_session.calls) or fake_session.calls == []


def test_load_sources_encodes_respect_robots_false_as_tag(tmp_path, conn):
    """sources.yaml 의 respect_robots: false 가 source.tags 에 표식으로 남는지."""
    sources_yaml = tmp_path / "sources.yaml"
    sources_yaml.write_text(
        'sources:\n'
        '  - {kind: arxiv, name: "arXiv cs.CL", url: "cat:cs.CL", tags: "paper,nlp", '
        'respect_robots: false}\n'
        '  - {kind: rss, name: "Feed A", url: "https://a.example.com/feed.xml"}\n',
        encoding="utf-8",
    )
    cfg = make_cfg(tmp_path, sources_path=sources_yaml)
    feeds.load_sources(conn, cfg)

    arxiv_row = conn.execute("SELECT tags FROM source WHERE url = 'cat:cs.CL'").fetchone()
    rss_row = conn.execute("SELECT tags FROM source WHERE url = 'https://a.example.com/feed.xml'").fetchone()

    assert feeds.ROBOTS_EXEMPT_TAG in arxiv_row["tags"].split(",")
    assert feeds.ROBOTS_EXEMPT_TAG not in rss_row["tags"].split(",")


def test_load_sources_reload_without_respect_robots_removes_tag(tmp_path, conn):
    """나중에 respect_robots 를 다시 true 로(또는 필드 삭제로) 되돌리면 표식도 사라져야 한다."""
    sources_yaml = tmp_path / "sources.yaml"
    sources_yaml.write_text(
        'sources:\n  - {kind: arxiv, name: "arXiv cs.CL", url: "cat:cs.CL", respect_robots: false}\n',
        encoding="utf-8",
    )
    cfg = make_cfg(tmp_path, sources_path=sources_yaml)
    feeds.load_sources(conn, cfg)
    row = conn.execute("SELECT tags FROM source WHERE url = 'cat:cs.CL'").fetchone()
    assert feeds.ROBOTS_EXEMPT_TAG in row["tags"].split(",")

    sources_yaml.write_text(
        'sources:\n  - {kind: arxiv, name: "arXiv cs.CL", url: "cat:cs.CL"}\n',  # respect_robots 필드 제거
        encoding="utf-8",
    )
    feeds.load_sources(conn, cfg)
    row = conn.execute("SELECT tags FROM source WHERE url = 'cat:cs.CL'").fetchone()
    assert feeds.ROBOTS_EXEMPT_TAG not in row["tags"].split(",")


# ── arXiv 가 죽으면 티가 나는가 (2026-09-07) ──────────────────────────────
#
# 실측: arXiv 8개 소스의 `last_status` 가 전부 NULL, `fail_count` 가 전부 0이었다.
# 원인이 둘 겹쳐 있었다 —
#   ① `arxiv.search()` 가 실패해도 `[]` 만 돌려줘 **빈손과 고장이 같은 값**이었다
#      (arXiv 는 주말에 발행을 안 하므로 빈손은 정상이기도 하다)
#   ② `_mark_source_result` 가 `url_state` 를 `source.url`("cat:cs.CL")로 찾는데,
#      실제 요청 URL 은 쿼리 파라미터가 붙은 다른 주소라 **언제나 못 찾았다**
# 둘 다 "안 울린다" 쪽 고장이다 (저장소 규칙 §1).


class _FailingArxivSession:
    """robots 는 통과시키고 API 요청만 실패시키는 대역."""

    def __init__(self, status: int):
        self._status = status
        self.calls: list[str] = []

    def get(self, url, *, headers=None, timeout=None, allow_redirects=True):
        self.calls.append(url)
        if url.endswith("/robots.txt"):
            return _FakeHttpResponse(200, b"", "")
        return _FakeHttpResponse(self._status, b"", "")


def _arxiv_row(conn, **kw):
    return _make_source_row(
        conn, url="cat:cs.CL", kind="arxiv", name="arXiv cs.CL",
        tags=f"paper,{feeds.ROBOTS_EXEMPT_TAG}", **kw,
    )


def test_arxiv_실패가_source_행에_남는다(tmp_path, conn):
    """★ 이게 없으면 arXiv 는 **소리 없이 죽는다.** 유일한 증상이 "논문이 안 들어온다"인데
    주말에도 똑같이 보이므로 사람이 영영 눈치채지 못한다."""
    cfg = make_cfg(tmp_path, respect_robots=True)
    row = _arxiv_row(conn)
    ps = http.PoliteSession(cfg, conn, session=_FailingArxivSession(503))

    feeds.fetch_source(conn, cfg, ps, row)

    after = conn.execute("SELECT * FROM source WHERE id = ?", (row["id"],)).fetchone()
    assert after["last_status"] == 503
    assert after["fail_count"] == 1, "실패가 안 세지면 doctor 가 볼 것이 없다"


def test_arxiv_성공하면_실패_카운터가_꺼진다(tmp_path, conn):
    """★ "언제 꺼지나" 쪽. 고치고 나서도 안 꺼지면 그 신호는 죽은 것이다."""
    cfg = make_cfg(tmp_path, respect_robots=True)
    row = _arxiv_row(conn)
    ps = http.PoliteSession(cfg, conn, session=_FailingArxivSession(503))
    feeds.fetch_source(conn, cfg, ps, row)
    assert conn.execute("SELECT fail_count AS n FROM source WHERE id=?", (row["id"],)).fetchone()["n"] == 1

    ok_session = _RobotsAwareFakeSession(
        robots_body=ARXIV_ROBOTS_DISALLOW_ALL, content=ATOM_FIXTURE.encode("utf-8")
    )
    row = conn.execute("SELECT * FROM source WHERE id = ?", (row["id"],)).fetchone()
    feeds.fetch_source(conn, cfg, http.PoliteSession(cfg, conn, session=ok_session), row)

    after = conn.execute("SELECT * FROM source WHERE id = ?", (row["id"],)).fetchone()
    assert after["last_status"] == 200
    assert after["fail_count"] == 0, "원인을 고쳤는데 카운터가 안 꺼지면 새 실패와 구분이 안 된다"


def test_arxiv_빈손은_실패가_아니다(tmp_path, conn):
    """★ 반대쪽 — 주말에 새 논문이 0건인 것은 **정상이다.** 이걸 실패로 세면
    fail_count 가 주말마다 오르고, 사람이 그 경보를 끈다."""
    cfg = make_cfg(tmp_path, respect_robots=True)
    row = _arxiv_row(conn)
    empty_feed = '<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom"/>'
    session = _RobotsAwareFakeSession(
        robots_body=ARXIV_ROBOTS_DISALLOW_ALL, content=empty_feed.encode("utf-8")
    )

    result = feeds.fetch_source(conn, cfg, http.PoliteSession(cfg, conn, session=session), row)

    assert result.error is None and result.new_docs == 0
    after = conn.execute("SELECT * FROM source WHERE id = ?", (row["id"],)).fetchone()
    assert after["fail_count"] == 0


def test_yaml_에서_사라진_소스는_꺼진다(tmp_path, conn):
    """★ 주소를 고치면 **옛 행이 남아 영원히 실패한다** (2026-09-07 실제 사고).

    upsert 의 키가 url 이라, 폴더 개편이 Microsoft Research 의 URL 을 깨뜨렸다가
    고쳤더니 404 행과 정상 행이 나란히 두 개가 됐다. 죽은 쪽은 yaml 어디에도 없는데
    계속 두드려서, doctor 의 "수집 소스" 경보를 **끌 방법이 없는 상수**로 만들었다.
    """
    sources = tmp_path / "sources.yaml"
    cfg = make_cfg(tmp_path, sources_path=sources)

    sources.write_text('sources:\n  - {kind: rss, name: "옛 주소", url: "https://a.example/old.xml"}\n', encoding="utf-8")
    feeds.load_sources(conn, cfg)
    assert conn.execute("SELECT enabled AS e FROM source WHERE url LIKE '%old%'").fetchone()["e"] == 1

    # 주소를 고친다 (= 옛 항목이 파일에서 사라진다)
    sources.write_text('sources:\n  - {kind: rss, name: "옛 주소", url: "https://a.example/new.xml"}\n', encoding="utf-8")
    feeds.load_sources(conn, cfg)

    rows = {r["url"]: r["enabled"] for r in conn.execute("SELECT url, enabled FROM source")}
    assert rows["https://a.example/new.xml"] == 1
    assert rows["https://a.example/old.xml"] == 0, "옛 행이 계속 켜져 있으면 실패가 영원히 쌓인다"


def test_파일이_비면_아무것도_끄지_않는다(tmp_path, conn):
    """★ 반대쪽 안전장치. yaml 이 깨지거나 비었을 때 **전부 꺼 버리면** 그건 사고다.

    파싱 실패는 이미 위에서 0 을 반환하고 빠져나가지만, 빈 목록도 같은 취급을 해야 한다 —
    "아무것도 안 적혀 있다" 는 "전부 끄라" 가 아니다.
    """
    _make_source_row(conn, url="https://keep.example/feed.xml")
    sources = tmp_path / "sources.yaml"
    sources.write_text("sources: []\n", encoding="utf-8")
    cfg = make_cfg(tmp_path, sources_path=sources)

    feeds.load_sources(conn, cfg)

    assert conn.execute("SELECT enabled AS e FROM source WHERE url LIKE '%keep%'").fetchone()["e"] == 1


def test_연결_실패는_결과가_안_적히는_소스로_안_센다(tmp_path, conn):
    """★ 붙이자마자 오탐이 났다 (2026-09-07). TCP 가 안 붙으면 HTTP 상태가 **없으므로**
    `last_status` NULL 이 정상이다. 그걸 "결과가 안 적힌다" 로 세면 네트워크가 한 번
    끊길 때마다 doctor 가 울고, 그러면 사람이 doctor 를 안 본다.

    실패는 `fail_count` 가 이미 말한다 — **한 사건에 경보는 하나다** (저장소 규칙 §1).
    """
    cfg = make_cfg(tmp_path, respect_robots=False)
    row = _make_source_row(conn, url="https://dead.example/feed.xml")

    class _Refuses:
        def get(self, url, **kwargs):
            raise requests.ConnectionError("연결 거부")

    feeds.fetch_source(conn, cfg, http.PoliteSession(cfg, conn, session=_Refuses()), row)

    after = conn.execute("SELECT last_status, fail_count FROM source WHERE id=?", (row["id"],)).fetchone()
    assert after["last_status"] is None, "연결 실패에 상태 코드가 있을 수 없다"
    assert after["fail_count"] >= 1, "실패는 fail_count 가 말해야 한다"
