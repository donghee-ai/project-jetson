"""lifetrainer.llm.interactive + 워커의 대화 우선권 테스트.

여기서 지키려는 계약:

- **대화 중에는 GPU 잡을 집지 않는다.** llama-server 가 `--parallel 1` 이라
  요약 1건(약 20초)을 집는 순간 사용자 질문이 그 뒤에 선다.
- **알림은 미루지 않는다.** `reminder` 는 LLM 을 안 쓰고 시각이 곧 약속이다.
  "10분 뒤 알려줘"가 대화 때문에 늦으면 그건 기능이 고장난 것이다.
- **락은 편의 기능이다.** 락 파일을 못 열어도 대화는 진행되고 배치도 멈추지 않는다.

flock 은 열린 파일 서술자 단위라, 같은 프로세스라도 다른 `open()` 으로 잡은
공유 락과 배타 락은 서로 충돌한다 — 그래서 이 검증이 한 프로세스 안에서 성립한다.
"""

from __future__ import annotations

import dataclasses

import pytest

from lifetrainer import db
from lifetrainer.config import load_config
from lifetrainer.llm.interactive import interactive_busy, interactive_turn, lock_path
from lifetrainer.llm.worker import GPU_KINDS, HANDLERS, _claimable_kinds


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


# ── 락 자체 ──────────────────────────────────────────────────────────────


def test_not_busy_when_no_conversation(cfg):
    assert interactive_busy(cfg) is False


def test_busy_while_turn_is_held(cfg):
    with interactive_turn(cfg) as acquired:
        assert acquired is True
        assert interactive_busy(cfg) is True


def test_not_busy_after_turn_ends(cfg):
    with interactive_turn(cfg):
        pass
    assert interactive_busy(cfg) is False


def test_two_turns_do_not_block_each_other(cfg):
    """공유 락이라 대화끼리는 서로를 막지 않는다 (두 채널 동시 대화)."""
    with interactive_turn(cfg) as first, interactive_turn(cfg) as second:
        assert first is True
        assert second is True


def test_turn_releases_lock_on_exception(cfg):
    with pytest.raises(RuntimeError):
        with interactive_turn(cfg):
            raise RuntimeError("대화 도중 실패")
    assert interactive_busy(cfg) is False


def test_lock_lives_in_data_dir(cfg):
    assert lock_path(cfg).parent == cfg.data_dir


def test_busy_is_false_when_lock_file_unavailable(cfg, monkeypatch):
    """락 파일을 못 열면 배치를 멈추지 않는다 — 대화가 20초 밀리는 편이 낫다."""

    def boom(*args, **kwargs):
        raise OSError("열 수 없음")

    monkeypatch.setattr("builtins.open", boom)
    assert interactive_busy(cfg) is False


def test_turn_proceeds_when_lock_unavailable(cfg, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("열 수 없음")

    monkeypatch.setattr("builtins.open", boom)
    with interactive_turn(cfg) as acquired:
        assert acquired is False  # 락 없이도 대화는 계속된다


# ── 워커 쪽 ──────────────────────────────────────────────────────────────


def test_worker_takes_everything_when_idle(cfg):
    assert set(_claimable_kinds(cfg)) == set(HANDLERS)


def test_worker_defers_gpu_kinds_during_conversation(cfg):
    with interactive_turn(cfg):
        kinds = set(_claimable_kinds(cfg))
    assert kinds.isdisjoint(GPU_KINDS)
    assert "summarize_doc" not in kinds
    assert "tag_activity" not in kinds


def test_worker_still_takes_reminders_during_conversation(cfg):
    with interactive_turn(cfg):
        assert "reminder" in _claimable_kinds(cfg)


def test_gpu_kinds_match_handlers_that_use_llm(cfg):
    """새 핸들러를 추가하면서 GPU_KINDS 갱신을 잊으면 여기서 걸린다."""
    assert GPU_KINDS <= set(HANDLERS)
    assert GPU_KINDS == {"summarize_doc", "tag_activity"}


def test_deferred_job_is_not_claimed_but_reminder_is(conn, cfg):
    """실제 큐로 확인 — 대화 중에는 요약이 아니라 알림이 집힌다."""
    from lifetrainer.llm.queue import claim, enqueue

    enqueue(conn, "summarize_doc", {"doc_id": 1}, priority=200)
    enqueue(conn, "reminder", {"text": "물 마시기"}, priority=100)

    with interactive_turn(cfg):
        job = claim(conn, worker="t", kinds=_claimable_kinds(cfg))
        assert job is not None
        assert job.kind == "reminder"

        # 알림을 처리한 뒤에는 대화 중이므로 더 집을 것이 없다
        assert claim(conn, worker="t", kinds=_claimable_kinds(cfg)) is None

    # 대화가 끝나면 요약이 집힌다
    job = claim(conn, worker="t", kinds=_claimable_kinds(cfg))
    assert job is not None and job.kind == "summarize_doc"
