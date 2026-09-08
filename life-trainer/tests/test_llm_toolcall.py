"""lifetrainer.llm.client 의 툴 콜링 경로 테스트 + Slack 대화 핸들러 테스트.

네트워크 금지. 가짜 `requests.Session` 과 가짜 Slack client 로만 돈다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from lifetrainer.llm.client import LLMClient, _parse_tool_calls, _truncate_messages, estimate_tokens


@dataclass
class _FakeLLMConfig:
    base_url: str = "http://fake-llama.local/v1"
    model: str = "qwen3-8b"
    timeout_sec: float = 5.0
    max_input_tokens: int = 4000
    enable_thinking: bool = True  # 툴을 쓰면 무조건 꺼져야 한다는 걸 보기 위해 켜 둔다


@dataclass
class _FakeConfig:
    llm: _FakeLLMConfig = field(default_factory=_FakeLLMConfig)


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class _CapturingSession:
    """마지막으로 보낸 payload 를 잡아두는 가짜 세션."""

    def __init__(self, message: dict | None = None) -> None:
        self.sent: dict = {}
        self._message = message or {"role": "assistant", "content": "안녕"}

    def post(self, url, json=None, timeout=None, **kw):  # noqa: A002, ANN001 - requests 시그니처를 따라간다
        self.sent = json
        return _FakeResponse(
            {"choices": [{"message": self._message, "finish_reason": "stop"}], "usage": {}}
        )


TOOL = {
    "type": "function",
    "function": {
        "name": "get_activity_summary",
        "description": "활동 요약",
        "parameters": {"type": "object", "properties": {"day": {"type": "string"}}, "required": []},
    },
}


# ── payload 조립 ─────────────────────────────────────────────────────────


def test_tools_는_payload_에_실린다():
    session = _CapturingSession()
    client = LLMClient(_FakeConfig(), session=session)
    client.chat([{"role": "user", "content": "안녕"}], tools=[TOOL])
    assert session.sent["tools"] == [TOOL]


def test_툴을_쓰면_thinking_이_꺼진다():
    """사고 텍스트가 섞이면 툴콜 파싱이 깨진다 (계약서 §0)."""
    session = _CapturingSession()
    client = LLMClient(_FakeConfig(), session=session)
    client.chat([{"role": "user", "content": "안녕"}], tools=[TOOL])
    assert session.sent["chat_template_kwargs"]["enable_thinking"] is False


def test_툴이_없으면_cfg_값을_따른다():
    session = _CapturingSession()
    client = LLMClient(_FakeConfig(), session=session)
    client.chat([{"role": "user", "content": "안녕"}])
    assert session.sent["chat_template_kwargs"]["enable_thinking"] is True


def test_툴_스키마의_pattern_은_발신_전에_막힌다():
    """정규식 하나가 요청 전체를 400 으로 만든다 (operate/notes/agent-gateway.md §4-5)."""
    bad = json.loads(json.dumps(TOOL))
    bad["function"]["parameters"]["properties"]["day"]["pattern"] = r"^\d{4}$"
    client = LLMClient(_FakeConfig(), session=_CapturingSession())
    with pytest.raises(ValueError, match="pattern"):
        client.chat([{"role": "user", "content": "안녕"}], tools=[bad])


def test_tool_choice_는_줄_때만_실린다():
    session = _CapturingSession()
    client = LLMClient(_FakeConfig(), session=session)
    client.chat([{"role": "user", "content": "안녕"}], tools=[TOOL])
    assert "tool_choice" not in session.sent
    client.chat([{"role": "user", "content": "안녕"}], tools=[TOOL], tool_choice="auto")
    assert session.sent["tool_choice"] == "auto"


# ── 입력 예산 ────────────────────────────────────────────────────────────


def test_툴_토큰이_입력_예산에서_빠진다():
    """툴은 payload 로 따로 가지만 채팅 템플릿이 시스템 뒤에 렌더링해 컨텍스트를 먹는다."""
    tool_tokens = estimate_tokens(json.dumps([TOOL], ensure_ascii=False))
    text = "가" * 500
    # ★ 예산을 계수에서 **유도한다.** 숫자를 박아 두면 `estimate_tokens` 를 실측으로
    #   고칠 때마다 이 테스트가 무관하게 깨진다 (2026-08-23 에 실제로 깨졌다).
    budget = tool_tokens + estimate_tokens(text) / 2
    messages = [{"role": "user", "content": text}]

    kept, truncated = _truncate_messages(messages, budget, reserved_tokens=0)
    assert truncated is True
    without = len(kept[0]["content"])

    kept2, truncated2 = _truncate_messages(messages, budget, reserved_tokens=tool_tokens)
    assert truncated2 is True
    assert kept2, "툴 예산을 빼도 본문이 통째로 사라지면 안 된다"
    assert len(kept2[0]["content"]) < without


def test_툴콜_메시지는_잘리지_않고_통째로_버려진다():
    """자르면 tool_calls 짝이 깨져 서버가 400 을 낸다."""
    messages = [
        {"role": "system", "content": "짧음"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "가" * 500},
    ]
    kept, truncated = _truncate_messages(messages, 10)
    assert truncated is True
    assert all(m.get("role") != "tool" for m in kept)
    assert all("tool_calls" not in m for m in kept)


# ── 응답 파싱 ────────────────────────────────────────────────────────────


def test_툴콜_응답을_파싱한다():
    session = _CapturingSession(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_a",
                    "type": "function",
                    "function": {"name": "get_activity_summary", "arguments": '{"day": "2026-08-17"}'},
                }
            ],
        }
    )
    client = LLMClient(_FakeConfig(), session=session)
    resp = client.chat([{"role": "user", "content": "오늘?"}], tools=[TOOL])
    assert len(resp.tool_calls) == 1
    call = resp.tool_calls[0]
    assert call.name == "get_activity_summary"
    assert call.arguments == {"day": "2026-08-17"}
    assert call.parse_error is None


def test_깨진_인자는_예외가_아니라_parse_error():
    calls = _parse_tool_calls(
        {"tool_calls": [{"id": "c1", "function": {"name": "t", "arguments": "{깨짐"}}]}
    )
    assert len(calls) == 1
    assert calls[0].arguments == {}
    assert "파싱 실패" in calls[0].parse_error


def test_인자가_객체로_와도_받아준다():
    """스펙 밖이지만 실제로 이렇게 주는 구현이 있다."""
    calls = _parse_tool_calls(
        {"tool_calls": [{"id": "c1", "function": {"name": "t", "arguments": {"day": "x"}}}]}
    )
    assert calls[0].arguments == {"day": "x"}
    assert calls[0].parse_error is None


def test_이름_없는_툴콜은_버린다():
    calls = _parse_tool_calls({"tool_calls": [{"id": "c1", "function": {"arguments": "{}"}}]})
    assert calls == []


def test_툴콜이_없으면_빈_목록():
    assert _parse_tool_calls({"role": "assistant", "content": "안녕"}) == []


def test_id_가_없으면_만들어_붙인다():
    calls = _parse_tool_calls({"tool_calls": [{"function": {"name": "t", "arguments": "{}"}}]})
    assert calls[0].id == "call_0"


def test_토큰_추정은_실제보다_웃돈다():
    """★ 이 추정으로 입력을 자르므로 밑돌면 `max_input_tokens` 가 이름만 남는다.

    옛 값(한글 0.6 · 그 외 0.15)은 "넉넉하게 잡는다" 고 적어 놓고 실제로는
    최악 3.23배 밑돌았다. 특히 **영문**이 심했다 (실측 문자당 0.42 인데 0.15 로 잡음).
    여기 숫자는 2026-08-23 젯슨에서 llama-server `/tokenize` 로 잰 하한이다.
    """
    # (텍스트, 실제 토큰 수) — 실측값. 추정이 이보다 작아지면 안 된다.
    cases = [
        ("Open-source LLMs like Falcon, (Open-)LLaMA, X-Gen, StarCoder or RedPajama, "
         "have come a long way in recent months.", 28),
        ("이 문서는 인텔 CPU 를 사용한 스테이블 디퓨전 추론 속도를 높이는 방법을 다룹니다.", 30),
    ]
    for text, actual in cases:
        assert estimate_tokens(text) >= actual, f"밑돈다: {text[:30]!r}"
