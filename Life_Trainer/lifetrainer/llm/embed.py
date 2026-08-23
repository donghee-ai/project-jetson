"""문서 임베딩 — 수집해 둔 글을 벡터로 만들어 두는 계층.

## 왜 별도 서버인가

llama.cpp 는 **프로세스당 모델 하나**다. 대화용 8B(:8080)와 임베딩용 0.6B(:8081)를
같이 못 올린다. 0.6B Q8_0 이 약 640MB 라 8B(10.2GB) 옆에 상주시켜도 가용 메모리
안에 든다 — 실측 여유 5GB.

## 없으면 어떻게 되나

**검색이 죽지 않는다.** 임베딩 서버가 꺼져 있거나 벡터가 없는 문서는 하이브리드
검색에서 키워드(FTS5) 쪽 점수만으로 순위가 매겨진다. RAG 는 있으면 좋아지는
것이지 없으면 못 쓰는 것이 아니어야 한다 — 이 기기는 메모리가 빠듯해서 임베딩
서버를 내려야 할 날이 온다.

## 무엇을 임베딩하나

`title + abstract + summary` — `doc_fts` 가 색인하는 것과 **같은 세 칼럼**이다.
둘이 다른 것을 보면 "키워드로는 찾히는데 벡터로는 안 찾히는" 문서가 생기고,
그 차이는 디버깅할 방법이 없다. 본문은 아직 저장하지 않는다(`doc.body_path` 전부 NULL).

요약이 나중에 채워지므로 원문이 바뀔 수 있다. `source_hash` 로 그것을 감지해
바뀐 것만 다시 만든다 — 4천 건을 매번 다시 도는 것은 야간 배치 예산 낭비다.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import struct
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lifetrainer.config import Config

logger = logging.getLogger(__name__)


class EmbedError(Exception):
    """임베딩 실패. 호출부가 문장으로 바꾸거나 조용히 건너뛴다."""


class EmbedUnavailable(EmbedError):
    """서버에 닿지 못했다. 꺼져 있는 것과 구분한다 — 재시도할 가치가 있다."""


@dataclass
class EmbedResult:
    embedded: int = 0
    skipped: int = 0
    failed: int = 0


def source_text(row, max_chars: int) -> str:
    """임베딩할 원문. `doc_fts` 와 **같은 세 칼럼**을 같은 순서로 잇는다."""
    parts = [row["title"] or "", row["abstract"] or "", row["summary"] or ""]
    text = "\n".join(p.strip() for p in parts if p and p.strip())
    return text[:max_chars]


def source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def pack(vec: list[float]) -> bytes:
    """float32 리틀엔디언 raw. numpy 로 한 번에 읽으려고 이 형식으로 고정한다."""
    return struct.pack(f"<{len(vec)}f", *vec)


def embed_texts(cfg: "Config", texts: list[str]) -> list[list[float]]:
    """텍스트 여러 개를 한 번에 벡터로. OpenAI 호환 `/embeddings` 를 쓴다."""
    if not texts:
        return []
    import requests

    url = cfg.embed.base_url.rstrip("/") + "/embeddings"
    try:
        resp = requests.post(
            url,
            json={"model": cfg.embed.model, "input": texts},
            timeout=cfg.embed.timeout_sec,
        )
    except requests.RequestException as exc:
        raise EmbedUnavailable(f"임베딩 서버에 연결할 수 없습니다 ({url}): {exc}") from exc

    if resp.status_code != 200:
        raise EmbedError(f"임베딩 실패 (HTTP {resp.status_code}): {resp.text[:200]}")

    data = (resp.json() or {}).get("data") or []
    if len(data) != len(texts):
        raise EmbedError(f"임베딩 개수가 맞지 않습니다: 요청 {len(texts)} → 응답 {len(data)}")
    # 서버가 순서를 섞어 줄 수 있으므로 index 로 되돌린다.
    out: list[list[float]] = [[] for _ in texts]
    for item in data:
        out[int(item.get("index", 0))] = [float(x) for x in item["embedding"]]
    return out


def embed_query(cfg: "Config", query: str) -> list[float] | None:
    """질의어 하나를 벡터로. 실패하면 None — 검색은 키워드로 계속 간다."""
    if not cfg.embed.enabled or not query.strip():
        return None
    try:
        vecs = embed_texts(cfg, [query[: cfg.embed.max_chars]])
    except EmbedError as exc:
        logger.warning("질의 임베딩 실패 (키워드 검색으로 계속): %s", exc)
        return None
    return vecs[0] if vecs else None


def pending_docs(conn: sqlite3.Connection, cfg: "Config", limit: int) -> list[sqlite3.Row]:
    """아직 벡터가 없거나 **원문이 바뀐** 문서.

    `dup_of` 가 있는 중복 문서는 건너뛴다 — 검색에서 어차피 제외된다.

    ★ 해시 비교는 SQL 이 아니라 파이썬에서 한다. 원문은 `title + abstract + summary`
    를 이어 붙여 자른 것이라 SQL 로는 같은 문자열을 만들 수 없다 — 만들려고 하면
    `source_text()` 규칙이 두 벌이 되고(반복 실패 2번) 조용히 어긋난다.
    그래서 후보를 넓게 뽑아 와서 한 곳에서 판정한다.

    **요약이 나중에 채워지므로 이 비교가 핵심이다.** 없으면 야간 요약이 붙은 문서가
    옛 벡터를 그대로 달고 있어, 검색이 요약을 못 본다.
    """
    rows = conn.execute(
        """
        SELECT d.id, d.title, d.abstract, d.summary,
               e.source_hash AS have_hash, e.model AS have_model
        FROM doc d
        LEFT JOIN doc_embedding e ON e.doc_id = d.id
        WHERE d.dup_of IS NULL
        ORDER BY d.score DESC, d.id DESC
        """
    ).fetchall()

    out: list[sqlite3.Row] = []
    for r in rows:
        if r["have_model"] == cfg.embed.model and r["have_hash"] == source_hash(
            source_text(r, cfg.embed.max_chars)
        ):
            continue  # 모델도 원문도 그대로다
        out.append(r)
        if len(out) >= limit:
            break
    return out


def embed_pending(
    conn: sqlite3.Connection, cfg: "Config", *, limit: int = 500, now: float | None = None
) -> EmbedResult:
    """벡터가 없는 문서를 배치로 채운다. 야간 배치가 부른다.

    한 배치가 실패해도 **다음 배치를 계속 시도한다** — 문서 하나 때문에 4천 건이
    멈추면 안 된다. 서버 자체가 죽었으면(`EmbedUnavailable`) 그때는 멈춘다.
    """
    result = EmbedResult()
    if not cfg.embed.enabled:
        return result

    rows = pending_docs(conn, cfg, limit)
    if not rows:
        return result

    ts = now if now is not None else time.time()
    batch = max(1, cfg.embed.batch_size)

    for start in range(0, len(rows), batch):
        chunk = rows[start : start + batch]
        texts, keep = [], []
        for r in chunk:
            text = source_text(r, cfg.embed.max_chars)
            if not text:
                result.skipped += 1
                continue
            texts.append(text)
            keep.append((r["id"], source_hash(text)))

        if not texts:
            continue
        try:
            vecs = embed_texts(cfg, texts)
        except EmbedUnavailable:
            raise
        except EmbedError as exc:
            logger.warning("임베딩 배치 실패 (%d건 건너뜀): %s", len(texts), exc)
            result.failed += len(texts)
            continue

        with conn:
            for (doc_id, shash), vec in zip(keep, vecs):
                if len(vec) != cfg.embed.dim:
                    logger.warning(
                        "차원 불일치 doc=%s: %d (설정 %d) — 건너뛴다", doc_id, len(vec), cfg.embed.dim
                    )
                    result.failed += 1
                    continue
                conn.execute(
                    "INSERT INTO doc_embedding(doc_id, model, dim, vec, source_hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(doc_id) DO UPDATE SET model=excluded.model, dim=excluded.dim, "
                    "vec=excluded.vec, source_hash=excluded.source_hash, created_at=excluded.created_at",
                    (doc_id, cfg.embed.model, len(vec), pack(vec), shash, ts),
                )
                result.embedded += 1

    logger.info(
        "임베딩 완료: %d건 생성 · %d건 건너뜀 · %d건 실패",
        result.embedded, result.skipped, result.failed,
    )
    return result


def nearest(
    conn: sqlite3.Connection,
    cfg: "Config",
    query_vec: list[float],
    limit: int,
    *,
    allowed_ids: set[int] | None = None,
) -> list[tuple[int, float]]:
    """코사인 유사도 상위 N. `(doc_id, 점수)`.

    ★ 확장(sqlite-vec) 없이 numpy 로 전수 계산한다. 4천 건 × 1024차원 = 16MB 라
    한 번에 올려 곱해도 밀리초다. 10만 건을 넘기면 그때 색인을 고민한다.
    벡터는 서버가 이미 정규화해서 주므로(`--embd-normalize 2` 기본) 내적이 곧 코사인이다.

    `allowed_ids` 는 기간 필터가 준 후보다. **SQL 의 IN 절로 좁히지 않는다** — 4천 건에
    파라미터 수천 개를 밀어 넣는 것보다 전부 읽어 numpy 마스크로 거르는 쪽이 싸고,
    무엇보다 코드 경로가 하나로 남는다(필터 있는 검색과 없는 검색이 갈라지지 않는다).
    """
    import numpy as np

    rows = conn.execute(
        "SELECT doc_id, vec FROM doc_embedding WHERE model = ? AND dim = ?",
        (cfg.embed.model, cfg.embed.dim),
    ).fetchall()
    if not rows:
        return []

    ids = np.fromiter((r["doc_id"] for r in rows), dtype=np.int64, count=len(rows))
    mat = np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float32).reshape(
        len(rows), cfg.embed.dim
    )
    q = np.asarray(query_vec, dtype=np.float32)
    norm = float(np.linalg.norm(q))
    if norm > 0:
        q = q / norm

    scores = mat @ q
    if allowed_ids is not None:
        # 후보 밖은 -inf 로 눌러 argsort 가 절대 못 고르게 한다.
        mask = np.fromiter((int(i) in allowed_ids for i in ids), dtype=bool, count=len(ids))
        scores = np.where(mask, scores, -np.inf)
        if not mask.any():
            return []
    order = np.argsort(-scores)[:limit]
    return [(int(ids[i]), float(scores[i])) for i in order if np.isfinite(scores[i])]
