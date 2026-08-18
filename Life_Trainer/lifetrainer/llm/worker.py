"""GPU 잡 워커 — `job` 큐를 폴링해 LLM 을 호출하는 핸들러를 실행한다.

**GPU 는 단일 자원이다.** `acquire_gpu_lock` 이 프로세스 락(`fcntl.flock(LOCK_EX|LOCK_NB)`)을
걸어 두 번째 워커가 뜨면 즉시 종료하게 한다. 16GB 공유 메모리에서 llama-server(이미
~10.4GB 사용 중) 옆에 또 하나가 모델을 올리면 곧바로 OOM 이다(계약서 §0).

Phase 3 골격에서 구현하는 핸들러는 두 개뿐이다:
- `tag_activity`: `unclassified` 상위 N개를 한 번의 호출로 묶어 태깅.
- `summarize_doc`: `doc` 하나를 요약하고 태그를 뽑는다.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import IO, TYPE_CHECKING, Callable

from lifetrainer.db import transaction
from lifetrainer.llm.client import LLMClient, LLMUnavailable
from lifetrainer.llm.queue import Job, claim, complete, fail, reap_expired
from lifetrainer.llm.schemas import DocSummary, json_schema_of
from lifetrainer.timeutil import now_ts

if TYPE_CHECKING:
    import sqlite3

    from lifetrainer.config import Config

logger = logging.getLogger(__name__)

# 배치 태깅 응답 스키마. `pattern` 없이 순수 타입/필수 필드 제약만 쓴다
# (llama.cpp 의 GBNF 변환기가 정규식을 못 다루므로 — client.py 의 _check_no_pattern 참고).
_BATCH_TAG_SCHEMA: dict = {
    "title": "ActivityTagBatch",
    "type": "object",
    "properties": {
        "tags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "입력 목록의 index 를 그대로"},
                    "category": {"type": "string"},
                    "subcategory": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                },
                "required": ["index", "category", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["tags"],
    "additionalProperties": False,
}


def _load_category_ids(cfg: "Config") -> list[str]:
    """`config/rules.yaml` 에서 카테고리 id 목록을 읽어 프롬프트 힌트로 쓴다.

    실패해도(파일 없음/형식 오류) 예외를 올리지 않는다 — 프롬프트 품질에만
    영향을 주는 보조 정보이지, 없어도 태깅 자체는 동작해야 한다.
    """
    try:
        import yaml

        raw = yaml.safe_load(Path(cfg.rollup.rules_path).read_text(encoding="utf-8"))
        cats = (raw or {}).get("categories") or []
        ids = [str(c["id"]) for c in cats if isinstance(c, dict) and "id" in c]
        # away/off 는 규칙에서 직접 지정하지 않는 예약 카테고리라 후보에서 뺀다.
        return [i for i in ids if i not in ("away", "off")]
    except Exception as exc:  # noqa: BLE001 - 힌트용이라 실패해도 계속 진행한다
        logger.debug("rules.yaml 에서 카테고리 목록을 읽지 못했습니다: %s", exc)
        return []


def tag_activity(
    conn: "sqlite3.Connection", cfg: "Config", client: LLMClient, job: Job
) -> dict:
    """`unclassified` 에서 미태깅 항목 중 점유 시간 상위 N개를 한 번의 호출로 태깅한다.

    개별 호출로 처리하면 느리므로(요청당 프롬프트 처리 오버헤드) 반드시 배치로 묶는다.
    `job.payload`: `{"limit": int}` (기본 20).
    """
    limit = int(job.payload.get("limit", 20))
    rows = conn.execute(
        "SELECT fingerprint, app, title_sample FROM unclassified"
        " WHERE llm_category IS NULL ORDER BY seconds_total DESC LIMIT ?",
        (limit,),
    ).fetchall()
    if not rows:
        return {"tagged": 0, "total": 0}

    category_ids = _load_category_ids(cfg)
    lines = "\n".join(
        f"{i}: app={(r['app'] or '')!r} title={(r['title_sample'] or '')!r}"
        for i, r in enumerate(rows)
    )
    hint = f" 가능한 category 값: {', '.join(category_ids)}." if category_ids else ""
    system = (
        "당신은 컴퓨터 사용 활동을 분류하는 도우미입니다. 각 항목에 category(카테고리 id), "
        "subcategory(모르면 null), confidence(0.0~1.0)를 매기세요." + hint
    )
    user = f"다음 {len(rows)}개 활동을 분류하세요 (index 는 입력 그대로 유지):\n{lines}"

    max_tokens = min(1024, 100 + 40 * len(rows))
    result = client.complete_json(
        system, user, _BATCH_TAG_SCHEMA, purpose="tag_activity", max_tokens=max_tokens
    )
    tags = result.get("tags") or []

    now = now_ts()
    tagged = 0
    with transaction(conn) as tx:
        for entry in tags:
            if not isinstance(entry, dict):
                continue
            idx = entry.get("index")
            if not isinstance(idx, int) or not (0 <= idx < len(rows)):
                continue
            category = entry.get("category")
            if not category:
                continue
            fingerprint = rows[idx]["fingerprint"]
            tx.execute(
                "UPDATE unclassified SET llm_category=?, llm_subcategory=?,"
                " llm_confidence=?, llm_at=? WHERE fingerprint=?",
                (
                    str(category),
                    entry.get("subcategory"),
                    entry.get("confidence"),
                    now,
                    fingerprint,
                ),
            )
            tagged += 1

    return {"tagged": tagged, "total": len(rows)}


def summarize_doc(
    conn: "sqlite3.Connection", cfg: "Config", client: LLMClient, job: Job
) -> dict:
    """`doc` 하나를 3~4줄로 요약하고 태그를 뽑아 `summary`/`summary_at`/`state` 를 갱신한다.

    `job.payload`: `{"doc_id": int}`.
    """
    doc_id = job.payload.get("doc_id")
    if doc_id is None:
        raise ValueError("summarize_doc 페이로드에 doc_id 가 없습니다")

    row = conn.execute(
        "SELECT id, title, abstract FROM doc WHERE id = ?", (doc_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"doc {doc_id} 를 찾을 수 없습니다")

    title = row["title"] or ""
    abstract = row["abstract"] or ""
    system = (
        "당신은 문서를 한국어로 간결하게 요약하는 도우미입니다. "
        "3~4문장으로 요약하고 핵심 키워드 태그를 뽑으세요."
    )
    user = f"제목: {title}\n\n본문/초록:\n{abstract}"
    schema = json_schema_of(DocSummary, "DocSummary")

    result = client.complete_json(system, user, schema, purpose="summarize_doc", max_tokens=400)
    parsed = DocSummary.model_validate(result)

    now = now_ts()
    with transaction(conn) as tx:
        tx.execute(
            "UPDATE doc SET summary = ?, summary_at = ?, state = 'summarized' WHERE id = ?",
            (parsed.summary, now, doc_id),
        )
        for tag in parsed.tags:
            tag = (tag or "").strip()
            if not tag:
                continue
            tx.execute(
                "INSERT INTO doc_tag(doc_id, tag, weight, source) VALUES (?, ?, 1.0, 'llm')"
                " ON CONFLICT(doc_id, tag) DO UPDATE SET weight = excluded.weight,"
                " source = excluded.source",
                (doc_id, tag),
            )

    return {
        "doc_id": doc_id,
        "summary": parsed.summary,
        "tags": parsed.tags,
        "relevance": parsed.relevance,
    }


def _build_reminder_notifier(cfg: "Config"):
    """`SlackNotifier` 생성 팩토리. 테스트는 이걸 monkeypatch 해 실제 발송을 막는다."""
    from lifetrainer.slackio.notify import SlackNotifier

    return SlackNotifier(cfg)


def send_reminder(
    conn: "sqlite3.Connection", cfg: "Config", client: LLMClient, job: Job
) -> dict:
    """예약된 알림을 Slack 으로 보낸다 (`job.not_before` 가 곧 예약 시각).

    **LLM 을 쓰지 않는 유일한 핸들러다.** 대화 중 `schedule_reminder` 툴이 큐에
    넣어두면 `claim` 이 `not_before <= now` 조건으로 때가 됐을 때만 집어 온다 —
    별도의 스케줄러가 필요 없다.

    `job.payload`: `{"text": str, "channel": str|None, "actor": str}`.
    """
    text = str(job.payload.get("text") or "").strip()
    if not text:
        raise ValueError("reminder 페이로드에 text 가 없습니다")

    notifier = _build_reminder_notifier(cfg)
    ts = notifier.post(f"⏰ {text}", channel=job.payload.get("channel"))
    if not ts:
        # 토큰/채널이 없어 발송이 생략된 경우. 재시도해도 같은 결과라 실패로 올리지 않는다.
        logger.warning("알림 발송이 생략됐습니다 (job %s): %s", job.id, text[:80])
    return {"text": text, "posted": bool(ts), "ts": ts}


HANDLERS: dict[str, Callable[["sqlite3.Connection", "Config", LLMClient, Job], dict]] = {
    "tag_activity": tag_activity,
    "summarize_doc": summarize_doc,
    "reminder": send_reminder,
}


def acquire_gpu_lock(cfg: "Config") -> "IO | None":
    """`data/gpu.lock` 에 배타 락을 건다. 이미 잡혀 있으면(다른 워커 실행 중) `None`.

    반환된 파일 객체는 프로세스가 죽거나 `flock(LOCK_UN)` 할 때까지 락을 쥔다 —
    호출부가 들고 있다가 작업이 끝나면 닫아야 한다.
    """
    import fcntl

    lock_path = Path(cfg.data_dir) / "gpu.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    return fh


def _release_gpu_lock(lock: "IO") -> None:
    import fcntl

    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    finally:
        lock.close()


def _process_ready_jobs(conn: "sqlite3.Connection", cfg: "Config", *, worker: str) -> int:
    """대기 중인 잡을 처리 가능한 만큼 순서대로 처리한다. 처리한 잡 수를 반환."""
    reap_expired(conn)
    client = LLMClient(cfg, conn=conn)
    processed = 0

    while True:
        job = claim(conn, worker=worker, kinds=list(HANDLERS.keys()))
        if job is None:
            break

        handler = HANDLERS.get(job.kind)
        if handler is None:
            fail(conn, job.id, f"알 수 없는 job kind: {job.kind!r}")
            processed += 1
            continue

        try:
            result = handler(conn, cfg, client, job)
        except LLMUnavailable as exc:
            # LLM 이 죽어 있는 건 이 잡의 잘못이 아니다 — 시도 횟수를 소진시키지 않고
            # 되돌린 뒤, 이번 실행에서는 더 claim 하지 않는다(다른 잡도 어차피 실패한다).
            logger.warning(
                "LLM 서버가 응답하지 않습니다 (job %s/%s), 재시도로 되돌립니다: %s",
                job.id,
                job.kind,
                exc,
            )
            with transaction(conn) as tx:
                tx.execute(
                    "UPDATE job SET state = 'queued', attempts = attempts - 1, error = ?,"
                    " not_before = ?, lease_until = NULL, worker = NULL WHERE id = ?",
                    (str(exc), now_ts() + 60.0, job.id),
                )
            break
        except Exception as exc:  # noqa: BLE001 - 핸들러 하나의 실패로 워커 전체가 죽으면 안 된다.
            logger.exception("잡 %s(%s) 처리 실패", job.id, job.kind)
            fail(conn, job.id, str(exc))
        else:
            complete(conn, job.id, result)

        processed += 1

    return processed


def run_once(conn: "sqlite3.Connection", cfg: "Config", *, worker: str | None = None) -> int:
    """GPU 락을 잡고, 처리 가능한 잡을 한 차례 모두 처리한 뒤 락을 놓는다.

    락을 못 잡으면(다른 워커 실행 중) 아무 것도 하지 않고 0 을 반환한다.
    """
    lock = acquire_gpu_lock(cfg)
    if lock is None:
        logger.warning("GPU 락을 잡지 못했습니다 — 다른 워커가 실행 중입니다. 이번 실행은 건너뜁니다.")
        return 0
    try:
        return _process_ready_jobs(conn, cfg, worker=worker or f"worker-{os.getpid()}")
    finally:
        _release_gpu_lock(lock)


def run_forever(conn: "sqlite3.Connection", cfg: "Config", *, poll_sec: float = 5.0) -> None:
    """GPU 락을 프로세스 수명 동안 쥐고, 주기적으로 큐를 폴링하는 상시 워커.

    락을 못 잡으면 즉시 종료한다 — 두 번째 워커가 GPU 를 나눠 쓰려 들면 OOM 이다.
    """
    lock = acquire_gpu_lock(cfg)
    if lock is None:
        logger.error("GPU 락을 잡지 못했습니다 — 다른 워커 프로세스가 이미 실행 중입니다. 종료합니다.")
        return

    worker_name = f"worker-{os.getpid()}"
    logger.info("GPU 워커 시작 (poll_sec=%s)", poll_sec)
    try:
        while True:
            try:
                processed = _process_ready_jobs(conn, cfg, worker=worker_name)
            except Exception:  # noqa: BLE001 - 루프 자체는 절대 죽으면 안 된다.
                logger.exception("워커 루프에서 예외가 발생했습니다")
                processed = 0
            if processed == 0:
                time.sleep(poll_sec)
    finally:
        _release_gpu_lock(lock)
