"""야간 배치 적재 — 만들어 둔 GPU 핸들러를 실제로 돌리는 유일한 진입점.

## 왜 이 파일이 생겼나

`summarize_doc`·`tag_activity` 핸들러는 Phase 3(08-16)에 만들어졌는데 **큐에 넣는
곳이 없었다.** `enqueue()` 호출부가 온 저장소에 `llm/tools.py` 한 곳(예약 알림)뿐이라,
상시 워커가 설치 이후 실질적으로 놀았다 — 문서 2,976건 중 요약 0건, LLM 태깅 0건
(`docs/rag-plan.md §0`). 임베딩을 얹기 전에 이것부터 돌아야 비교 기준이 생긴다.

## 설계

- **새 프로세스를 띄우지 않는다.** 여기서는 큐에 넣기만 하고, 처리는 이미 5초마다
  폴링하는 `lifetrainer-worker.service` 가 한다.
- **점수 상위부터 일부만.** 2,586건 × 20초 = 14시간이다. 전부 요약하지 않는다.
- **`dedupe_key` 로 중복 적재를 막는다.** 타이머가 두 번 뛰거나 사람이 손으로 한 번
  더 돌려도 같은 문서가 두 번 큐에 들어가지 않는다 (`job.dedupe_key` UNIQUE).
- **우선순위는 대화·알림보다 뒤.** `priority` 는 작을수록 먼저이고 예약 알림이
  기본값 100 이다. 배치는 200 을 써서 시각이 된 알림에 절대 앞서지 않는다.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

from lifetrainer.db import transaction
from lifetrainer.llm.queue import enqueue
from lifetrainer.timeutil import day_str, now_ts

if TYPE_CHECKING:
    from lifetrainer.config import Config

logger = logging.getLogger(__name__)

# 배치 잡의 우선순위. `queue.enqueue` 기본값(100, 예약 알림이 쓴다)보다 뒤로 둔다.
BATCH_PRIORITY = 200

# 이 종류만 배치로 취급한다. `reminder` 는 사람에게 한 약속이라 절대 건드리지 않는다.
BATCH_KINDS = ("summarize_doc", "tag_activity")


def pending_docs(conn: sqlite3.Connection, limit: int) -> list[int]:
    """요약 대상 문서 id 를 점수 상위부터 고른다.

    - `summary IS NULL` — 이미 요약된 것은 다시 하지 않는다
    - `state = 'scored'` — 스코어링을 마친 것만 (점수가 없으면 순서를 못 정한다)
    - `dup_of IS NULL` — 중복으로 접힌 문서는 원본만 요약한다
    """
    if limit <= 0:
        return []
    rows = conn.execute(
        "SELECT id FROM doc"
        " WHERE summary IS NULL AND state = 'scored' AND dup_of IS NULL"
        " ORDER BY score DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [int(r["id"]) for r in rows]


def untagged_count(conn: sqlite3.Connection) -> int:
    """LLM 태깅이 안 된 미분류 지문 수."""
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM unclassified WHERE llm_category IS NULL"
    ).fetchone()
    return int(row["c"] if row else 0)


def cancel_pending_batch(conn: sqlite3.Connection) -> int:
    """아직 시작 안 한 배치 잡을 큐에서 **지운다**. 지운 개수를 반환.

    새벽 창(02:00~06:00)이 끝날 때 부른다 — 남은 잡을 그대로 두면 사람이 쓰는
    낮 시간에 GPU 를 물고 있다. 대화 우선권이 있어 질문이 밀리지는 않지만,
    "밤에 하는 일"이 낮까지 새는 것은 설계가 아니라 사고다.

    `state='cancelled'` 로 표시하지 않고 **삭제**하는 이유: `dedupe_key` 가
    UNIQUE 라 취소 행이 남아 있으면 그 문서는 **다음 밤에 다시 큐에 못 들어간다.**
    시작도 안 한 잡이라 지워도 잃을 이력이 없다.

    `running` 은 건드리지 않는다 — 진행 중인 요약 1건(약 20초)은 끝내는 게 맞다.
    """
    placeholders = ",".join("?" for _ in BATCH_KINDS)
    with transaction(conn) as tx:
        cur = tx.execute(
            f"DELETE FROM job WHERE state = 'queued' AND kind IN ({placeholders})",
            BATCH_KINDS,
        )
        return cur.rowcount


def enqueue_nightly(
    conn: sqlite3.Connection,
    cfg: "Config",
    *,
    summary_limit: int | None = None,
    tag_limit: int | None = None,
    now: float | None = None,
) -> dict:
    """야간 배치 잡을 큐에 넣는다. 넣기만 하고 실행은 상시 워커가 한다.

    반환: `{"summaries": 적재수, "summaries_skipped": 중복수, "tags": 0|1,
             "untagged": 미태깅수, "candidates": 요약후보수}`
    """
    ts = now if now is not None else now_ts()
    s_limit = cfg.nightly.summary_limit if summary_limit is None else summary_limit
    t_limit = cfg.nightly.tag_limit if tag_limit is None else tag_limit

    doc_ids = pending_docs(conn, s_limit)
    queued = 0
    skipped = 0
    for doc_id in doc_ids:
        job_id = enqueue(
            conn,
            "summarize_doc",
            {"doc_id": doc_id},
            priority=BATCH_PRIORITY,
            dedupe_key=f"summarize_doc:{doc_id}",
        )
        if job_id is None:
            skipped += 1  # 이미 큐에 있거나 예전에 처리된 문서
        else:
            queued += 1

    pending_tags = untagged_count(conn)
    tag_jobs = 0
    if pending_tags > 0 and t_limit > 0:
        # 하루에 한 번만. 태깅은 상위 N개를 **한 번의 호출로** 묶는 배치라
        # 여러 건을 큐에 넣을 이유가 없다.
        day = day_str(ts, cfg.tz, boundary_hour=cfg.rollup.day_boundary_hour)
        job_id = enqueue(
            conn,
            "tag_activity",
            {"limit": t_limit},
            priority=BATCH_PRIORITY,
            dedupe_key=f"tag_activity:{day}",
        )
        if job_id is not None:
            tag_jobs = 1

    logger.info(
        "야간 배치 적재: 요약 %d건(중복 제외 %d) · 태깅 %d건(미태깅 %d개 대기)",
        queued,
        skipped,
        tag_jobs,
        pending_tags,
    )
    return {
        "summaries": queued,
        "summaries_skipped": skipped,
        "candidates": len(doc_ids),
        "tags": tag_jobs,
        "untagged": pending_tags,
    }
