"""관심사 키워드 랭킹 — Phase 2 는 LLM 을 쓰지 않는다.

`interest` 테이블(term/weight)로 제목·초록의 매칭 가중합을 계산한다.
제목 매칭에는 더 큰 가중치를 준다.

★ **`doc.score` 는 시간이 안 들어간 점수다** (2026-09-04 부터). 최신성 보정은
  **읽는 시점에** `top_docs` 가 곱한다. 전에는 채점할 때 곱해서 저장했는데,
  그러면 **보정이 채점 순간에 굳어** 문서가 늙어도 점수가 안 내려간다 —
  아침 다이제스트가 8일 연속 같은 문서를 보냈다.
  경위: HISTORY/2026-09-04-the-decay-was-frozen-into-the-score.md
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
            # ★ 최신성 보정을 **여기서 곱하지 않는다.** 곱해서 저장하면 그 값이
            #   채점 순간의 나이로 굳는다 (모듈 머리말).
            base = score_doc(row["title"], row["abstract"], interests)
            conn.execute(
                "UPDATE doc SET score = ?, scored_at = ?, state = 'scored' WHERE id = ?",
                (base, now, row["id"]),
            )
            processed += 1
        except Exception as exc:  # noqa: BLE001 - 문서 하나 실패로 전체가 죽지 않게
            logger.warning("문서 스코어링 실패, 건너뜀 (id=%s): %s", row["id"], exc)
            continue
    return processed


def rescore_all(conn, cfg, *, limit: int | None = None) -> int:
    """이미 채점된 문서의 `score` 를 **시간이 안 들어간 기준점수**로 다시 계산한다.

    2026-09-04 이전에는 `score` 에 최신성 보정이 곱해져 저장됐다. 그 값은 채점 순간의
    나이로 굳어 있어 지금 랭킹에 쓰면 **옛 문서가 부당하게 높다** — 8,689건이 그 상태였다.
    한 번 돌려서 씻어낸다.

    ★ `state` 를 건드리지 않는다. `state='new'` 로 되돌리면 요약이 끝난 1,896건이
      파이프라인 앞으로 되돌아간다. 여기서 바꾸는 것은 **점수 하나**다.

    ★ 다시 돌려도 안전하다(멱등). 같은 제목·초록·관심사로 같은 값이 나온다.
    """
    interests = load_interests(conn, cfg)
    if not interests:
        logger.warning("관심사가 비어 있어 재채점을 건너뛴다 (전부 0 점이 될 뻔했다)")
        return 0

    sql = "SELECT id, title, abstract FROM doc WHERE state != 'new' ORDER BY id"
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)

    processed = 0
    for row in conn.execute(sql, params).fetchall():
        try:
            conn.execute(
                "UPDATE doc SET score = ? WHERE id = ?",
                (score_doc(row["title"], row["abstract"], interests), row["id"]),
            )
            processed += 1
        except Exception as exc:  # noqa: BLE001 - 문서 하나 실패로 전체가 죽지 않게
            logger.warning("문서 재채점 실패, 건너뜀 (id=%s): %s", row["id"], exc)
            continue
    return processed


def top_docs(
    conn,
    *,
    day: str | None = None,
    limit: int = 3,
    min_score: float = 0.0,
    now: float | None = None,
    include_digested: bool = False,
) -> list[sqlite3.Row]:
    """랭킹 상위 문서를 반환한다. 반환 행에는 `ranked_score` 가 붙는다.

    **랭킹 = `score`(관심사 매칭) × 최신성 보정(지금 기준).** 보정을 여기서 곱하는 것이
    핵심이다 — 저장된 값에 굳혀 두면 문서가 늙어도 순위가 안 내려간다
    (모듈 머리말 · HISTORY 2026-09-04).

    **한 번 나간 문서는 다시 안 나온다** (`digested_at IS NULL`). 최신성만으로는
    부족하다 — 반감기가 14일이라 1등이 2주 동안 1등이다. 그건
    *"이미 아는 것을 매일 다시 알리는"* 부류다 ([저장소 규칙 §1](../../../CLAUDE.md) 4번).
    `include_digested=True` 면 이 제외를 끈다 (미리보기·테스트용).

    ★ `min_score` 는 **보정 전 `score`** 에 건다. 그건 "관심사에 얼마나 걸리나" 라는
      시간과 무관한 값이고, 나이 때문에 관심사 문턱을 못 넘는 것은 다른 이야기다.

    `day`('YYYY-MM-DD')가 주어지면 그 날짜(UTC 기준 `fetched_at`)로 제한한다.
    (이 함수는 `cfg`/타임존 정보를 받지 않으므로 UTC 자정을 경계로 쓴다 — 호출부에서
    로컬 날짜 문자열을 그대로 넘기면 경계가 몇 시간 어긋날 수 있음을 감안한다.)
    """
    now = time.time() if now is None else now
    # 최신성 보정을 SQL 안에서 쓴다 — 후보를 파이썬으로 다 끌어와 정렬하면
    # 문서가 늘수록 비용이 문서 수에 비례한다. 정렬은 SQLite 가 한다.
    conn.create_function("lt_recency", 1, lambda p: _recency_factor(p, now))

    where = ["state != 'new'", "score >= ?"]
    params: list = [min_score]
    if not include_digested:
        where.append("digested_at IS NULL")
    if day is not None:
        d = date.fromisoformat(day)
        day_start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        where.append("fetched_at >= ? AND fetched_at < ?")
        params += [day_start.timestamp(), (day_start + timedelta(days=1)).timestamp()]

    params.append(limit)
    return conn.execute(
        "SELECT *, score * lt_recency(published_at) AS ranked_score FROM doc "
        "WHERE " + " AND ".join(where) + " ORDER BY ranked_score DESC, id DESC LIMIT ?",
        params,
    ).fetchall()


def mark_digested(conn, doc_ids, *, now: float | None = None) -> int:
    """다이제스트로 **실제 나간** 문서에 표식을 남긴다. 반환값은 표시한 건수.

    ★ 부르는 곳은 발송에 성공한 뒤여야 한다. 만들어만 보고(`--post` 없이) 표시하면
      사람이 미리보기 한 번 돌린 것만으로 그 문서가 영영 안 나간다.
    """
    now = time.time() if now is None else now
    ids = [int(d) for d in doc_ids]
    if not ids:
        return 0
    conn.executemany(
        "UPDATE doc SET digested_at = ? WHERE id = ? AND digested_at IS NULL",
        [(now, i) for i in ids],
    )
    return len(ids)
