"""본문 수집 — 왜 이 테스트들이 여기 있나.

`docs/rag-plan.md` §7 은 **문서 전문 수집을 안 하기로** 결정하면서 조건을 달아 뒀다:
*"초록으로 부족하다는 증거가 나오면 그때."* 2026-09-04 에 재서 그 조건절을 실행했는데,
**전부 받는 게 아니라 짧은 것만** 받는다 (arXiv 초록은 1,400자대로 이미 충분하다).

그래서 이 파일이 지키는 것은 "받아진다" 가 아니라 **경계**다:

| | 무엇 | 지키는 테스트 |
|---|---|---|
| 대상 | 초록이 긴 문서는 **안 건드린다** | `test_long_abstract_is_not_a_candidate` |
| 예의 | robots 차단은 실패가 아니라 **차단으로 센다** | `test_robots_block_is_counted_apart_from_failure` |
| 반복 | 실패한 문서를 **다시 두드리지 않는다** | `test_a_failed_doc_is_not_retried` |
| 품질 | 껍데기로 초록을 **덮지 않는다** | `test_a_thin_page_is_not_stored` |

네트워크를 타지 않는다 — 가짜 세션을 주입한다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from lifetrainer import db
from lifetrainer.collect.bodies import MIN_BODY_CHARS, collect_bodies, pending_docs


@dataclass
class _Res:
    """`PoliteSession.get()` 이 돌려주는 것 중 이 코드가 보는 필드만."""

    status: int = 200
    text: str | None = ""
    error: str | None = None


class _FakeSession:
    """URL → 결과 표. 안 적힌 URL 은 연결 실패로 친다."""

    def __init__(self, table):
        self.table = table
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self.table.get(url, _Res(status=0, text=None, error="connection"))


@pytest.fixture()
def cfg(tmp_path):
    import dataclasses

    from lifetrainer.config import load_config

    return dataclasses.replace(
        load_config(), db_path=tmp_path / "lt.db", data_dir=tmp_path / "data"
    )


@pytest.fixture()
def conn(cfg):
    c = db.open_db(cfg)
    yield c
    c.close()


def add_doc(conn, *, url, abstract, state="scored", score=1.0):
    cur = conn.execute(
        "INSERT INTO doc(url, title, abstract, fetched_at, content_hash, state, score) "
        "VALUES (?, 'title', ?, ?, ?, ?, ?)",
        (url, abstract, time.time(), f"h-{url}", state, score),
    )
    conn.commit()
    return int(cur.lastrowid)


def _page(chars: int) -> str:
    return "<html><body><p>" + ("본문 " * chars) + "</p></body></html>"


def test_long_abstract_is_not_a_candidate(conn):
    """★ 이 기능의 경계. **arXiv 초록(1,400자대)은 이미 답의 재료다.**

    전부 받으면 얻는 것 없이 robots·용량·요약 비용만 는다. 그래서 대상을 좁힌 것이고,
    그 좁힘이 사라지면 §7 이 안 하기로 한 바로 그 일이 된다.
    """
    add_doc(conn, url="https://example.com/long", abstract="가" * 1400)
    short = add_doc(conn, url="https://example.com/short", abstract="가" * 100)

    assert [r["id"] for r in pending_docs(conn)] == [short]


def test_stores_the_body_and_points_the_row_at_it(conn, cfg):
    doc_id = add_doc(conn, url="https://example.com/a", abstract="짧다")
    session = _FakeSession({"https://example.com/a": _Res(text=_page(500))})

    r = collect_bodies(conn, cfg, session=session)
    conn.commit()

    assert r.fetched == 1
    row = conn.execute("SELECT body_path FROM doc WHERE id = ?", (doc_id,)).fetchone()
    assert row["body_path"], "경로를 안 적으면 받아 놓고도 못 찾는다"
    from pathlib import Path

    assert "본문" in Path(row["body_path"]).read_text(encoding="utf-8")


def test_a_thin_page_is_not_stored(conn, cfg):
    """★ 껍데기로 초록을 덮지 않는다.

    쿠키 배너·"자바스크립트를 켜세요" 가 딱 이 길이로 온다. 저장하면 **초록보다 나쁜
    것으로 초록을 대체**하는 셈이라, 있는 것보다 못한 상태가 된다.
    """
    doc_id = add_doc(conn, url="https://example.com/thin", abstract="짧다")
    thin = "<html><body>쿠키를 허용해 주세요</body></html>"
    assert len(thin) < MIN_BODY_CHARS

    r = collect_bodies(conn, cfg, session=_FakeSession({"https://example.com/thin": _Res(text=thin)}))
    conn.commit()

    assert r.too_thin == 1 and r.fetched == 0
    row = conn.execute("SELECT body_path FROM doc WHERE id = ?", (doc_id,)).fetchone()
    assert row["body_path"] is None


def test_robots_block_is_counted_apart_from_failure(conn, cfg):
    """robots 차단은 **고장이 아니다.** 실패와 같이 세면 남의 정책이 우리 오류로 보인다."""
    add_doc(conn, url="https://example.com/nope", abstract="짧다")
    session = _FakeSession(
        {"https://example.com/nope": _Res(status=0, text=None, error="robots_disallowed")}
    )

    r = collect_bodies(conn, cfg, session=session)
    conn.commit()

    assert r.skipped_robots == 1 and r.failed == 0


def test_a_failed_doc_is_not_retried(conn, cfg):
    """★ 같은 URL 을 매 배치마다 다시 두드리면 그건 남의 서버에 대고 도는 루프다.

    `body_path IS NULL` 만으로는 "아직 안 받아 봤다" 와 "받아 봤는데 안 됐다" 가
    구분되지 않는다 — 그래서 `body_fetch_failed_at` 이 있다 (마이그레이션 009).
    """
    add_doc(conn, url="https://example.com/dead", abstract="짧다")
    session = _FakeSession({})  # 표에 없으면 연결 실패

    collect_bodies(conn, cfg, session=session)
    conn.commit()
    assert session.calls == ["https://example.com/dead"]

    collect_bodies(conn, cfg, session=session)
    conn.commit()
    assert session.calls == ["https://example.com/dead"], "두 번째 배치가 또 두드렸다"


def test_unscored_docs_are_not_candidates(conn):
    """`state='new'` 는 아직 파이프라인 앞이다. 점수가 없어 순서를 정할 수도 없다."""
    add_doc(conn, url="https://example.com/new", abstract="짧다", state="new")
    assert pending_docs(conn) == []
