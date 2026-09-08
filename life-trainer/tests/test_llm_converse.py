"""lifetrainer.llm.converse 테스트.

네트워크 금지 — `LLMClient.chat` 을 스크립트된 가짜로 갈아끼워 돈다.
실제 llama-server 는 절대 부르지 않는다.

이 파일이 지키려는 계약:
- **상한에 걸리면 지어내지 않는다.** 툴만 반복 호출하다 끝나면 그렇다고 말해야지,
  그럴듯한 문장을 내놓으면 안 된다 (소형 모델의 대표 실패 모드).
- **3층 배치가 유지된다.** 계획은 시스템, 실측·현재 시각은 user 메시지.
- **툴 인자가 깨져도 대화가 죽지 않는다.**
"""

from __future__ import annotations

import dataclasses

import pytest

from lifetrainer import db
from lifetrainer.config import load_config
from lifetrainer.llm.client import LLMResponse, LLMUnavailable, ToolCall
from lifetrainer.llm.converse import converse
from lifetrainer.plan import models as plan_models

DAY = "2026-08-17"


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


def _text_response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        prompt_tokens=10,
        completion_tokens=5,
        latency_ms=1,
        raw={"choices": [{"message": {"role": "assistant", "content": text}}]},
        tool_calls=[],
        finish_reason="stop",
    )


def _tool_response(name: str, args_raw: str, *, parse_error: str | None = None) -> LLMResponse:
    import json

    try:
        args = json.loads(args_raw)
    except ValueError:
        args = {}
    raw_message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": name, "arguments": args_raw}}
        ],
    }
    return LLMResponse(
        text="",
        prompt_tokens=10,
        completion_tokens=5,
        latency_ms=1,
        raw={"choices": [{"message": raw_message}]},
        tool_calls=[
            ToolCall(id="c1", name=name, arguments=args, arguments_raw=args_raw, parse_error=parse_error)
        ],
        finish_reason="tool_calls",
    )


class FakeClient:
    """스크립트된 응답을 순서대로 돌려주는 가짜 LLM 클라이언트."""

    def __init__(self, responses: list, *, raises: Exception | None = None) -> None:
        self._responses = list(responses)
        self._raises = raises
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):  # noqa: ANN001
        self.calls.append({"messages": list(messages), **kwargs})
        if self._raises is not None:
            raise self._raises
        if not self._responses:
            return _text_response("(스크립트 소진)")
        return self._responses.pop(0)


# ── 기본 흐름 ────────────────────────────────────────────────────────────


def test_툴_없이_바로_답하면_한_라운드(conn, cfg):
    client = FakeClient([_text_response("오늘 코딩 44분 했어.")])
    result = converse(conn, cfg, client, user_text="오늘 코딩 얼마나 했어?", day=DAY)
    assert result.text == "오늘 코딩 44분 했어."
    assert result.rounds == 1
    assert result.tools_used == []
    assert result.ok is True


def test_툴을_부르면_실행하고_다시_묻는다(conn, cfg):
    client = FakeClient(
        [
            _tool_response("get_activity_summary", '{"day": "2026-08-16"}'),
            _text_response("8월 16일엔 활동이 없었어."),
        ]
    )
    result = converse(conn, cfg, client, user_text="16일에 뭐 했어?", day=DAY)
    assert result.tools_used == ["get_activity_summary"]
    assert result.rounds == 2
    assert result.ok is True

    # 두 번째 호출에는 어시스턴트 툴콜 + 툴 결과가 이어 붙어 있어야 한다.
    second = client.calls[1]["messages"]
    assert second[-2]["role"] == "assistant"
    assert second[-1]["role"] == "tool"
    assert second[-1]["tool_call_id"] == "c1"


def test_상한까지_툴만_부르면_지어내지_않는다(conn, cfg):
    """소형 모델이 툴만 반복 호출하는 경우. 그럴듯한 문장 대신 사실을 말해야 한다."""
    client = FakeClient([_tool_response("get_activity_summary", "{}") for _ in range(5)])
    result = converse(conn, cfg, client, user_text="오늘 어때?", day=DAY, max_rounds=3)
    assert result.rounds == 3
    assert result.ok is False
    assert "정리하지 못했" in result.text
    assert len(result.tools_used) == 3


def test_LLM_이_죽어_있으면_안내하고_ok_는_False(conn, cfg):
    client = FakeClient([], raises=LLMUnavailable("연결 실패"))
    result = converse(conn, cfg, client, user_text="오늘 뭐 했지?", day=DAY)
    assert result.ok is False
    assert "연결할 수 없습니다" in result.text
    assert result.rounds == 0


def test_툴_인자가_깨져도_대화가_죽지_않는다(conn, cfg):
    client = FakeClient(
        [
            _tool_response("get_activity_summary", "{깨진 json", parse_error="인자 JSON 파싱 실패"),
            _text_response("다시 물어봐 줄래?"),
        ]
    )
    result = converse(conn, cfg, client, user_text="오늘 어때?", day=DAY)
    assert result.ok is True
    assert result.tools_used == []  # 실행되지 않았다
    tool_msg = client.calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert "인자를 읽지 못했습니다" in tool_msg["content"]


# ── 3층 프롬프트 배치 ────────────────────────────────────────────────────


def test_계획은_시스템에_실측은_user_에_들어간다(conn, cfg):
    plan_models.add_instance(conn, cfg, DAY, title="딥워크", start_min=540, end_min=720)
    client = FakeClient([_text_response("ok")])
    converse(conn, cfg, client, user_text="오늘 어때?", day=DAY)

    messages = client.calls[0]["messages"]
    system, user = messages[0], messages[-1]
    assert system["role"] == "system"
    assert "딥워크" in system["content"]
    assert "기계 측정" not in system["content"]

    assert user["role"] == "user"
    assert "기계 측정" in user["content"]
    assert "오늘 어때?" in user["content"]


def test_시스템_프롬프트는_짧게_유지된다(conn, cfg):
    """OpenClaw 는 12,541 토큰이라 매 턴 25~40초였다. 여기 상한은 그것과의 차이다."""
    from lifetrainer.llm.client import estimate_tokens

    for i in range(6):
        plan_models.add_instance(conn, cfg, DAY, title=f"계획{i}", start_min=i * 60, end_min=i * 60 + 30)
    client = FakeClient([_text_response("ok")])
    converse(conn, cfg, client, user_text="오늘 어때?", day=DAY)

    system = client.calls[0]["messages"][0]["content"]
    assert estimate_tokens(system) < 1000


# ── 트리거 연동 ──────────────────────────────────────────────────────────


def test_평범한_질문에는_쓰기_툴이_실리지_않는다(conn, cfg):
    client = FakeClient([_text_response("ok")])
    result = converse(conn, cfg, client, user_text="오늘 뭐 했지?", day=DAY)
    assert result.triggers_fired == []
    assert "schedule_reminder" not in result.tools_offered
    assert "add_plan" not in result.tools_offered


def test_예약_표현이면_예약_툴이_실린다(conn, cfg):
    client = FakeClient([_text_response("ok")])
    result = converse(conn, cfg, client, user_text="10분 뒤에 알려줘", day=DAY)
    assert "reminder" in result.triggers_fired
    assert "schedule_reminder" in result.tools_offered
    names = [s["function"]["name"] for s in client.calls[0]["tools"]]
    assert "schedule_reminder" in names


def test_예약_툴이_실제로_큐에_넣는다(conn, cfg):
    from lifetrainer.llm.tools import REMINDER_KIND

    client = FakeClient(
        [
            _tool_response("schedule_reminder", '{"text": "스트레칭", "delay_minutes": 10}'),
            _text_response("10분 뒤에 알려줄게."),
        ]
    )
    result = converse(
        conn, cfg, client, user_text="10분 뒤에 스트레칭하라고 알려줘", day=DAY, channel="D_X"
    )
    assert result.ok is True
    row = conn.execute("SELECT state, payload_json FROM job WHERE kind = ?", (REMINDER_KIND,)).fetchone()
    assert row["state"] == "queued"
    assert "D_X" in row["payload_json"]


# ── 대화 이력 ────────────────────────────────────────────────────────────


def test_이력이_시스템과_현재질문_사이에_들어간다(conn, cfg):
    client = FakeClient([_text_response("ok")])
    history = [
        {"role": "user", "content": "어제 뭐 했지?"},
        {"role": "assistant", "content": "코딩 2시간."},
    ]
    converse(conn, cfg, client, user_text="오늘은?", day=DAY, history=history)

    messages = client.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[1] == history[0]
    assert messages[2] == history[1]
    assert "오늘은?" in messages[3]["content"]


def test_이력이_길면_최근_것만_남는다(conn, cfg):
    from lifetrainer.llm.converse import MAX_HISTORY_MESSAGES

    client = FakeClient([_text_response("ok")])
    history = [{"role": "user", "content": f"질문{i}"} for i in range(20)]
    converse(conn, cfg, client, user_text="지금", day=DAY, history=history)

    messages = client.calls[0]["messages"]
    assert len(messages) == 1 + MAX_HISTORY_MESSAGES + 1
    assert messages[1]["content"] == "질문14"


# ── 직전 답 반복 (2026-08-22) ─────────────────────────────────────────────
# 8B 는 되묻기("그래서?")에 직전 답을 그대로 복사한다. 프롬프트로 안 돼서 구조로 막았다.


def test_반복_판정은_문장부호_차이를_무시한다():
    from lifetrainer.llm.converse import _is_repeat

    assert _is_repeat("지금은 문서 정리 시간입니다.", "지금은 문서 정리 시간입니다!")
    assert _is_repeat("50분 남았어요", "50분 남았어요.")


def test_다른_답은_반복이_아니다():
    from lifetrainer.llm.converse import _is_repeat

    assert not _is_repeat("오늘 코딩 2시간 27분 했어요.", "지금은 문서 정리 시간입니다.")
    assert not _is_repeat("", "무엇이든")
    assert not _is_repeat("무엇이든", "")


def test_직전_어시스턴트_답을_찾는다():
    from lifetrainer.llm.converse import _last_assistant

    hist = [
        {"role": "user", "content": "심심해"},
        {"role": "assistant", "content": "첫 답"},
        {"role": "user", "content": "그래서?"},
    ]
    assert _last_assistant(hist) == "첫 답"
    assert _last_assistant([]) == ""
