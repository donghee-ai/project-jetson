"""근거 텍스트 백필 — 제목뿐인 문서에 초록을 채운다.

★ 지키려는 성질 셋:
  ① 사이트 공통 문구(보일러플레이트)를 초록으로 심지 않는다 — 없느니만 못하다
  ② 너무 짧은 것은 안 쓴다
  ③ 한 건이 실패해도 배치가 멈추지 않는다
"""

from __future__ import annotations

import time

import pytest

from lifetrainer import db
from lifetrainer.collect import abstracts as A
from lifetrainer.config import load_config


@pytest.fixture()
def cfg(tmp_path):
    c = load_config()
    import dataclasses

    return dataclasses.replace(c, db_path=tmp_path / "t.db", data_dir=tmp_path)


@pytest.fixture()
def conn(cfg):
    c = db.open_db(cfg)
    yield c
    c.close()


def _doc(conn, doc_id, title, url, abstract=None, summary=None, score=1.0):
    conn.execute(
        "INSERT INTO doc(id, source_id, kind, url, title, abstract, summary, score, dup_of, "
        "fetched_at, content_hash, state) VALUES (?, NULL, 'rss', ?, ?, ?, ?, ?, NULL, ?, ?, 'fetched')",
        (doc_id, url, title, abstract, summary, score, time.time(), f"h{doc_id}"),
    )
    conn.commit()


# ── prose() 규칙 ──────────────────────────────────────────────────────────


def test_네비게이션과_목차를_거른다():
    """메뉴·헤딩은 짧거나 마침표가 없다. 사이트별 선택자를 두지 않는 이유."""
    text = "\n".join(
        [
            "Log In",
            "Sign Up",
            "Back to Articles",
            "Benchmark results",
            "LFM2.5-VL-3B is our most capable vision-language model you can run on your own hardware today.",
        ]
    )
    out = A.prose(text)
    assert out.startswith("LFM2.5-VL-3B is our most")
    assert "Log In" not in out and "Benchmark results" not in out


def test_링크_나열_줄을_막는다():
    """띄어쓰기가 적은 긴 줄은 문장이 아니라 링크 모음이다."""
    text = "a.com/x b.com/y\n" + "x" * 200 + ".\n"
    assert A.prose(text) == ""


def test_상한을_넘지_않는다():
    line = "This is a real sentence with plenty of words in it for the heuristic. " * 40
    assert len(A.prose(line + "\n")) <= A.MAX_ABSTRACT_CHARS


# ── backfill() 동작 ───────────────────────────────────────────────────────


class _FakeSession:
    def __init__(self, bodies):
        self._bodies = bodies

    def get(self, url, **kw):
        from lifetrainer.collect.http import FetchResult

        body = self._bodies.get(url)
        if body is None:
            return FetchResult(url, 404, None, None, {}, False, 1, error="not found")
        return FetchResult(url, 200, body.encode(), body, {}, False, 1)


def _patch(monkeypatch, bodies):
    monkeypatch.setattr(
        "lifetrainer.collect.http.PoliteSession", lambda *a, **k: _FakeSession(bodies)
    )


def _page(paragraph: str) -> str:
    return f"<html><body><p>{paragraph}</p></body></html>"


LONG = (
    "This is a genuine article paragraph that easily clears the minimum length "
    "requirement and reads like real prose written for humans to consume."
)


def test_초록을_채우고_FTS_도_따라_갱신된다(conn, cfg, monkeypatch):
    _doc(conn, 1, "제목뿐인 글", "https://x/1")
    _patch(monkeypatch, {"https://x/1": _page(LONG)})

    r = A.backfill(conn, cfg, limit=10)
    assert r.filled == 1
    got = conn.execute("SELECT abstract FROM doc WHERE id=1").fetchone()[0]
    assert "genuine article paragraph" in got
    # doc_au 트리거가 FTS 를 갱신했는지 — 안 되면 채워도 검색에 안 걸린다
    hit = conn.execute("SELECT rowid FROM doc_fts WHERE doc_fts MATCH ?", ("genuine",)).fetchone()
    assert hit and hit[0] == 1


def test_같은_문구가_여러_건이면_보일러플레이트로_버린다(conn, cfg, monkeypatch):
    """HF 는 모든 글이 사이트 공통 안내문을 돌려준다. 심으면 그 소스 전체가 서로 비슷해진다."""
    for i in (1, 2, 3):
        _doc(conn, i, f"글{i}", f"https://x/{i}")
    _patch(monkeypatch, {f"https://x/{i}": _page(LONG) for i in (1, 2, 3)})

    r = A.backfill(conn, cfg, limit=10)
    assert r.filled == 0 and r.boilerplate == 3
    assert conn.execute("SELECT count(*) FROM doc WHERE abstract IS NOT NULL").fetchone()[0] == 0


def test_너무_짧으면_안_쓴다(conn, cfg, monkeypatch):
    _doc(conn, 1, "제목뿐인 글", "https://x/1")
    _patch(monkeypatch, {"https://x/1": _page("Too short. Nope.")})

    r = A.backfill(conn, cfg, limit=10)
    assert r.filled == 0 and r.too_short == 1
    assert conn.execute("SELECT abstract FROM doc WHERE id=1").fetchone()[0] is None


def test_한_건이_실패해도_배치가_계속된다(conn, cfg, monkeypatch):
    _doc(conn, 1, "죽은 링크", "https://x/dead")
    _doc(conn, 2, "살아있는 글", "https://x/2")
    _patch(monkeypatch, {"https://x/2": _page(LONG)})

    r = A.backfill(conn, cfg, limit=10)
    assert r.failed == 1 and r.filled == 1


def test_dry_run_은_쓰지_않는다(conn, cfg, monkeypatch):
    _doc(conn, 1, "제목뿐인 글", "https://x/1")
    _patch(monkeypatch, {"https://x/1": _page(LONG)})

    r = A.backfill(conn, cfg, limit=10, dry_run=True)
    assert r.filled == 1
    assert conn.execute("SELECT abstract FROM doc WHERE id=1").fetchone()[0] is None


def test_이미_근거가_있는_문서는_건드리지_않는다(conn, cfg):
    _doc(conn, 1, "초록 있음", "https://x/1", abstract="이미 있다")
    _doc(conn, 2, "요약 있음", "https://x/2", summary="요약이 있다")
    _doc(conn, 3, "제목뿐", "https://x/3")
    assert [r["id"] for r in A.pending(conn, 10)] == [3]


def test_점수_높은_것부터_채운다(conn, cfg):
    """중간에 멈춰도 값어치 있는 것부터 채워져 있어야 한다."""
    _doc(conn, 1, "낮음", "https://x/1", score=0.1)
    _doc(conn, 2, "높음", "https://x/2", score=9.9)
    assert [r["id"] for r in A.pending(conn, 10)] == [2, 1]


def test_중간에_끊겨도_직전_묶음까지는_남는다(conn, cfg, monkeypatch):
    """도메인당 2초 간격이라 785건이 40분짜리다. 끝에 한 번만 쓰면 전부 날아간다."""
    bodies = {}
    # ★ 점수를 내림차순으로 줘서 처리 순서를 1→6 으로 고정한다.
    #   `pending()` 이 score DESC, id DESC 라 안 그러면 6→1 로 돈다.
    for i in range(1, 7):
        _doc(conn, i, f"글{i}", f"https://x/{i}", score=10.0 - i)
        bodies[f"https://x/{i}"] = _page(f"Paragraph number {i}. " + LONG)

    class _Boom(_FakeSession):
        def get(self, url, **kw):
            if url == "https://x/5":
                raise KeyboardInterrupt  # 사용자가 끊었다
            return super().get(url, **kw)

    monkeypatch.setattr("lifetrainer.collect.http.PoliteSession", lambda *a, **k: _Boom(bodies))
    with pytest.raises(KeyboardInterrupt):
        A.backfill(conn, cfg, limit=10, flush_every=2)

    # 1~4 중 flush 된 묶음(2건씩)이 DB 에 남아 있어야 한다
    kept = conn.execute("SELECT count(*) FROM doc WHERE abstract IS NOT NULL").fetchone()[0]
    assert kept >= 2


def test_429_가_이어지면_물러난다(conn, cfg, monkeypatch):
    """실측: HF 가 600건째부터 429 를 줬는데 코드가 184번을 더 두드렸다.

    상대가 그만하라고 말하는데 계속 치는 것은 robots.txt 를 지키는 이유와 어긋난다.
    """
    from lifetrainer.collect.http import FetchResult

    for i in range(1, 21):
        _doc(conn, i, f"글{i}", f"https://x/{i}", score=100.0 - i)

    hits = []

    class _RateLimited:
        def get(self, url, **kw):
            hits.append(url)
            return FetchResult(url, 429, None, None, {}, False, 1, error="http_429")

    monkeypatch.setattr("lifetrainer.collect.http.PoliteSession", lambda *a, **k: _RateLimited())
    r = A.backfill(conn, cfg, limit=20)

    assert r.stopped_early is True
    assert len(hits) == A._MAX_CONSECUTIVE_429, "물러나지 않고 계속 두드렸다"


def test_429_가_끊기면_계속한다(conn, cfg, monkeypatch):
    """일시적인 429 하나로 배치 전체를 버릴 이유는 없다."""
    from lifetrainer.collect.http import FetchResult

    for i in range(1, 5):
        _doc(conn, i, f"글{i}", f"https://x/{i}", score=100.0 - i)
    ok = _page(LONG)

    class _Flaky:
        def __init__(self):
            self.n = 0

        def get(self, url, **kw):
            self.n += 1
            if self.n % 2 == 1:
                return FetchResult(url, 429, None, None, {}, False, 1, error="http_429")
            return FetchResult(url, 200, ok.encode(), ok, {}, False, 1)

    monkeypatch.setattr("lifetrainer.collect.http.PoliteSession", lambda *a, **k: _Flaky())
    r = A.backfill(conn, cfg, limit=4, flush_every=1)
    assert r.stopped_early is False
    assert r.failed == 2
