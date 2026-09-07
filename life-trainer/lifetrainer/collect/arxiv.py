"""arXiv API 수집기.

`https://export.arxiv.org/api/query` 는 Atom 응답이라 `feedparser` 로 그대로 파싱된다.
요청 간 최소 간격(기본 3초)은 이 모듈이 직접 세지 않는다 — `PoliteSession` 이 도메인
(`export.arxiv.org`)별 레이트리밋으로 강제한다 (`cfg.collect.arxiv_min_interval_sec`).

## robots.txt 예외 (2026-08-16 결정)

`export.arxiv.org/robots.txt` 는 `Disallow: /` 로 호스트 전체를 막아 두고 있다 —
이건 웹 페이지를 긁는 크롤러를 겨냥한 규칙이다. 반면 `/api/query` 는 arXiv 가
**프로그램 접근용으로 공식 제공하고 별도 API 이용약관을 둔 전용 인터페이스**이며,
설계서(`docs/life-trainer-design.md` §6)도 "HTML 스크레이핑은 최후수단, arXiv API 우선,
요청 간 3초 간격 권장 준수"라고 명시한다. 그래서 이 모듈은 robots.txt 검사를
기본으로 건너뛴다(`respect_robots` 기본값 `False`) — 대신 도메인 레이트리밋(3초)은
그대로 강제해 API 이용약관을 지킨다. 이 결정을 되돌리려면 `search()` 호출부에서
`respect_robots=True` 를 넘기거나, `config/sources.yaml` 의 arXiv 소스 항목에서
`respect_robots: false` 를 지우면 된다 (`feeds.py` 가 그 값을 읽어 넘겨준다).
"""

from __future__ import annotations

import calendar
import logging
import sqlite3
from urllib.parse import urlencode

import feedparser

from lifetrainer.collect.http import PoliteSession
from lifetrainer.config import Config

logger = logging.getLogger(__name__)

ARXIV_API_BASE = "https://export.arxiv.org/api/query"
MAX_RESULTS_PER_PAGE = 100  # arXiv API 권장 상한


def _parsed_time_to_epoch(struct_time) -> float | None:
    if not struct_time:
        return None
    try:
        return float(calendar.timegm(struct_time))
    except Exception:  # noqa: BLE001
        return None


def _normalize_entry(item) -> dict:
    """feedparser Atom 항목을 feeds.upsert_docs 가 기대하는 형태로 정규화한다."""
    title = (getattr(item, "title", "") or "").strip()
    link = (getattr(item, "link", "") or getattr(item, "id", "") or "").strip()
    authors = getattr(item, "authors", None) or []
    names = [a.get("name") for a in authors if isinstance(a, dict) and a.get("name")]
    author = ", ".join(names) if names else None
    abstract = (getattr(item, "summary", "") or "").strip() or None
    published_at = _parsed_time_to_epoch(getattr(item, "published_parsed", None))
    return {"title": title, "url": link, "author": author, "abstract": abstract, "published_at": published_at}


def search_result(
    cfg: Config,
    session: PoliteSession,
    query: str,
    *,
    max_results: int = 50,
    start: int = 0,
    respect_robots: bool = False,
) -> tuple[list[dict], int, str | None]:
    """arXiv API 로 검색한다. `query` 는 arXiv 쿼리 문법 그대로 받는다 (예: 'cat:cs.CL').

    페이지당 최대 `MAX_RESULTS_PER_PAGE`(100) 건. 실패해도 예외를 올리지 않는다
    (계약서 §0: 개별 수집 실패로 전체가 죽으면 안 된다).

    ★ **`(entries, status, error)` 를 돌려준다** (2026-09-07). 예전에는 실패해도 `[]` 만
      돌려줬는데, 그러면 호출부에서 *"arXiv 가 죽었다"* 와 *"오늘 새 논문이 없다"* 가
      **같은 값**이 된다. arXiv 는 주말에 발행을 안 하므로 빈손은 정상이기도 하다 —
      즉 빈 리스트만으로는 고장을 영영 못 본다. 겉면인 `search()` 는 그대로 남는다.

    `respect_robots` 기본값은 `False` 다 — 모듈 docstring 에 적은 대로 `/api/query` 는
    robots.txt 가 아니라 arXiv 의 API 이용약관(요청 간 3초 이상)이 적용 대상이기 때문이다.
    `feeds.py` 는 `config/sources.yaml` 의 소스별 `respect_robots` 값을 읽어 이 인자로 넘긴다.
    """
    capped = min(max_results, MAX_RESULTS_PER_PAGE)
    params = {
        "search_query": query,
        "start": start,
        "max_results": capped,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    url = f"{ARXIV_API_BASE}?{urlencode(params)}"

    if not respect_robots:
        logger.info("robots 예외 적용(공식 API): %s", url)

    result = session.get(url, respect_robots=respect_robots)
    if result.error or not result.body:
        if result.error:
            logger.warning("arXiv 검색 실패 (%s): %s", query, result.error)
        # ★ 304 는 실패가 아니다 — 조건부 GET 이 "안 바뀌었다" 고 답한 것이고 본문이 원래 없다.
        #   실패와 미확인·정상 무변화를 같이 세면 fail_count 가 상수가 된다 (저장소 규칙 §1).
        empty = None if (result.body or result.status == 304) else "empty_body"
        return [], result.status, result.error or empty

    parsed = feedparser.parse(result.body)
    entries: list[dict] = []
    for item in parsed.entries:
        try:
            entries.append(_normalize_entry(item))
        except Exception as exc:  # noqa: BLE001 - 항목 하나가 깨져도 나머지는 진행
            logger.warning("arXiv 항목 파싱 실패, 건너뜀: %s", exc)
            continue
    return entries, result.status, None


def search(cfg, session, query, **kwargs) -> list[dict]:
    """`search_result()` 의 항목만. 결과가 필요 없는 호출부(`collect`)를 위한 얇은 겉면."""
    entries, _status, _error = search_result(cfg, session, query, **kwargs)
    return entries


def collect(conn: sqlite3.Connection, cfg: Config, session: PoliteSession, queries: list[str]) -> int:
    """여러 쿼리로 arXiv 를 수집해 `doc` 테이블에 upsert 한다. 반환값은 새로 추가된 문서 수."""
    # 지연 import: feeds.py 가 kind='arxiv' 소스를 처리할 때 이 모듈을 되부르기 때문에
    # 모듈 최상단에서 서로를 import 하면 순환 임포트가 된다.
    from lifetrainer.collect.feeds import upsert_docs

    total_new = 0
    for query in queries:
        try:
            entries = search(cfg, session, query)
        except Exception as exc:  # noqa: BLE001
            logger.error("arXiv 쿼리 실패, 건너뜀 (%s): %s", query, exc)
            continue
        new_docs, _dup_docs = upsert_docs(conn, source_id=None, entries=entries, kind="paper")
        total_new += new_docs
    return total_new
