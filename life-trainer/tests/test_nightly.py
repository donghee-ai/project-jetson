"""lifetrainer.llm.nightly 테스트 — 야간 배치 적재.

네트워크·LLM 없음. 여기서 검증하는 계약:

- **적재만 한다.** 이 모듈은 LLM 을 부르지 않는다 (부르는 것은 상시 워커다).
- **두 번 돌아도 두 번 안 들어간다.** 타이머가 재실행되거나 사람이 손으로 한 번
  더 돌리는 일은 실제로 일어난다 — `dedupe_key` 가 그걸 막는지 본다.
- **요약 대상 선별 조건**(`summary IS NULL`·`state='scored'`·`dup_of IS NULL`)이
  실제 쿼리와 일치하는지. 이 조건이 틀리면 이미 요약한 문서를 20초씩 다시 요약한다.
"""

from __future__ import annotations

import dataclasses

import pytest

from lifetrainer import db
from lifetrainer.config import load_config
from lifetrainer.llm.nightly import (
    BATCH_PRIORITY,
    cancel_pending_batch,
    enqueue_nightly,
    pending_docs,
    untagged_count,
)
from lifetrainer.timeutil import now_ts


@pytest.fixture()
def cfg(tmp_path):
    base = load_config()
    return dataclasses.replace(base, db_path=tmp_path / "lt.db", data_dir=tmp_path)


@pytest.fixture()
def conn(cfg):
    c = db.connect(cfg.db_path)
    db.init_db(c)
    yield c
    c.close()


def _doc(conn, *, title, score, state="scored", summary=None, dup_of=None):
    cur = conn.execute(
        "INSERT INTO doc(url, title, fetched_at, content_hash, score, scored_at,"
        " summary, dup_of, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            f"https://example.com/{title}",
            title,
            now_ts(),
            f"hash-{title}",
            score,
            now_ts(),
            summary,
            dup_of,
            state,
        ),
    )
    conn.commit()
    return cur.lastrowid


def _unclassified(conn, fingerprint, *, tagged=False):
    conn.execute(
        "INSERT INTO unclassified(fingerprint, app, title_sample, seconds_total, hits,"
        " first_seen, last_seen, llm_category) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (fingerprint, fingerprint, fingerprint, 600, 1, now_ts(), now_ts(),
         "gaming" if tagged else None),
    )
    conn.commit()


# ── pending_docs ─────────────────────────────────────────────────────────


def test_pending_docs_picks_highest_score_first(conn):
    _doc(conn, title="low", score=1.0)
    high = _doc(conn, title="high", score=9.0)
    mid = _doc(conn, title="mid", score=5.0)
    assert pending_docs(conn, 2) == [high, mid]


def test_pending_docs_skips_already_summarized(conn):
    _doc(conn, title="done", score=9.0, summary="이미 요약됨", state="summarized")
    fresh = _doc(conn, title="fresh", score=1.0)
    assert pending_docs(conn, 10) == [fresh]


def test_pending_docs_skips_unscored_and_duplicates(conn):
    original = _doc(conn, title="original", score=5.0)
    _doc(conn, title="new-state", score=9.0, state="new")
    _doc(conn, title="dup", score=9.0, dup_of=original)
    assert pending_docs(conn, 10) == [original]


def test_pending_docs_zero_limit_returns_empty(conn):
    _doc(conn, title="a", score=1.0)
    assert pending_docs(conn, 0) == []


# ── enqueue_nightly ──────────────────────────────────────────────────────


def test_enqueue_nightly_queues_summaries_and_one_tag_job(conn, cfg):
    for i in range(3):
        _doc(conn, title=f"d{i}", score=float(i))
    _unclassified(conn, "VALORANT-Win64-Shipping.exe")

    result = enqueue_nightly(conn, cfg, summary_limit=2, tag_limit=20)

    assert result["summaries"] == 2
    assert result["tags"] == 1
    assert result["untagged"] == 1
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM job ORDER BY id")]
    assert kinds == ["summarize_doc", "summarize_doc", "tag_activity"]


def test_enqueue_nightly_uses_batch_priority_behind_reminders(conn, cfg):
    """예약 알림(기본 100)이 배치보다 먼저 나가야 한다 — 시각이 곧 약속이다."""
    _doc(conn, title="a", score=1.0)
    enqueue_nightly(conn, cfg, summary_limit=1, tag_limit=0)
    row = conn.execute("SELECT priority FROM job").fetchone()
    assert row["priority"] == BATCH_PRIORITY
    assert BATCH_PRIORITY > 100


def test_enqueue_nightly_is_idempotent(conn, cfg):
    """두 번 돌려도 같은 문서가 두 번 큐에 들어가지 않는다."""
    _doc(conn, title="a", score=1.0)
    _unclassified(conn, "MapleStory.exe")

    first = enqueue_nightly(conn, cfg, summary_limit=5, tag_limit=20)
    second = enqueue_nightly(conn, cfg, summary_limit=5, tag_limit=20)

    assert first["summaries"] == 1
    assert second["summaries"] == 0
    assert second["summaries_skipped"] == 1
    assert second["tags"] == 0  # 같은 날 태깅 잡은 하루에 하나
    assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 2


def test_enqueue_nightly_skips_tag_job_when_nothing_untagged(conn, cfg):
    _unclassified(conn, "already", tagged=True)
    result = enqueue_nightly(conn, cfg, summary_limit=0, tag_limit=20)
    assert result["untagged"] == 0
    assert result["tags"] == 0
    assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 0


def test_enqueue_nightly_tag_limit_zero_disables_tagging(conn, cfg):
    _unclassified(conn, "VALORANT-Win64-Shipping.exe")
    result = enqueue_nightly(conn, cfg, summary_limit=0, tag_limit=0)
    assert result["untagged"] == 1
    assert result["tags"] == 0


def test_enqueue_nightly_defaults_come_from_config(conn, cfg):
    for i in range(30):
        _doc(conn, title=f"d{i}", score=float(i))
    tight = dataclasses.replace(
        cfg, nightly=dataclasses.replace(cfg.nightly, summary_limit=3, tag_limit=0)
    )
    result = enqueue_nightly(conn, tight)
    assert result["summaries"] == 3


def test_enqueue_nightly_payload_carries_doc_id(conn, cfg):
    doc_id = _doc(conn, title="a", score=1.0)
    enqueue_nightly(conn, cfg, summary_limit=1, tag_limit=0)
    row = conn.execute("SELECT payload_json FROM job").fetchone()
    assert str(doc_id) in row["payload_json"]


def test_untagged_count_counts_only_untagged(conn):
    _unclassified(conn, "a")
    _unclassified(conn, "b", tagged=True)
    assert untagged_count(conn) == 1


# ── 새벽 창 종료 (--stop) ────────────────────────────────────────────────
#
# 요약 600건은 약 3.3시간이라 보통 05:20 쯤 끝나지만, 문서가 길어 느려진 밤에는
# 낮까지 샐 수 있다. 05:50 타이머가 남은 잡을 비운다.


def test_cancel_pending_batch_removes_queued_batch_jobs(conn, cfg):
    _doc(conn, title="a", score=1.0)
    _doc(conn, title="b", score=2.0)
    _unclassified(conn, "MysteryApp.exe")
    enqueue_nightly(conn, cfg, summary_limit=2, tag_limit=20)

    dropped = cancel_pending_batch(conn)

    assert dropped == 3  # 요약 2 + 태깅 1
    assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 0


def test_cancel_pending_batch_leaves_reminders_alone(conn, cfg):
    """예약 알림은 사람에게 한 약속이다. 배치를 접는다고 같이 지우면 안 된다."""
    from lifetrainer.llm.queue import enqueue

    _doc(conn, title="a", score=1.0)
    enqueue_nightly(conn, cfg, summary_limit=1, tag_limit=0)
    enqueue(conn, "reminder", {"text": "약 먹기"}, dedupe_key="r1")

    cancel_pending_batch(conn)

    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM job")]
    assert kinds == ["reminder"]


def test_cancel_pending_batch_does_not_touch_running(conn, cfg):
    """진행 중인 요약 1건(약 20초)은 끝내는 게 맞다."""
    from lifetrainer.llm.queue import claim

    _doc(conn, title="a", score=1.0)
    _doc(conn, title="b", score=2.0)
    enqueue_nightly(conn, cfg, summary_limit=2, tag_limit=0)
    claimed = claim(conn, worker="w", kinds=["summarize_doc"])

    cancel_pending_batch(conn)

    rows = conn.execute("SELECT id, state FROM job").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == claimed.id
    assert rows[0]["state"] == "running"


def test_cancelled_docs_can_be_requeued_next_night(conn, cfg):
    """★ 삭제가 아니라 'cancelled' 로 두면 dedupe_key 가 남아 **영영 다시 못 들어간다.**"""
    _doc(conn, title="a", score=1.0)
    assert enqueue_nightly(conn, cfg, summary_limit=1, tag_limit=0)["summaries"] == 1
    cancel_pending_batch(conn)
    assert enqueue_nightly(conn, cfg, summary_limit=1, tag_limit=0)["summaries"] == 1
