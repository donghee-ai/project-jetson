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


@dataclass
class QueueHealth:
    """지금 큐가 아픈가. `stats()` 의 누적 개수와는 다른 질문에 답한다."""

    window_days: int
    recent_failed: int          # 창 안에서 끝난 실패
    recent_finished: int        # 창 안에서 끝난 전체 (done + failed)
    oldest_queued_age: float    # 가장 오래 기다린 대기 잡의 나이(초). 없으면 0
    stale_running: int          # running 인데 lease 가 만료된 잡
    last_failure_ts: float      # 마지막 실패 시각. 없으면 0
    success_since_last_failure: int  # 그 뒤로 성공한 잡 수 ★ "고쳐졌나" 에 답한다
    worst_kind: tuple[str, int, int] | None  # (kind, failed, finished) — 창 안 최악
    now: float

    @property
    def recent_rate(self) -> float:
        return self.recent_failed / self.recent_finished if self.recent_finished else 0.0

    @property
    def hours_since_last_failure(self) -> float | None:
        """마지막 실패 이후 시간. 실패가 아예 없으면 None."""
        if not self.last_failure_ts:
            return None
        return (self.now - self.last_failure_ts) / 3600.0


def health(
    conn: sqlite3.Connection,
    *,
    window_days: int = 7,
    min_sample: int = 10,
    lease_grace: float = 300.0,
) -> QueueHealth:
    """최근 창 기준으로 큐 건강을 잰다.

    ★ 왜 누적이 아니라 창인가 (issues/0016)
      `stats()` 의 누적 실패율은 **고쳐도 안 내려간다.** `purge_done` 이 종료 잡을
      14일 뒤에 걷어가므로 done 은 회전하는데 failed 는 그 창이 지나야 사라진다.
      실제로 08-20 에 난 실패 203건이 08-21 에 원인이 고쳐진 뒤에도 doctor 를
      계속 노란불로 만들었고, 그 2주 동안 **새 실패가 들어와도 구분이 안 됐다.**

    ★ 실패율 하나로는 부족하다
      backlog 는 실패율에 안 잡힌다 — 워커가 죽으면 실패가 아니라 `queued` 가 쌓이고,
      집다 만 잡은 `running` 에서 lease 만 만료된 채 남는다. 셋을 같이 본다.

    ★ 창을 넓히는 것만으로는 부족하다 — 창 크기를 데이터에 맞춰 고르게 된다
      실제로 7일 창을 잡아 보니 08-20 의 실패 무더기가 **여전히 창 안**이었다.
      창을 줄여 빼는 것은 데이터를 보고 눈금을 정하는 것이라 정직하지 않다.
      그래서 판정 축을 바꾼다 — **"지금 실패하고 있나"** 다:

        · `hours_since_last_failure`     마지막 실패가 언제였나
        · `success_since_last_failure`   그 뒤로 몇 건이 성공했나

      *"6일 전에 무더기로 죽었고 그 뒤 486건이 연속 성공"* 은 **고쳐진 것**이다.
      비율은 같아도 이 둘이 다르면 다른 상태다.

    `min_sample` 은 표본이 적을 때 비율을 과대 해석하지 않으려는 것이다 —
    3건 중 1건 실패를 33% 경보로 읽으면 안 된다. 판정은 호출부가 한다.
    """
    now = now_ts()
    cutoff = now - window_days * 86400

    rows = conn.execute(
        "SELECT kind, state, COUNT(*) AS c FROM job"
        " WHERE state IN ('done', 'failed') AND finished_at IS NOT NULL AND finished_at >= ?"
        " GROUP BY kind, state",
        (cutoff,),
    ).fetchall()
    by_kind: dict[str, dict[str, int]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], {})[r["state"]] = r["c"]

    recent_failed = sum(v.get("failed", 0) for v in by_kind.values())
    recent_finished = sum(v.get("failed", 0) + v.get("done", 0) for v in by_kind.values())

    worst = None
    for kind, v in by_kind.items():
        f, fin = v.get("failed", 0), v.get("failed", 0) + v.get("done", 0)
        if f and (worst is None or f / fin > worst[1] / worst[2]):
            worst = (kind, f, fin)

    oldest = conn.execute(
        "SELECT MIN(created_at) AS t FROM job WHERE state = 'queued'"
    ).fetchone()["t"]
    # lease_grace 를 주는 이유: 방금 갱신한 잡을 죽은 것으로 세지 않는다.
    stale = conn.execute(
        "SELECT COUNT(*) AS c FROM job WHERE state = 'running' AND lease_until < ?",
        (now - lease_grace,),
    ).fetchone()["c"]
    last_fail = conn.execute(
        "SELECT MAX(finished_at) AS t FROM job WHERE state = 'failed'"
    ).fetchone()["t"]
    # 마지막 실패 뒤로 성공한 잡. 이게 크면 원인이 이미 제거된 것이다.
    since = conn.execute(
        "SELECT COUNT(*) AS c FROM job WHERE state = 'done' AND finished_at > ?",
        (last_fail or 0,),
    ).fetchone()["c"]

    return QueueHealth(
        window_days=window_days,
        recent_failed=recent_failed,
        recent_finished=recent_finished,
        oldest_queued_age=(now - oldest) if oldest else 0.0,
        stale_running=int(stale or 0),
        last_failure_ts=float(last_fail or 0.0),
        success_since_last_failure=int(since or 0),
        worst_kind=worst,
        now=now,
    )


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
