"""본문 수집 — 초록이 너무 짧아 답의 재료가 안 되는 문서만 골라 전문을 받는다.

## 왜 "전부"가 아닌가

[`docs/rag-plan.md`](../../docs/rag-plan.md) §7 은 **문서 전문 수집을 안 하기로** 결정하면서
조건을 달아 뒀다 — *"초록으로 부족하다는 증거가 나오면 그때."* 2026-09-04 에 쟀다.

**검색은 병목이 아니었다.** 엔티티 질의 10개에서 하이브리드+접두가 `hit@5` 100% 다
(`scripts/eval_search.py`). 못 찾는 게 아니다.

**모자란 것은 찾은 문서 안의 내용이고, 그건 소스마다 다르다:**

    소스                     문서    초록 길이 중앙값
    arXiv cs.*              3,182       1,400자대   ← 초록이 원래 초록이다. 충분하다
    Hugging Face Blog         861         700자
    Hacker News                1,109         322자   ← 링크 모음이라 남의 글 한 줄이다
    GeekNews                  583         215자
    OpenAI News             1,171         157자   ← 제목 다시 쓴 수준

전체의 **29%(2,446건)가 300자 미만**이다. 한 문단도 안 된다.
**arXiv 까지 전문을 받으면 얻는 것 없이 robots·용량·요약 비용만 는다.**
그래서 짧은 것만 받는다. §7 의 결정을 뒤집는 게 아니라 그 조건절을 실행하는 것이다.

## 무엇을 안 하나

- **재시도를 안 쌓는다.** 실패는 `body_fetch_failed_at` 에 시각만 남기고 다음 문서로 간다.
  같은 문서를 매번 다시 두드리면 그건 남의 서버에 대고 도는 루프다
- **타이머를 안 만든다.** 지금은 손으로 부른다 (`lt bodies --limit N`).
  받은 본문이 실제로 답을 낫게 하는지 보기 전에 자동으로 돌리면,
  *만들었다 ≠ 그게 값을 한다* 를 확인할 기회가 사라진다
- **청킹을 안 한다.** 그건 rag-plan 5단계고, 재료가 쌓인 다음이다
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# 이 길이 미만이면 "답의 재료로는 모자라다" 고 본다.
# 300자 = 한 문단. 실측 분포에서 arXiv(1,400자대)와 뉴스 요약(157~322자)이 이 선에서 갈린다.
SHORT_ABSTRACT_CHARS = 300

# 받은 본문이 이보다 짧으면 저장하지 않는다. 쿠키 배너·"자바스크립트를 켜세요" 같은
# 껍데기가 이 길이로 온다 — 저장하면 초록보다 나쁜 것으로 초록을 덮는 셈이다.
MIN_BODY_CHARS = 400

# 본문 상한. 넘으면 잘라서 저장한다 (2MB 게이트는 http 계층이 이미 갖고 있다).
MAX_BODY_CHARS = 60_000


@dataclass
class BodyResult:
    fetched: int = 0
    skipped_robots: int = 0
    too_thin: int = 0
    failed: int = 0


def bodies_dir(cfg) -> Path:
    """본문 저장 위치. DB 에는 경로만 넣는다 — 텍스트를 행에 넣으면 doc 테이블이 무거워진다."""
    return Path(cfg.data_dir) / "bodies"


def pending_docs(conn, *, limit: int = 50, min_score: float = 0.0):
    """본문이 필요한 문서. **짧은 초록**이면서 아직 안 받아 본 것만.

    점수 높은 것부터 준다 — 다 받을 생각이 없으니 순서가 곧 정책이다.
    `score` 는 시간이 안 들어간 기준점수라 어제 돌린 것과 오늘 돌린 것의 순서가
    흔들리지 않는다 (`collect/score.py` 머리말).
    """
    return conn.execute(
        """
        SELECT id, url, title, abstract FROM doc
        WHERE body_path IS NULL
          AND body_fetch_failed_at IS NULL
          AND state != 'new'
          AND score >= ?
          AND (abstract IS NULL OR length(abstract) < ?)
        ORDER BY score DESC, id DESC
        LIMIT ?
        """,
        (min_score, SHORT_ABSTRACT_CHARS, limit),
    ).fetchall()


def _store(cfg, doc_id: int, text: str) -> Path:
    out_dir = bodies_dir(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{doc_id}.txt"
    path.write_text(text[:MAX_BODY_CHARS], encoding="utf-8")
    return path


def collect_bodies(conn, cfg, *, limit: int = 50, session=None) -> BodyResult:
    """초록이 짧은 문서의 전문을 받아 파일로 저장하고 `doc.body_path` 를 채운다.

    `session` 을 주면 그것을 쓴다 (테스트가 네트워크 없이 돌기 위한 자리다).
    안 주면 `PoliteSession` 을 연다 — robots·도메인별 레이트리밋·조건부 GET 이
    거기 있고, **본문 수집이 그 규칙 밖으로 나가면 안 된다.**

    문서 하나가 실패해도 나머지는 계속한다 (계약서 §0).
    """
    from lifetrainer.collect.http import PoliteSession
    from lifetrainer.llm.webfetch import html_to_text

    session = PoliteSession(cfg, conn) if session is None else session
    result = BodyResult()

    for row in pending_docs(conn, limit=limit):
        now = time.time()
        try:
            # ★ `use_state=False` — 조건부 GET 의 ETag 는 **수집기가 피드를 볼 때** 쓰는
            #   상태다. 본문을 처음 받는 자리에서 304 를 받으면 본문 없이 끝난다.
            res = session.get(row["url"], use_state=False)
        except Exception as exc:  # noqa: BLE001 - 한 건 실패로 배치가 죽지 않게
            logger.warning("본문 수집 실패 (id=%s): %s", row["id"], exc)
            res = None

        if res is None or res.error or res.status != 200 or not res.text:
            if res is not None and res.error == "robots_disallowed":
                result.skipped_robots += 1
            else:
                result.failed += 1
            # 실패를 안 적으면 다음 배치가 같은 문서를 또 두드린다.
            conn.execute("UPDATE doc SET body_fetch_failed_at = ? WHERE id = ?", (now, row["id"]))
            continue

        _, text = html_to_text(res.text)
        text = (text or "").strip()
        if len(text) < MIN_BODY_CHARS:
            # 껍데기다 (쿠키 배너·"자바스크립트를 켜세요"). 초록을 이걸로 덮지 않는다.
            result.too_thin += 1
            conn.execute("UPDATE doc SET body_fetch_failed_at = ? WHERE id = ?", (now, row["id"]))
            continue

        path = _store(cfg, int(row["id"]), text)
        conn.execute("UPDATE doc SET body_path = ? WHERE id = ?", (str(path), row["id"]))
        result.fetched += 1

    return result
