"""관심사 키워드 랭킹 — Phase 2 는 LLM 을 쓰지 않는다.

`interest` 테이블(term/weight)로 제목·초록의 매칭 가중합을 계산한다.
제목 매칭에는 더 큰 가중치를 주고, 오래된 문서는 최신성 보정으로 감점한다.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[0-9a-zA-Z가-힣]+")

# 제목에 매칭되면 초록 매칭보다 이만큼 더 가중치를 준다.
TITLE_WEIGHT_MULTIPLIER = 2.0
# 최신성 보정 반감기(일). 이 기간마다 스코어가 절반이 된다.
RECENCY_HALF_LIFE_DAYS = 14.0
# 아무리 오래돼도 완전히 0 으로 죽이지는 않는다 (최소 배율).
RECENCY_MIN_FACTOR = 0.2


def load_interests(conn, cfg) -> dict[str, float]:
    """`interest` 테이블에서 term(소문자) -> weight 맵을 읽는다.

    `config/sources.yaml` 의 `interests:` 섹션은 `feeds.load_sources()` 가 이미
    `interest` 테이블에 적재했다고 가정한다 (수집 파이프라인은 `lt collect` 가
    `load_sources` 를 먼저 돌린 뒤 스코어링으로 이어진다).
    """
    rows = conn.execute("SELECT term, weight FROM interest").fetchall()
    return {str(row["term"]).strip().lower(): float(row["weight"]) for row in rows if row["term"]}


def _tokenize(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text)}


def score_doc(title: str, abstract: str | None, interests: dict[str, float]) -> float:
    """제목·초록에 대한 관심사 키워드 가중합. 제목 매칭이 더 크게 반영된다.

    단어 하나짜리 term 은 토큰 집합으로, 여러 단어(예: 'on-device')는 부분 문자열로 매칭한다.
    """
    if not interests:
        return 0.0
    title = title or ""
    title_lower = title.lower()
    abstract_lower = (abstract or "").lower()
    title_tokens = _tokenize(title)
    abstract_tokens = _tokenize(abstract or "")

    score = 0.0
    for term, weight in interests.items():
        term_norm = term.strip().lower()
        if not term_norm:
            continue
        in_title = term_norm in title_tokens or term_norm in title_lower
        in_abstract = term_norm in abstract_tokens or term_norm in abstract_lower
        if in_title:
            score += weight * TITLE_WEIGHT_MULTIPLIER
        if in_abstract:
            score += weight
    return score


def _recency_factor(published_at: float | None, now: float) -> float:
    """오래된 문서를 감점한다. `published_at` 이 없으면 보정하지 않는다(1.0)."""
    if published_at is None:
        return 1.0
    age_days = max(0.0, (now - published_at) / 86400.0)
    decay = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
    return max(RECENCY_MIN_FACTOR, decay)


def score_pending(conn, cfg, *, limit: int = 500) -> int:
    """`state='new'` 인 문서를 스코어링해 `score`/`scored_at`/`state` 를 채운다.

    반환값은 실제로 처리한 문서 수. 문서 하나가 실패해도 나머지는 계속 진행한다
    (계약서 §0 실패 처리 원칙).
    """
    interests = load_interests(conn, cfg)
    now = time.time()
    rows = conn.execute(
        "SELECT id, title, abstract, published_at FROM doc WHERE state = 'new' "
        "ORDER BY fetched_at DESC LIMIT ?",
        (limit,),
    ).fetchall()

    processed = 0
    for row in rows:
        try:
            base = score_doc(row["title"], row["abstract"], interests)
            final = base * _recency_factor(row["published_at"], now)
            conn.execute(
                "UPDATE doc SET score = ?, scored_at = ?, state = 'scored' WHERE id = ?",
                (final, now, row["id"]),
            )
            processed += 1
        except Exception as exc:  # noqa: BLE001 - 문서 하나 실패로 전체가 죽지 않게
            logger.warning("문서 스코어링 실패, 건너뜀 (id=%s): %s", row["id"], exc)
            continue
    return processed


def top_docs(conn, *, day: str | None = None, limit: int = 3, min_score: float = 0.0) -> list[sqlite3.Row]:
    """스코어 상위 문서를 반환한다.

    `day`('YYYY-MM-DD')가 주어지면 그 날짜(UTC 기준 `fetched_at`)로 제한한다.
    (이 함수는 `cfg`/타임존 정보를 받지 않으므로 UTC 자정을 경계로 쓴다 — 호출부에서
    로컬 날짜 문자열을 그대로 넘기면 경계가 몇 시간 어긋날 수 있음을 감안한다.)
    """
    if day is not None:
        d = date.fromisoformat(day)
        day_start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        start = day_start.timestamp()
        end = (day_start + timedelta(days=1)).timestamp()
        return conn.execute(
            "SELECT * FROM doc WHERE state != 'new' AND score >= ? "
            "AND fetched_at >= ? AND fetched_at < ? ORDER BY score DESC LIMIT ?",
            (min_score, start, end, limit),
        ).fetchall()

    return conn.execute(
        "SELECT * FROM doc WHERE state != 'new' AND score >= ? ORDER BY score DESC LIMIT ?",
        (min_score, limit),
    ).fetchall()
