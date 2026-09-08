"""lifetrainer.llm.queue 테스트. 전부 tmp_path 의 임시 SQLite 파일로 돈다 (네트워크 없음)."""

from __future__ import annotations

import pytest

from lifetrainer import db
from lifetrainer.llm import queue
from lifetrainer.timeutil import now_ts


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "lt.db")
    db.init_db(c)
    yield c
    c.close()


def _row(conn, job_id):
    return conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()


# ── enqueue ──────────────────────────────────────────────────────────────


def test_enqueue_returns_job_id(conn):
    job_id = queue.enqueue(conn, "tag_activity", {"limit": 10})
    assert isinstance(job_id, int)
    row = _row(conn, job_id)
    assert row["kind"] == "tag_activity"
    assert row["state"] == "queued"
    assert row["attempts"] == 0


def test_enqueue_stores_payload_as_json(conn):
    job_id = queue.enqueue(conn, "summarize_doc", {"doc_id": 7})
    row = _row(conn, job_id)
    assert '"doc_id"' in row["payload_json"]
    assert "7" in row["payload_json"]


def test_enqueue_duplicate_dedupe_key_returns_none(conn):
    first = queue.enqueue(conn, "tag_activity", {}, dedupe_key="tag-batch-1")
    second = queue.enqueue(conn, "tag_activity", {}, dedupe_key="tag-batch-1")
    assert first is not None
    assert second is None
    count = conn.execute("SELECT COUNT(*) FROM job").fetchone()[0]
    assert count == 1


def test_enqueue_without_dedupe_key_allows_many(conn):
    a = queue.enqueue(conn, "tag_activity", {})
    b = queue.enqueue(conn, "tag_activity", {})
    assert a != b
    count = conn.execute("SELECT COUNT(*) FROM job").fetchone()[0]
    assert count == 2


# ── claim ────────────────────────────────────────────────────────────────


def test_claim_marks_running_and_sets_lease(conn):
    job_id = queue.enqueue(conn, "tag_activity", {"limit": 5})
    now = now_ts()

    job = queue.claim(conn, worker="w1", now=now, lease_sec=100)

    assert job is not None
    assert job.id == job_id
    assert job.kind == "tag_activity"
    assert job.payload == {"limit": 5}
    assert job.attempts == 1

    row = _row(conn, job_id)
    assert row["state"] == "running"
    assert row["worker"] == "w1"
    assert row["lease_until"] == pytest.approx(now + 100)
    assert row["started_at"] == pytest.approx(now)


def test_claim_returns_none_when_queue_empty(conn):
    assert queue.claim(conn, worker="w1") is None


def test_claim_twice_returns_different_jobs(conn):
    id_a = queue.enqueue(conn, "tag_activity", {}, priority=10)
    id_b = queue.enqueue(conn, "tag_activity", {}, priority=20)

    job1 = queue.claim(conn, worker="w1")
    job2 = queue.claim(conn, worker="w2")

    assert job1 is not None and job2 is not None
    assert job1.id != job2.id
    assert {job1.id, job2.id} == {id_a, id_b}
    # priority 가 작은(=우선순위 높은) 쪽이 먼저 나가야 한다.
    assert job1.id == id_a

    # 더 이상 큐에 남은 게 없으면 세 번째 claim 은 None.
    assert queue.claim(conn, worker="w3") is None


def test_claim_respects_kinds_filter(conn):
    doc_id = queue.enqueue(conn, "summarize_doc", {"doc_id": 1})
    queue.enqueue(conn, "tag_activity", {})

    job = queue.claim(conn, worker="w1", kinds=["summarize_doc"])
    assert job is not None
    assert job.id == doc_id
    assert job.kind == "summarize_doc"


def test_claim_ignores_not_yet_due_jobs(conn):
    now = now_ts()
    future_id = queue.enqueue(conn, "tag_activity", {}, not_before=now + 3600)
    assert queue.claim(conn, worker="w1", now=now) is None

    # 시간이 지나면(now 를 미래로 밀면) 잡을 수 있어야 한다.
    job = queue.claim(conn, worker="w1", now=now + 3601)
    assert job is not None
    assert job.id == future_id


def test_claim_is_exclusive_no_double_claim(conn):
    queue.enqueue(conn, "tag_activity", {})
    job1 = queue.claim(conn, worker="w1")
    assert job1 is not None
    # 같은 잡을 다른 워커가 다시 집을 수 없어야 한다 (이미 running).
    job2 = queue.claim(conn, worker="w2")
    assert job2 is None


# ── complete ─────────────────────────────────────────────────────────────


def test_complete_marks_done_with_result(conn):
    job_id = queue.enqueue(conn, "tag_activity", {})
    queue.claim(conn, worker="w1")

    queue.complete(conn, job_id, {"tagged": 3})

    row = _row(conn, job_id)
    assert row["state"] == "done"
    assert '"tagged"' in row["result_json"]
    assert row["finished_at"] is not None
    assert row["lease_until"] is None


def test_complete_without_result(conn):
    job_id = queue.enqueue(conn, "tag_activity", {})
    queue.claim(conn, worker="w1")
    queue.complete(conn, job_id)
    row = _row(conn, job_id)
    assert row["state"] == "done"
    assert row["result_json"] is None


# ── fail / 백오프 / 소진 ─────────────────────────────────────────────────


def test_fail_requeues_with_growing_backoff(conn):
    job_id = queue.enqueue(conn, "tag_activity", {}, max_attempts=10)

    before = now_ts()
    job = queue.claim(conn, worker="w1")
    assert job.attempts == 1
    queue.fail(conn, job_id, "boom-1")

    row = _row(conn, job_id)
    assert row["state"] == "queued"
    delay1 = row["not_before"] - before
    # not_before = now + 60 * 2**attempts (attempts=1) -> 약 120초
    assert 100 <= delay1 <= 140
    assert row["error"] == "boom-1"

    # 두 번째 실패: not_before 를 넘긴 시각으로 다시 claim 해서 재현한다.
    before2 = now_ts()
    job2 = queue.claim(conn, worker="w1", now=row["not_before"] + 1)
    assert job2.attempts == 2
    queue.fail(conn, job_id, "boom-2")

    row2 = _row(conn, job_id)
    delay2 = row2["not_before"] - before2
    # attempts=2 -> 약 240초. 첫 번째보다 확실히 커야 한다.
    assert delay2 > delay1


def test_fail_with_explicit_retry_in(conn):
    job_id = queue.enqueue(conn, "tag_activity", {}, max_attempts=5)
    queue.claim(conn, worker="w1")
    before = now_ts()

    queue.fail(conn, job_id, "custom-delay", retry_in=30.0)

    row = _row(conn, job_id)
    assert row["state"] == "queued"
    assert row["not_before"] == pytest.approx(before + 30.0, abs=2.0)


def test_fail_exhausts_to_failed_state_after_max_attempts(conn):
    job_id = queue.enqueue(conn, "tag_activity", {}, max_attempts=2)

    # 1차 시도 실패 -> 재시도로 되돌아감
    job = queue.claim(conn, worker="w1")
    assert job.attempts == 1
    queue.fail(conn, job_id, "err-1")
    assert _row(conn, job_id)["state"] == "queued"

    # 2차 시도 실패 -> attempts(2) >= max_attempts(2) -> 완전히 failed
    job2 = queue.claim(conn, worker="w1", now=_row(conn, job_id)["not_before"] + 1)
    assert job2.attempts == 2
    queue.fail(conn, job_id, "err-2")

    row = _row(conn, job_id)
    assert row["state"] == "failed"
    assert row["error"] == "err-2"
    assert row["finished_at"] is not None


def test_fail_unknown_job_id_is_noop(conn):
    queue.fail(conn, 999999, "no such job")  # 예외 없이 조용히 무시


# ── reap_expired ─────────────────────────────────────────────────────────


def test_reap_expired_requeues_stale_lease(conn):
    job_id = queue.enqueue(conn, "tag_activity", {})
    now = now_ts()
    queue.claim(conn, worker="w1", now=now, lease_sec=10)  # lease_until = now + 10

    # 리스가 아직 안 끝났으면 회수 대상 아님.
    assert queue.reap_expired(conn, now=now + 5) == 0
    assert _row(conn, job_id)["state"] == "running"

    # 리스가 끝난 뒤에는 queued 로 되돌아가야 한다 (워커 크래시 복구).
    reaped = queue.reap_expired(conn, now=now + 11)
    assert reaped == 1
    row = _row(conn, job_id)
    assert row["state"] == "queued"
    assert row["lease_until"] is None
    assert row["worker"] is None


def test_reap_expired_ignores_done_jobs(conn):
    job_id = queue.enqueue(conn, "tag_activity", {})
    queue.claim(conn, worker="w1")
    queue.complete(conn, job_id)
    assert queue.reap_expired(conn, now=now_ts() + 100000) == 0
    assert _row(conn, job_id)["state"] == "done"


# ── stats ────────────────────────────────────────────────────────────────


def test_stats_counts_by_state(conn):
    done_id = queue.enqueue(conn, "tag_activity", {})
    queue.claim(conn, worker="w1")
    queue.complete(conn, done_id)

    queue.enqueue(conn, "tag_activity", {})  # queued 로 남김
    queue.enqueue(conn, "summarize_doc", {"doc_id": 1})  # queued 로 남김

    result = queue.stats(conn)
    assert result["done"] == 1
    assert result["queued"] == 2


# ── purge_done ───────────────────────────────────────────────────────────


def test_purge_done_removes_old_finished_jobs(conn):
    job_id = queue.enqueue(conn, "tag_activity", {})
    queue.claim(conn, worker="w1")
    queue.complete(conn, job_id)

    old_ts = now_ts() - 30 * 86400
    conn.execute("UPDATE job SET finished_at = ? WHERE id = ?", (old_ts, job_id))
    conn.commit()

    purged = queue.purge_done(conn, older_than_days=14)
    assert purged == 1
    assert _row(conn, job_id) is None


def test_purge_done_keeps_recent_and_unfinished(conn):
    recent_done = queue.enqueue(conn, "tag_activity", {})
    queue.claim(conn, worker="w1")
    queue.complete(conn, recent_done)  # finished_at = 지금

    queue.enqueue(conn, "tag_activity", {})  # 아직 queued, finished_at NULL

    purged = queue.purge_done(conn, older_than_days=14)
    assert purged == 0
    count = conn.execute("SELECT COUNT(*) FROM job").fetchone()[0]
    assert count == 2


# ── 예약 알림 (worker.send_reminder) ──────────────────────────────────────
#
# 예약은 별도 스케줄러 없이 `job.not_before` 하나로 돈다 — `claim` 이 때가 된
# 것만 집어 오기 때문이다. 아래 테스트는 그 계약을 고정한다.


class _FakeNotifier:
    def __init__(self) -> None:
        self.posted: list[tuple] = []

    def post(self, text, *, channel=None, **kwargs):  # noqa: ANN001, ANN003
        self.posted.append((text, channel))
        return "1700000000.000100"


def _reminder_job(conn, *, text="스트레칭", channel="D1", not_before=0.0):
    from lifetrainer.llm.tools import REMINDER_KIND

    return queue.enqueue(
        conn, REMINDER_KIND, {"text": text, "channel": channel}, not_before=not_before
    )


def test_예약_시각_전에는_집히지_않는다(conn):
    _reminder_job(conn, not_before=now_ts() + 3600)
    assert queue.claim(conn, worker="w", kinds=["reminder"]) is None


def test_예약_시각이_되면_집힌다(conn):
    _reminder_job(conn, not_before=now_ts() - 1)
    job = queue.claim(conn, worker="w", kinds=["reminder"])
    assert job is not None and job.kind == "reminder"
    assert job.payload["text"] == "스트레칭"


def test_알림_핸들러가_대화_채널로_보낸다(conn, monkeypatch, tmp_path):
    import dataclasses

    from lifetrainer.config import load_config
    from lifetrainer.llm import worker as worker_mod

    notifier = _FakeNotifier()
    monkeypatch.setattr(worker_mod, "_build_reminder_notifier", lambda cfg: notifier)

    cfg = dataclasses.replace(load_config(), db_path=tmp_path / "lt.db", data_dir=tmp_path)
    _reminder_job(conn, text="물 마시기", channel="D_ABC", not_before=now_ts() - 1)
    job = queue.claim(conn, worker="w", kinds=["reminder"])

    result = worker_mod.send_reminder(conn, cfg, None, job)
    assert result["posted"] is True
    assert notifier.posted == [("⏰ 물 마시기", "D_ABC")]


def test_내용이_없는_알림은_실패한다(conn, monkeypatch, tmp_path):
    import dataclasses

    from lifetrainer.config import load_config
    from lifetrainer.llm import worker as worker_mod

    monkeypatch.setattr(worker_mod, "_build_reminder_notifier", lambda cfg: _FakeNotifier())
    cfg = dataclasses.replace(load_config(), db_path=tmp_path / "lt.db", data_dir=tmp_path)
    queue.enqueue(conn, "reminder", {"channel": "D1"}, not_before=now_ts() - 1)
    job = queue.claim(conn, worker="w", kinds=["reminder"])

    import pytest as _pytest

    with _pytest.raises(ValueError, match="text"):
        worker_mod.send_reminder(conn, cfg, None, job)


def test_reminder_가_핸들러에_등록돼_있다():
    """툴은 큐에 넣지만 실제 발송은 워커가 한다 — 이름이 어긋나면 조용히 안 간다."""
    from lifetrainer.llm.tools import REMINDER_KIND
    from lifetrainer.llm.worker import HANDLERS

    assert REMINDER_KIND in HANDLERS


# ── 워커: 일시 장애를 잡의 잘못으로 세지 않는다 ──────────────────────────────
#
# 이 절은 2026-08-21 사고의 방어선이다. 02:00 배치가 llama-server 의 모델 로딩을
# 안 기다려 요약 잡 277건이 죽었다. 원인은 두 겹이었고 **둘 다 있어야 산다**:
#
#   ① client 가 503 을 LLMUnavailable 로 분류한다  → tests/test_llm_client.py
#   ② worker 가 그걸 잡아 큐로 되돌린다             → 여기
#
# ①만 고치고 ②가 깨지면 증상이 똑같이 돌아온다. 그래서 따로 못박는다.


def _fake_cfg(tmp_path):
    """워커가 읽는 최소 설정. GPU 락 파일만 임시 경로로 돌린다."""
    from lifetrainer.config import load_config

    cfg = load_config(None)
    return cfg.__class__(**{**cfg.__dict__, "data_dir": tmp_path})


def test_worker_requeues_job_when_llm_unavailable(conn, tmp_path, monkeypatch):
    """503(모델 로딩 중)은 잡을 죽이지 않는다 — 큐로 돌아가고 시도 횟수도 안 깎인다."""
    from lifetrainer.llm import worker as worker_mod
    from lifetrainer.llm.client import LLMUnavailable

    job_id = queue.enqueue(conn, "summarize_doc", {"doc_id": 1})

    def boom(*_args, **_kwargs):
        raise LLMUnavailable('LLM 서버가 503 를 반환했습니다: {"message":"Loading model"}')

    monkeypatch.setitem(worker_mod.HANDLERS, "summarize_doc", boom)
    monkeypatch.setattr(worker_mod, "LLMClient", lambda *a, **k: object())

    worker_mod._process_ready_jobs(conn, _fake_cfg(tmp_path), worker="test")

    row = _row(conn, job_id)
    assert row["state"] == "queued", "503 인데 failed 로 떨어졌다 — 그날의 사고가 그대로다"
    assert row["attempts"] == 0, "일시 장애로 시도 횟수를 소진시키면 안 된다"
    assert row["not_before"] > now_ts(), "곧바로 다시 집으면 또 503 이다 — 뒤로 밀어야 한다"
    assert row["lease_until"] is None and row["worker"] is None


def test_worker_burns_attempts_on_permanent_error(conn, tmp_path, monkeypatch):
    """반대쪽 — 진짜 잘못된 잡은 시도 횟수를 소진하고 결국 failed 로 확정돼야 한다.

    `queue.fail()` 은 곧바로 죽이지 않는다: `attempts < max_attempts` 동안 백오프
    (`60 * 2**attempts`)를 걸고 큐로 되돌린다. **여기가 503 과 결정적으로 다른 지점이다.**

        일시 장애(LLMUnavailable)  attempts 를 **안** 깎고, 이번 바퀴를 **멈춘다**(break)
        영구 오류(그 외 예외)       attempts 를 깎고, **다른 잡을 계속 집는다**

    2026-08-21 사고의 본체가 이 차이였다. 503 이 영구 오류로 분류돼 있어서, 워커가
    모델 로딩 중에 **큐 전체를 훑으며** 잡마다 3번씩 태워 없앴다.
    """
    from lifetrainer.llm import worker as worker_mod

    job_id = queue.enqueue(conn, "summarize_doc", {"doc_id": 1})

    def boom(*_args, **_kwargs):
        raise ValueError("payload 가 잘못됐다")

    monkeypatch.setitem(worker_mod.HANDLERS, "summarize_doc", boom)
    monkeypatch.setattr(worker_mod, "LLMClient", lambda *a, **k: object())
    cfg = _fake_cfg(tmp_path)

    # 백오프를 무시하고 max_attempts 까지 몰아본다.
    for _ in range(5):
        conn.execute("UPDATE job SET not_before = 0 WHERE id = ?", (job_id,))
        conn.commit()
        worker_mod._process_ready_jobs(conn, cfg, worker="test")

    row = _row(conn, job_id)
    assert row["state"] == "failed", "영구 오류가 큐를 영원히 맴돌면 안 된다"
    assert row["attempts"] >= row["max_attempts"]
    assert "payload" in row["error"]


def test_worker_stops_claiming_more_jobs_when_llm_unavailable(conn, tmp_path, monkeypatch):
    """★ 사고의 본체 — LLM 이 죽어 있으면 **뒤 잡을 건드리지 않고 멈춰야** 한다.

    503 이 영구 오류로 분류돼 있던 동안 워커는 큐 전체를 훑었다. 잡 하나가 죽는 게
    아니라 **그 시간대에 대기 중이던 잡 전부**가 시도 횟수를 태웠다. 277건이 그렇게 갔다.
    """
    from lifetrainer.llm import worker as worker_mod
    from lifetrainer.llm.client import LLMUnavailable

    ids = [queue.enqueue(conn, "summarize_doc", {"doc_id": i}) for i in range(5)]
    seen = []

    def boom(_conn, _cfg, _client, job):
        seen.append(job.id)
        raise LLMUnavailable("LLM 서버가 503 를 반환했습니다: Loading model")

    monkeypatch.setitem(worker_mod.HANDLERS, "summarize_doc", boom)
    monkeypatch.setattr(worker_mod, "LLMClient", lambda *a, **k: object())

    worker_mod._process_ready_jobs(conn, _fake_cfg(tmp_path), worker="test")

    assert len(seen) == 1, f"한 건에서 멈춰야 하는데 {len(seen)}건을 집었다"
    others = conn.execute(
        "SELECT COUNT(*) c FROM job WHERE id != ? AND state = 'queued' AND attempts = 0",
        (seen[0],),
    ).fetchone()["c"]
    assert others == len(ids) - 1, "나머지 잡은 손대지 않은 채로 남아 있어야 한다"


# ── health — issues/0016 ─────────────────────────────────────────────────
#
# 누적 실패율은 원인을 고쳐도 `purge_done` 창(14일)이 지나야 내려간다. 그래서
# doctor 가 2주 동안 노란불이었고, 그 사이 새 실패가 들어와도 구분이 안 됐다.
# 아래 테스트가 고정하는 것은 **판정 축이 "지금 실패하고 있나" 라는 것**이다.


def _finished(conn, *, state, kind="summarize_doc", at, n=1):
    """끝난 잡 n 건을 직접 심는다 (실제 워커 없이 시간축을 만들기 위해)."""
    for _ in range(n):
        conn.execute(
            "INSERT INTO job (kind, payload_json, priority, state, attempts,"
            " max_attempts, not_before, lease_until, created_at, finished_at)"
            " VALUES (?, '{}', 100, ?, 1, 3, 0, 0, ?, ?)",
            (kind, state, at - 60, at),
        )
    conn.commit()


def test_health_burst_that_stopped_is_not_ongoing_failure(conn):
    """★ 이 테스트가 issues/0016 그 자체다.

    엿새 전에 203건이 무더기로 죽었고 그 뒤로 계속 성공했다. 누적 실패율은
    여전히 높지만 **지금 실패하고 있지는 않다.** 둘을 구분하지 못하면
    doctor 가 상수처럼 노란불이 되고, 그러면 사람이 doctor 를 안 본다.
    """
    now = now_ts()
    _finished(conn, state="failed", at=now - 6 * 86400, n=203)
    _finished(conn, state="done", at=now - 2 * 86400, n=595)

    h = queue.health(conn)

    assert h.recent_rate > 0.10, "누적/창 비율만 보면 여전히 높다"
    assert h.hours_since_last_failure > 48, "그러나 마지막 실패는 이틀보다 오래됐다"
    assert h.success_since_last_failure == 595, "그 뒤로 계속 성공했다"


def test_health_counts_only_failures_inside_the_window(conn):
    now = now_ts()
    _finished(conn, state="failed", at=now - 30 * 86400, n=50)   # 창 밖
    _finished(conn, state="done", at=now - 1 * 86400, n=10)

    h = queue.health(conn, window_days=7)

    assert h.recent_failed == 0
    assert h.recent_finished == 10


def test_health_reports_backlog_that_no_failure_rate_would_show(conn):
    """워커가 죽으면 실패가 아니라 `queued` 가 쌓인다 — 실패율은 0 인 채로."""
    queue.enqueue(conn, "summarize_doc", {"doc_id": 1})
    conn.execute("UPDATE job SET created_at = ? WHERE state = 'queued'", (now_ts() - 9 * 3600,))
    conn.commit()

    h = queue.health(conn)

    assert h.recent_rate == 0.0
    assert h.oldest_queued_age > 8 * 3600


def test_health_reports_running_job_whose_lease_expired(conn):
    """집다 만 잡. 실패도 대기도 아니라 어느 집계에도 안 잡힌다."""
    queue.enqueue(conn, "summarize_doc", {"doc_id": 1})
    conn.execute(
        "UPDATE job SET state = 'running', lease_until = ? WHERE state = 'queued'",
        (now_ts() - 3600,),
    )
    conn.commit()

    assert queue.health(conn).stale_running == 1


def test_health_names_the_worst_kind(conn):
    now = now_ts()
    _finished(conn, state="done", kind="tag_activity", at=now - 3600, n=20)
    _finished(conn, state="failed", kind="summarize_doc", at=now - 3600, n=4)
    _finished(conn, state="done", kind="summarize_doc", at=now - 3600, n=1)

    kind, failed, finished = queue.health(conn).worst_kind

    assert (kind, failed, finished) == ("summarize_doc", 4, 5)


def test_health_empty_queue_has_no_last_failure(conn):
    h = queue.health(conn)

    assert h.hours_since_last_failure is None
    assert h.recent_rate == 0.0
