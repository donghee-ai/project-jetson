"""RSS/Atom 피드 수집기.

`config/sources.yaml` 를 `source` 테이블로 적재하고, 기한이 된 소스를
`PoliteSession` 을 통해 조건부 GET 으로 가져와 `feedparser` 로 파싱한 뒤
`doc` 테이블에 넣는다. 완전 중복(같은 URL)은 건너뛰고, 근사 중복(SimHash)은
행은 남기되 `dup_of` 를 채운다 (뉴스 신디케이션 추적용).
"""

from __future__ import annotations

import calendar
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import feedparser
import yaml

from lifetrainer.collect import dedupe
from lifetrainer.collect.http import PoliteSession
from lifetrainer.config import Config

logger = logging.getLogger(__name__)

# arXiv 쿼리 소스 하나당 한 번에 가져올 최대 건수 (API 페이지당 최대 100 이지만
# 피드 폴링 주기마다는 이 정도면 충분하고, 응답 크기/파싱 시간도 아낀다).
ARXIV_FETCH_MAX_RESULTS = 50

# sources.yaml 의 소스별 `respect_robots: false` 를 source.tags 에 심어두는 표식.
# schema.sql 을 건드리지 않고(읽기 전용) 소스 단위 robots 예외를 기억하기 위한 방편이다
# (예: arXiv 공식 API — `lifetrainer/collect/arxiv.py` 모듈 docstring 참고).
ROBOTS_EXEMPT_TAG = "robots-exempt"

_WS_RE = re.compile(r"\s+")


@dataclass
class FeedResult:
    """소스 하나를 한 번 수집한 결과."""

    source_id: int
    name: str
    status: int
    new_docs: int
    dup_docs: int
    error: str | None = None


def _normalize_text(title: str | None, abstract: str | None) -> str:
    """content_hash/simhash 계산용으로 제목+초록을 정규화한다 (소문자, 공백 압축)."""
    combined = f"{title or ''} {abstract or ''}".strip().lower()
    return _WS_RE.sub(" ", combined)


def load_sources(conn: sqlite3.Connection, cfg: Config) -> int:
    """`config/sources.yaml` 을 읽어 `source`(+`interest`) 테이블에 upsert 한다.

    반환값은 upsert 한 source 행 수. 파일이 없거나 파싱에 실패하면 0 을 반환하고
    (계약서 §0: 개별 실패로 전체가 죽으면 안 된다) 로그만 남긴다.
    """
    path = Path(cfg.collect.sources_path)
    if not path.exists():
        logger.warning("소스 설정 파일이 없습니다: %s", path)
        return 0

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        logger.error("sources.yaml 파싱 실패: %s", exc)
        return 0

    now = time.time()
    count = 0
    for entry in data.get("sources") or []:
        try:
            url = str(entry["url"]).strip()
            if not url:
                continue
            kind = str(entry.get("kind", "rss"))
            name = str(entry.get("name", url))
            tags = str(entry.get("tags", ""))
            interval_sec = int(entry.get("interval_sec", 3600))
            respect_robots = bool(entry.get("respect_robots", True))
        except Exception as exc:  # noqa: BLE001
            logger.warning("잘못된 source 항목 무시: %r (%s)", entry, exc)
            continue
        # respect_robots 여부를 tags 에 심어둔다 (schema.sql 변경 없이 소스 단위 예외를
        # 기억하는 방편). yaml 을 다시 로드할 때마다 최신 값으로 맞춰지므로, 나중에
        # respect_robots 를 다시 true 로 되돌리면 이 표식도 자동으로 사라진다.
        tag_list = [t.strip() for t in tags.split(",") if t.strip() and t.strip() != ROBOTS_EXEMPT_TAG]
        if not respect_robots:
            tag_list.append(ROBOTS_EXEMPT_TAG)
        tags = ",".join(tag_list)
        conn.execute(
            "INSERT INTO source(kind, name, url, tags, interval_sec, next_fetch_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, 0, ?) "
            "ON CONFLICT(url) DO UPDATE SET kind=excluded.kind, name=excluded.name, "
            "tags=excluded.tags, interval_sec=excluded.interval_sec",
            (kind, name, url, tags, interval_sec, now),
        )
        count += 1

    for entry in data.get("interests") or []:
        try:
            term = str(entry["term"]).strip().lower()
            weight = float(entry.get("weight", 1.0))
        except Exception as exc:  # noqa: BLE001
            logger.warning("잘못된 interest 항목 무시: %r (%s)", entry, exc)
            continue
        if not term:
            continue
        conn.execute(
            "INSERT INTO interest(term, weight, source, updated_at) VALUES (?, ?, 'manual', ?) "
            "ON CONFLICT(term) DO UPDATE SET weight=excluded.weight, updated_at=excluded.updated_at",
            (term, weight, now),
        )

    return count


def due_sources(conn: sqlite3.Connection, *, now: float | None = None, limit: int = 100) -> list[sqlite3.Row]:
    """지금 수집해야 할 소스 목록 (`next_fetch_at` 도래 + 활성화)."""
    ts = now if now is not None else time.time()
    return conn.execute(
        "SELECT * FROM source WHERE enabled = 1 AND next_fetch_at <= ? "
        "ORDER BY next_fetch_at ASC LIMIT ?",
        (ts, limit),
    ).fetchall()


def upsert_docs(
    conn: sqlite3.Connection,
    *,
    source_id: int | None,
    entries: list[dict],
    kind: str = "article",
    fetched_at: float | None = None,
) -> tuple[int, int]:
    """정규화된 항목 리스트를 `doc` 테이블에 upsert 한다.

    entries 의 각 원소는 {title, url, author, published_at, abstract} 형태를 기대한다.
    `doc.url` 은 UNIQUE 라 이미 있으면 갱신하지 않고 dup 로 센다. 근사 중복(SimHash)은
    새 행으로 넣되 `dup_of` 를 채운다 (뉴스 신디케이션 추적용).
    반환값은 (new_docs, dup_docs).
    """
    ts = fetched_at if fetched_at is not None else time.time()
    new_count = 0
    dup_count = 0

    for entry in entries:
        try:
            url = (entry.get("url") or "").strip()
            title = (entry.get("title") or "").strip()
            if not url or not title:
                continue

            existing = conn.execute("SELECT id FROM doc WHERE url = ?", (url,)).fetchone()
            if existing is not None:
                dup_count += 1
                continue

            abstract = entry.get("abstract")
            normalized = _normalize_text(title, abstract)
            content_hash = dedupe.sha256_hex(normalized)
            simhash = dedupe.simhash64(normalized)
            dup_of = dedupe.is_near_duplicate(conn, simhash)

            conn.execute(
                "INSERT INTO doc(source_id, kind, url, title, author, published_at, fetched_at, "
                "abstract, content_hash, simhash, dup_of, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')",
                (
                    source_id,
                    kind,
                    url,
                    title,
                    entry.get("author"),
                    entry.get("published_at"),
                    ts,
                    abstract,
                    content_hash,
                    simhash,
                    dup_of,
                ),
            )
            new_count += 1
        except Exception as exc:  # noqa: BLE001 - 항목 하나가 깨져도 나머지는 진행
            # entry 자체가 손상돼 .get() 마저 예외를 던질 수 있으니 로깅에서도 방어한다.
            try:
                bad_url = entry.get("url")
            except Exception:  # noqa: BLE001
                bad_url = "<손상된 항목>"
            logger.warning("문서 upsert 실패, 건너뜀 (%r): %s", bad_url, exc)
            continue

    return new_count, dup_count


def _parsed_time_to_epoch(struct_time) -> float | None:
    """feedparser 의 `*_parsed`(UTC struct_time)를 epoch 로. 실패하면 None."""
    if not struct_time:
        return None
    try:
        return float(calendar.timegm(struct_time))
    except Exception:  # noqa: BLE001
        return None


def _normalize_feed_entry(item) -> dict:
    """feedparser 항목 하나를 upsert_docs 가 기대하는 딕셔너리로 정규화한다."""
    title = (getattr(item, "title", "") or "").strip()
    link = (getattr(item, "link", "") or "").strip()
    author = getattr(item, "author", None)
    abstract = getattr(item, "summary", None) or getattr(item, "description", None)
    published_at = _parsed_time_to_epoch(getattr(item, "published_parsed", None))
    if published_at is None:
        published_at = _parsed_time_to_epoch(getattr(item, "updated_parsed", None))
    return {
        "title": title,
        "url": link,
        "author": author,
        "abstract": abstract,
        "published_at": published_at,
    }


def _fetch_feed_entries(session: PoliteSession, url: str) -> tuple[list[dict], int, str | None]:
    """RSS/Atom URL 을 가져와 정규화된 항목 리스트로 파싱한다. (entries, status, error)."""
    result = session.get(url)
    if result.error:
        return [], result.status, result.error
    if result.status == 304 or not result.body:
        return [], result.status, None

    parsed = feedparser.parse(result.body)
    entries: list[dict] = []
    for item in parsed.entries:
        try:
            entries.append(_normalize_feed_entry(item))
        except Exception as exc:  # noqa: BLE001 - 항목 하나가 깨져도 나머지는 진행
            logger.warning("피드 항목 파싱 실패, 건너뜀: %s", exc)
            continue
    return entries, result.status, None


def _source_respects_robots(row: sqlite3.Row) -> bool:
    """`row.tags` 에 `ROBOTS_EXEMPT_TAG` 가 있으면 이 소스는 robots.txt 예외 대상이다.

    `sources.yaml` 의 소스별 `respect_robots: false` 는 `load_sources()` 가 이 태그로
    인코딩해 `source` 테이블에 심어둔다 (schema.sql 변경 없이 소스 단위 정책을 기억하는 방편).
    """
    tags = [t.strip() for t in (row["tags"] or "").split(",") if t.strip()]
    return ROBOTS_EXEMPT_TAG not in tags


def _fetch_arxiv_entries(
    cfg: Config, session: PoliteSession, query: str, *, respect_robots: bool = True
) -> tuple[list[dict], int, str | None]:
    """arXiv API 쿼리(소스 URL 필드에 담긴 query 문자열)를 검색한다.

    `respect_robots` 는 `_source_respects_robots()` 로 얻은 소스별 정책을 그대로 넘겨받는다
    (arXiv 공식 API 소스는 기본적으로 `False` 로 설정되어 있다 — `arxiv.py` 모듈 docstring 참고).
    """
    from lifetrainer.collect import arxiv  # 지연 import: arxiv.py 가 필요시 feeds.py 를 되부르기 때문

    try:
        entries = arxiv.search(
            cfg, session, query, max_results=ARXIV_FETCH_MAX_RESULTS, respect_robots=respect_robots
        )
    except Exception as exc:  # noqa: BLE001
        return [], 0, str(exc)
    return entries, 200, None


def _mark_source_result(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """`PoliteSession` 이 `url_state` 에 남긴 결과를 `source` 행에도 반영한다.

    조건부 GET/적응형 주기/백오프 로직은 http.py 가 url_state 를 대상으로 이미 계산했으므로,
    여기서는 그 계산 결과를 그대로 복사해 due_sources() 의 스케줄링 기준(`source.next_fetch_at`)
    을 갱신한다. 중복 로직을 두 곳에 두지 않기 위한 설계다.
    """
    state = conn.execute("SELECT * FROM url_state WHERE url = ?", (row["url"],)).fetchone()
    now = time.time()
    if state is not None:
        conn.execute(
            "UPDATE source SET last_fetched=?, next_fetch_at=?, etag=?, last_modified=?, "
            "content_hash=?, last_status=?, fail_count=?, unchanged_streak=? WHERE id=?",
            (
                state["last_fetched"],
                state["next_fetch_at"],
                state["etag"],
                state["last_modified"],
                state["content_hash"],
                state["last_status"],
                state["fail_count"],
                state["unchanged_streak"],
                row["id"],
            ),
        )
    else:
        # url_state 를 쓰지 않은 경로(arxiv 등)는 최소한 재시도 시각만 소스 자체 주기로 미룬다.
        fallback_next = now + (row["interval_sec"] or 3600)
        conn.execute(
            "UPDATE source SET last_fetched=?, next_fetch_at=? WHERE id=?",
            (now, fallback_next, row["id"]),
        )


def fetch_source(conn: sqlite3.Connection, cfg: Config, session: PoliteSession, row: sqlite3.Row) -> FeedResult:
    """소스 하나를 수집한다. 실패해도 예외를 올리지 않고 `FeedResult.error` 에 담는다."""
    source_id = row["id"]
    name = row["name"]
    kind = row["kind"]
    url = row["url"]

    if kind == "arxiv":
        entries, status, error = _fetch_arxiv_entries(
            cfg, session, url, respect_robots=_source_respects_robots(row)
        )
    else:
        entries, status, error = _fetch_feed_entries(session, url)

    _mark_source_result(conn, row)

    if error is not None:
        logger.warning("소스 수집 실패 %s(%s): %s", name, url, error)
        return FeedResult(source_id=source_id, name=name, status=status or 0, new_docs=0, dup_docs=0, error=error)

    if status == 304:
        return FeedResult(source_id=source_id, name=name, status=304, new_docs=0, dup_docs=0)

    if not (200 <= status < 300):
        return FeedResult(
            source_id=source_id, name=name, status=status, new_docs=0, dup_docs=0, error=f"http_{status}"
        )

    doc_kind = "paper" if kind == "arxiv" else "article"
    new_docs, dup_docs = upsert_docs(conn, source_id=source_id, entries=entries, kind=doc_kind)
    return FeedResult(source_id=source_id, name=name, status=status, new_docs=new_docs, dup_docs=dup_docs)


def run_once(conn: sqlite3.Connection, cfg: Config, *, now: float | None = None) -> list[FeedResult]:
    """소스 목록을 새로고침하고, 기한이 된 소스를 전부 한 번씩 수집한다.

    소스 하나가 실패해도 나머지는 계속 진행한다 (계약서 §0 실패 처리 원칙).
    """
    load_sources(conn, cfg)
    session = PoliteSession(cfg, conn)
    rows = due_sources(conn, now=now)

    results: list[FeedResult] = []
    for row in rows:
        try:
            result = fetch_source(conn, cfg, session, row)
        except Exception as exc:  # noqa: BLE001 - 소스 하나가 죽어도 나머지는 진행
            logger.error("소스 처리 중 예외, 건너뜀 (%s): %s", row["name"], exc)
            result = FeedResult(
                source_id=row["id"], name=row["name"], status=0, new_docs=0, dup_docs=0, error=str(exc)
            )
        results.append(result)
    return results
