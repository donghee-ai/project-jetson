"""근거 텍스트 백필 — 제목밖에 없는 문서에 초록을 채운다.

## 왜 필요한가

문서 4,431건 중 **785건(17.7%)이 제목뿐**이다. 초록도 요약도 없다.
이 문서들은 두 곳에서 동시에 손해를 본다:

- `search_docs` 가 근거로 싣는 것이 `summary or abstract` 라, 모델에게 제목만 간다
- 임베딩 원문이 `title + abstract + summary` 라, 벡터도 제목 한 줄로 만들어진다

즉 **검색에 걸리기는 하는데 근거가 없다.** 상위에 올라올수록 나쁘다.

## 왜 피드에서 못 받았나

Hugging Face Blog 가 662건으로 84% 를 차지하는데, 그 피드(`feed.xml`)는
`title · link · published` 만 준다 — 초록 필드 자체가 없다. 그래서 페이지를 받아야 한다.

## 어떻게 뽑나 — 사이트별 특례를 만들지 않는다

`<meta description>` 은 못 쓴다. HF 는 모든 글이 "We're on a journey to advance and
democratize artificial intelligence…" 라는 **사이트 공통 문구**를 돌려준다.

본문에서 **문장처럼 생긴 줄만** 고른다. 네비게이션("Log In")·목차("Benchmark results")·
저자줄은 짧거나 마침표가 없거나 띄어쓰기가 적다. 규칙 하나로 HF·OpenAI·DeepMind 가
모두 깨끗하게 나온다 — 사이트마다 선택자를 두면 그 사이트가 개편될 때마다 깨진다.

## 쓰고 나면

`doc.abstract` 를 UPDATE 하면 `doc_au` 트리거가 FTS 색인을 갱신하고,
`embed.source_hash` 가 달라지므로 다음 임베딩 배치가 **알아서 다시 벡터를 만든다.**
따로 무효화할 것이 없다.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lifetrainer.config import Config

logger = logging.getLogger(__name__)

MIN_ABSTRACT_CHARS = 120  # 이보다 짧으면 안 쓴다 — 제목만 있는 것보다 나쁠 수 있다
MAX_ABSTRACT_CHARS = 700  # 툴 결과는 220자만 싣는다. 임베딩 여유까지 보고 700
_SENT_END = re.compile(r"[.!?。？！]\s*$")
_WS = re.compile(r"\s+")


# 연속으로 이만큼 429 를 맞으면 배치를 멈춘다.
#
# ★ 실측: 785건을 도메인당 2초 간격으로 돌렸더니 600건째부터 Hugging Face 가
# 429 를 주기 시작했고, 코드가 그걸 그냥 "실패" 로 세며 **184번을 더 두드렸다.**
# 상대가 그만하라고 말하는데 계속 치는 것은 이 프로젝트가 robots.txt 를 지키는
# 이유와 정면으로 어긋난다. 남은 것은 다음 실행에서 이어 하면 된다 —
# `pending()` 이 아직 빈 문서만 고르므로 재실행이 곧 이어하기다.
_MAX_CONSECUTIVE_429 = 5


@dataclass
class BackfillResult:
    filled: int = 0
    too_short: int = 0
    failed: int = 0
    boilerplate: int = 0
    stopped_early: bool = False  # 429 로 물러났다


def prose(text: str, *, max_chars: int = MAX_ABSTRACT_CHARS, min_line: int = 70) -> str:
    """본문에서 **문장처럼 생긴 줄만** 이어 붙인다.

    세 조건을 모두 통과해야 한다:
      ① 길이 `min_line` 이상 — 메뉴·목차는 짧다
      ② 마침표를 담고 있음 — 제목·헤딩에는 없다
      ③ 띄어쓰기 6개 이상 — 링크를 죽 나열한 줄을 막는다
    """
    picked: list[str] = []
    total = 0
    for line in (text or "").splitlines():
        line = line.strip()
        if len(line) < min_line or "." not in line or line.count(" ") < 6:
            continue
        picked.append(line)
        total += len(line)
        if total >= max_chars:
            break
    return _WS.sub(" ", " ".join(picked)).strip()[:max_chars]


def pending(conn: sqlite3.Connection, limit: int, *, source: str | None = None) -> list[sqlite3.Row]:
    """근거가 없는 문서. **점수 상위부터** — 중간에 멈춰도 값어치 있는 것부터 채워진다."""
    sql = (
        "SELECT d.id, d.url, d.title, d.source_id FROM doc d "
        "LEFT JOIN source s ON s.id = d.source_id "
        "WHERE d.dup_of IS NULL AND d.url IS NOT NULL AND d.url <> '' "
        "AND (d.abstract IS NULL OR trim(d.abstract) = '') "
        "AND (d.summary IS NULL OR trim(d.summary) = '') "
    )
    params: list = []
    if source:
        sql += "AND s.name = ? "
        params.append(source)
    sql += "ORDER BY d.score DESC, d.id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def _flush(
    conn: sqlite3.Connection, staged: list[tuple[int, str]], result: BackfillResult, dry_run: bool
) -> None:
    """모아 둔 것을 판정해 쓴다. **보일러플레이트 판정은 이 묶음 안에서만 한다.**

    사이트 공통 문구는 같은 소스의 글이라면 어느 50건을 봐도 반복되므로 창 하나로
    충분하다. 대신 창을 넘어가는 반복은 못 잡는데, 그 대가로 **중간에 끊겨도
    여기까지가 남는다** — 785건이 40분짜리 배치라 이쪽이 훨씬 중요하다.
    """
    seen: dict[str, int] = {}
    for _, abstract in staged:
        seen[abstract] = seen.get(abstract, 0) + 1
    for doc_id, abstract in staged:
        if seen[abstract] > 1:
            result.boilerplate += 1
            continue
        result.filled += 1
        if not dry_run:
            with conn:
                conn.execute("UPDATE doc SET abstract = ? WHERE id = ?", (abstract, doc_id))
    staged.clear()


def backfill(
    conn: sqlite3.Connection,
    cfg: "Config",
    *,
    limit: int = 50,
    source: str | None = None,
    dry_run: bool = False,
    flush_every: int = 50,
    progress=None,
) -> BackfillResult:
    """제목뿐인 문서에 초록을 채운다. `PoliteSession` 을 통과하므로 robots·레이트리밋을 지킨다.

    ★ **같은 문구가 여러 문서에 들어가면 보일러플레이트로 보고 버린다.** 사이트 공통
    안내문을 초록으로 심으면 그 소스 전체가 서로 비슷해져 검색이 무너진다 — 없느니만
    못하다.

    ★ **`flush_every` 건마다 커밋한다.** 도메인당 2초 간격이라 785건이 40분짜리
    배치다. 끝에 한 번만 쓰면 중간에 끊길 때 40분이 통째로 날아간다.
    """
    from lifetrainer.collect.http import PoliteSession
    from lifetrainer.llm.webfetch import html_to_text

    result = BackfillResult()
    rows = pending(conn, limit, source=source)
    if not rows:
        return result

    session = PoliteSession(cfg, conn)
    staged: list[tuple[int, str]] = []
    consecutive_429 = 0

    for i, row in enumerate(rows, 1):
        if progress:
            progress(i, len(rows), row["title"])
        res = session.get(row["url"], use_state=False)
        if res.status == 429:
            consecutive_429 += 1
            result.failed += 1
            if consecutive_429 >= _MAX_CONSECUTIVE_429:
                logger.warning(
                    "429 가 %d번 이어져 물러납니다 (%d/%d 처리). 다음 실행이 이어 합니다.",
                    consecutive_429, i, len(rows),
                )
                result.stopped_early = True
                break
            continue
        consecutive_429 = 0
        if res.error or res.status != 200 or not res.text:
            logger.info("초록 백필 실패 doc=%s (%s): %s", row["id"], res.status, res.error)
            result.failed += 1
        else:
            try:
                _, text = html_to_text(res.text)
            except Exception as exc:  # noqa: BLE001 - 한 건 때문에 배치를 멈추지 않는다
                logger.info("본문 추출 실패 doc=%s: %s", row["id"], exc)
                result.failed += 1
            else:
                abstract = prose(text)
                if len(abstract) < MIN_ABSTRACT_CHARS:
                    result.too_short += 1
                else:
                    staged.append((row["id"], abstract))

        if len(staged) >= flush_every:
            _flush(conn, staged, result, dry_run)

    _flush(conn, staged, result, dry_run)

    logger.info(
        "초록 백필: %d건 채움 · %d건 너무 짧음 · %d건 보일러플레이트 · %d건 실패%s",
        result.filled, result.too_short, result.boilerplate, result.failed,
        " (429 로 중단)" if result.stopped_early else "",
    )
    return result
