"""GPU 작업 큐 (`job` 테이블 하나를 통과하는 모든 LLM 작업).

워커는 단 하나만 돈다 — 16GB 공유 메모리에서 동시 로드는 곧 OOM 이다(계약서 §0).
이 모듈은 큐 자체(적재/집기/완료/실패/회수)만 책임진다. 실제로 GPU 를 쓰는 건
`worker.py` 다.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from lifetrainer.db import transaction
from lifetrainer.timeutil import now_ts


@dataclass
class Job:
    id: int
    kind: str
    payload: dict
    attempts: int
    priority: int


def enqueue(
    conn: sqlite3.Connection,
    kind: str,
    payload: dict,
    *,
    priority: int = 100,
    dedupe_key: str | None = None,
    not_before: float = 0.0,
    max_attempts: int = 3,
) -> int | None:
    """잡을 큐에 넣는다. `dedupe_key` 가 이미 존재하면 넣지 않고 `None` 을 반환한다."""
    now = now_ts()
    try:
        with transaction(conn) as tx:
            cur = tx.execute(
                "INSERT INTO job"
                "(kind, payload_json, priority, state, attempts, max_attempts,"
                " not_before, dedupe_key, created_at)"
                " VALUES (?, ?, ?, 'queued', 0, ?, ?, ?, ?)",
                (
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    priority,
                    max_attempts,
                    not_before,
                    dedupe_key,
                    now,
                ),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        # dedupe_key UNIQUE 충돌 = 이미 같은 작업이 큐에 있다. 중복 적재 방지.
        return None


def claim(
    conn: sqlite3.Connection,
    *,
    worker: str,
    kinds: list[str] | None = None,
    lease_sec: float = 1800,
    now: float | None = None,
) -> Job | None:
    """대기 중인 잡 하나를 집어 `running` 으로 바꾸고 리스를 건다.

    `BEGIN IMMEDIATE` 트랜잭션 안에서 select+update 를 하나로 묶어, 여러 워커가
    동시에 같은 잡을 집는 경합을 막는다(WAL 모드에서 writer 경합 시 즉시 실패).
    우선순위는 숫자가 작을수록 먼저, 같으면 `not_before` 가 이른 것, 그다음 id 순.
    """
    now_val = now if now is not None else now_ts()
    with transaction(conn) as tx:
        query = (
            "SELECT id, kind, payload_json, attempts, priority FROM job"
            " WHERE state = 'queued' AND not_before <= ?"
        )
        params: list = [now_val]
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            query += f" AND kind IN ({placeholders})"
            params.extend(kinds)
        query += " ORDER BY priority ASC, not_before ASC, id ASC LIMIT 1"

        row = tx.execute(query, params).fetchone()
        if row is None:
            return None

        lease_until = now_val + lease_sec
        new_attempts = row["attempts"] + 1
        tx.execute(
            "UPDATE job SET state='running', lease_until=?, worker=?,"
            " attempts=?, started_at=? WHERE id=?",
            (lease_until, worker, new_attempts, now_val, row["id"]),
        )
        payload = json.loads(row["payload_json"])
        return Job(
            id=row["id"],
            kind=row["kind"],
            payload=payload,
            attempts=new_attempts,
            priority=row["priority"],
        )


def complete(conn: sqlite3.Connection, job_id: int, result: dict | None = None) -> None:
    """잡을 성공으로 종료한다."""
    now = now_ts()
    with transaction(conn) as tx:
        tx.execute(
            "UPDATE job SET state='done', result_json=?, finished_at=?, lease_until=NULL"
            " WHERE id=?",
            (json.dumps(result, ensure_ascii=False) if result is not None else None, now, job_id),
        )


def fail(conn: sqlite3.Connection, job_id: int, error: str, *, retry_in: float | None = None) -> None:
    """잡 실패를 기록한다.

    `attempts < max_attempts` 이면 `queued` 로 되돌려 재시도시킨다(백오프:
    `not_before = now + 60 * 2**attempts`, `retry_in` 이 주어지면 그 값을 우선한다).
    소진됐으면 `failed` 로 확정한다.
    """
    now = now_ts()
    with transaction(conn) as tx:
        row = tx.execute(
            "SELECT attempts, max_attempts FROM job WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            return
        attempts = row["attempts"]
        max_attempts = row["max_attempts"]
        if attempts >= max_attempts:
            tx.execute(
                "UPDATE job SET state='failed', error=?, finished_at=?, lease_until=NULL"
                " WHERE id=?",
                (error, now, job_id),
            )
        else:
            delay = retry_in if retry_in is not None else 60 * (2**attempts)
            tx.execute(
                "UPDATE job SET state='queued', error=?, not_before=?, lease_until=NULL,"
                " worker=NULL WHERE id=?",
                (error, now + delay, job_id),
            )


def reap_expired(conn: sqlite3.Connection, *, now: float | None = None) -> int:
    """리스가 만료된 `running` 잡을 `queued` 로 되돌린다 (워커 크래시 복구)."""
    now_val = now if now is not None else now_ts()
    with transaction(conn) as tx:
        cur = tx.execute(
            "UPDATE job SET state='queued', lease_until=NULL, worker=NULL"
            " WHERE state='running' AND lease_until IS NOT NULL AND lease_until < ?",
            (now_val,),
        )
        return cur.rowcount


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    """state 별 잡 개수."""
    rows = conn.execute("SELECT state, COUNT(*) AS c FROM job GROUP BY state").fetchall()
    return {row["state"]: row["c"] for row in rows}


def purge_done(conn: sqlite3.Connection, *, older_than_days: int = 14) -> int:
    """`older_than_days` 보다 오래된 종료 상태(done/failed/cancelled) 잡을 지운다."""
    cutoff = now_ts() - older_than_days * 86400
    with transaction(conn) as tx:
        cur = tx.execute(
            "DELETE FROM job WHERE state IN ('done', 'failed', 'cancelled')"
            " AND finished_at IS NOT NULL AND finished_at < ?",
            (cutoff,),
        )
        return cur.rowcount
