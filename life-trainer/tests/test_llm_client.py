"""lifetrainer.llm.client 테스트.

네트워크 금지 — 전부 가짜 `requests.Session` 을 주입해서 돈다. 실제 llama-server 는
절대 호출하지 않는다 (개발 중 수동 확인은 스크립트로 따로 한다).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import pytest
import requests
from pydantic import BaseModel, Field

from lifetrainer import db
from lifetrainer.llm.client import (
    LLMClient,
    LLMError,
    LLMResponse,
    LLMUnavailable,
    _check_no_pattern,
    estimate_tokens,
)
from lifetrainer.llm.schemas import ActivityTag, json_schema_of


# ── 가짜 설정 / 가짜 세션 ────────────────────────────────────────────────


@dataclass
class _FakeLLMConfig:
    base_url: str = "http://fake-llama.local/v1"
    model: str = "qwen3-8b"
    timeout_sec: float = 5.0
    max_input_tokens: int = 4000
    enable_thinking: bool = False


@dataclass
class _FakeConfig:
    llm: _FakeLLMConfig = field(default_factory=_FakeLLMConfig)


class FakeResponse:
    """`requests.Response` 를 흉내내는 최소 더미."""

    def __init__(self, status_code: int = 200, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text or (json.dumps(json_data, ensure_ascii=False) if json_data is not None else "")

    def json(self) -> dict:
        if self._json_data is None:
            raise ValueError("가짜 응답에 JSON 이 없습니다")
        return self._json_data


class FakeSession:
    """`requests.Session.post`/`get` 를 흉내내는 가짜 세션. 네트워크를 절대 타지 않는다."""

    def __init__(
        self,
        *,
        post_response: FakeResponse | None = None,
        post_exc: Exception | None = None,
        get_response: FakeResponse | None = None,
        get_exc: Exception | None = None,
    ):
        self.post_response = post_response
        self.post_exc = post_exc
        self.get_response = get_response
        self.get_exc = get_exc
        self.last_url: str | None = None
        self.last_payload: dict | None = None
        self.get_urls: list[str] = []

    def post(self, url, json=None, timeout=None, **kw):  # noqa: A002 - requests API 시그니처를 흉내낸다
        self.last_url = url
        self.last_payload = json
        if self.post_exc is not None:
            raise self.post_exc
        return self.post_response

    def get(self, url, timeout=None):
        self.get_urls.append(url)
        if self.get_exc is not None:
            raise self.get_exc
        return self.get_response


def _ok_chat_response(content: str = "안녕하세요", *, prompt_tokens=10, completion_tokens=5) -> FakeResponse:
    return FakeResponse(
        200,
        {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        },
    )


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "lt.db")
    db.init_db(c)
    yield c
    c.close()


# ── pattern 검증기 (핵심 요구사항) ───────────────────────────────────────


def test_check_no_pattern_passes_for_clean_schema():
    schema = {"type": "object", "properties": {"x": {"type": "string", "minLength": 1}}}
    _check_no_pattern(schema)  # 예외 없어야 함


def test_check_no_pattern_top_level_raises():
    schema = {"type": "string", "pattern": r"^\S+$"}
    with pytest.raises(ValueError, match="pattern"):
        _check_no_pattern(schema)


def test_check_no_pattern_nested_in_properties_raises():
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "code": {"type": "string", "pattern": r"^[A-Z]+$"},
        },
    }
    with pytest.raises(ValueError, match="pattern"):
        _check_no_pattern(schema)


def test_check_no_pattern_nested_in_items_raises():
    schema = {
        "type": "array",
        "items": {"type": "object", "properties": {"x": {"type": "string", "pattern": r"^a$"}}},
    }
    with pytest.raises(ValueError, match="pattern"):
        _check_no_pattern(schema)


def test_check_no_pattern_nested_in_defs_and_anyof_raises():
    schema = {
        "$defs": {"Inner": {"type": "string", "pattern": "abc"}},
        "anyOf": [{"$ref": "#/$defs/Inner"}, {"type": "null"}],
    }
    with pytest.raises(ValueError, match="pattern"):
        _check_no_pattern(schema)


def test_check_no_pattern_anchored_pattern_still_raises():
    # 앵커(^...$)를 붙여도 llama.cpp GBNF 변환기는 여전히 실패한다 — 있으면 무조건 막는다.
    schema = {"type": "string", "pattern": r"^\S+$"}
    with pytest.raises(ValueError):
        _check_no_pattern(schema)


# ── schemas.json_schema_of 의 pattern 제거 ───────────────────────────────


def _contains_pattern(node) -> bool:
    if isinstance(node, dict):
        if "pattern" in node:
            return True
        return any(_contains_pattern(v) for v in node.values())
    if isinstance(node, list):
        return any(_contains_pattern(v) for v in node)
    return False


class _PatternedInner(BaseModel):
    code: str = Field(pattern=r"^[A-Z]{2,4}$")


class _PatternedOuter(BaseModel):
    inner: _PatternedInner
    items: list[_PatternedInner]


def test_json_schema_of_strips_top_level_pattern():
    schema = json_schema_of(_PatternedInner, "PatternedInner")
    assert not _contains_pattern(schema)
    # pattern 은 사라져도 필드 자체는 남아 있어야 한다.
    assert "code" in schema["properties"]


def test_json_schema_of_strips_nested_pattern_in_defs():
    schema = json_schema_of(_PatternedOuter, "PatternedOuter")
    assert not _contains_pattern(schema)
    assert "$defs" in schema  # 중첩 모델이 $defs 로 빠졌는지 확인 (진짜 nested 케이스인지 검증)


def test_json_schema_of_keeps_min_length_style_constraints():
    class _MinLenModel(BaseModel):
        name: str = Field(min_length=1)

    schema = json_schema_of(_MinLenModel, "MinLenModel")
    assert schema["properties"]["name"]["minLength"] == 1


# ── chat(): thinking 강제 off ────────────────────────────────────────────


def test_chat_json_schema_forces_enable_thinking_false_even_if_cfg_true():
    cfg = _FakeConfig(llm=_FakeLLMConfig(enable_thinking=True))
    session = FakeSession(post_response=_ok_chat_response('{"a": 1}'))
    client = LLMClient(cfg, session=session)

    client.chat(
        [{"role": "user", "content": "hi"}],
        json_schema={"type": "object", "properties": {"a": {"type": "integer"}}},
    )

    assert session.last_payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_chat_without_schema_uses_cfg_enable_thinking_true():
    cfg = _FakeConfig(llm=_FakeLLMConfig(enable_thinking=True))
    session = FakeSession(post_response=_ok_chat_response())
    client = LLMClient(cfg, session=session)

    client.chat([{"role": "user", "content": "hi"}])

    assert session.last_payload["chat_template_kwargs"] == {"enable_thinking": True}


def test_chat_without_schema_uses_cfg_enable_thinking_false():
    cfg = _FakeConfig(llm=_FakeLLMConfig(enable_thinking=False))
    session = FakeSession(post_response=_ok_chat_response())
    client = LLMClient(cfg, session=session)

    client.chat([{"role": "user", "content": "hi"}])

    assert session.last_payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_chat_raises_before_network_when_schema_has_pattern():
    cfg = _FakeConfig()
    session = FakeSession(post_response=_ok_chat_response())
    client = LLMClient(cfg, session=session)

    bad_schema = {"type": "object", "properties": {"x": {"type": "string", "pattern": r"^\S+$"}}}
    with pytest.raises(ValueError):
        client.chat([{"role": "user", "content": "hi"}], json_schema=bad_schema)

    assert session.last_url is None  # 네트워크를 아예 타지 않았어야 함


# ── <think> 제거 ─────────────────────────────────────────────────────────


def test_chat_strips_think_block_from_response():
    cfg = _FakeConfig()
    session = FakeSession(
        post_response=_ok_chat_response("<think>속으로 고민중...</think>실제 답변입니다")
    )
    client = LLMClient(cfg, session=session)

    resp = client.chat([{"role": "user", "content": "hi"}])

    assert resp.text == "실제 답변입니다"
    assert "<think>" not in resp.text


def test_chat_strips_stray_think_tags_without_matching_pair():
    cfg = _FakeConfig()
    session = FakeSession(post_response=_ok_chat_response("</think>정답만 왔습니다"))
    client = LLMClient(cfg, session=session)

    resp = client.chat([{"role": "user", "content": "hi"}])

    assert resp.text == "정답만 왔습니다"


# ── 입력 자르기 (max_input_tokens) ────────────────────────────────────────


def test_estimate_tokens_korean_costs_more_than_english_per_char():
    korean = "안녕하세요오늘도좋은하루" * 3
    english = "a" * len(korean)
    assert estimate_tokens(korean) > estimate_tokens(english)


def test_chat_truncates_input_over_max_input_tokens_and_warns(caplog):
    cfg = _FakeConfig(llm=_FakeLLMConfig(max_input_tokens=5))
    session = FakeSession(post_response=_ok_chat_response())
    client = LLMClient(cfg, session=session)

    long_content = "가" * 200  # 한글 -> 문자당 0.6 토큰 추정, 5 토큰 예산을 크게 초과
    with caplog.at_level(logging.WARNING, logger="lifetrainer.llm.client"):
        client.chat([{"role": "user", "content": long_content}], purpose="test")

    sent_content = session.last_payload["messages"][0]["content"]
    assert len(sent_content) < len(long_content)
    assert estimate_tokens(sent_content) <= cfg.llm.max_input_tokens + 1e-6
    assert any("max_input_tokens" in rec.message for rec in caplog.records)


def test_chat_does_not_truncate_when_within_budget():
    cfg = _FakeConfig(llm=_FakeLLMConfig(max_input_tokens=4000))
    session = FakeSession(post_response=_ok_chat_response())
    client = LLMClient(cfg, session=session)

    content = "짧은 입력입니다"
    client.chat([{"role": "user", "content": content}])

    assert session.last_payload["messages"][0]["content"] == content


# ── llm_call 계측 ────────────────────────────────────────────────────────


def test_chat_records_llm_call_on_success(conn):
    cfg = _FakeConfig()
    session = FakeSession(post_response=_ok_chat_response(prompt_tokens=42, completion_tokens=7))
    client = LLMClient(cfg, conn=conn, session=session)

    client.chat([{"role": "user", "content": "hi"}], purpose="unit-test")

    row = conn.execute("SELECT * FROM llm_call").fetchone()
    assert row is not None
    assert row["ok"] == 1
    assert row["purpose"] == "unit-test"
    assert row["model"] == cfg.llm.model
    assert row["prompt_tokens"] == 42
    assert row["completion_tokens"] == 7
    assert row["latency_ms"] >= 0
    assert row["error"] is None


def test_chat_records_llm_call_on_http_error(conn):
    cfg = _FakeConfig()
    session = FakeSession(post_response=FakeResponse(500, None, text="internal error"))
    client = LLMClient(cfg, conn=conn, session=session)

    with pytest.raises(LLMError):
        client.chat([{"role": "user", "content": "hi"}], purpose="unit-test")

    row = conn.execute("SELECT * FROM llm_call").fetchone()
    assert row is not None
    assert row["ok"] == 0
    assert "500" in row["error"]


def test_chat_raises_unavailable_on_503_loading_model(conn):
    """★ 모델 로딩 중 503 은 `LLMUnavailable` 이어야 한다 — 잡의 잘못이 아니다.

    실제 사고: 02:00 야간 배치가 llama-server 가 모델을 올리는 동안 잡을 집었고,
    503 이 `LLMError` 로 분류돼 **재시도 없이** 277건이 죽었다. 워커에는 이미
    `LLMUnavailable` 을 큐로 되돌리는 분기가 있었는데 거기까지 못 갔다.
    (`HISTORY/2026-08-21-a-loading-model-burned-the-whole-queue.md`)
    """
    cfg = _FakeConfig()
    body = '{"error":{"message":"Loading model","type":"unavailable_error","code":503}}'
    session = FakeSession(post_response=FakeResponse(503, None, text=body))
    client = LLMClient(cfg, conn=conn, session=session)

    with pytest.raises(LLMUnavailable):
        client.chat([{"role": "user", "content": "hi"}], purpose="unit-test")

    row = conn.execute("SELECT * FROM llm_call").fetchone()
    assert row["ok"] == 0
    assert "503" in row["error"]


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_chat_transient_statuses_are_unavailable(conn, status):
    """잠깐 뒤에 다시 물어보면 되는 코드들. 재시도 대상으로 남아 있어야 한다."""
    client = LLMClient(_FakeConfig(), conn=conn, session=FakeSession(
        post_response=FakeResponse(status, None, text="transient")))
    with pytest.raises(LLMUnavailable):
        client.chat([{"role": "user", "content": "hi"}], purpose="unit-test")


@pytest.mark.parametrize("status", [400, 401, 404, 422, 500])
def test_chat_permanent_statuses_stay_llm_error(conn, status):
    """요청 자체가 틀린 것은 재시도해도 똑같다 — `LLMUnavailable` 이면 안 된다.

    이걸 재시도로 돌리면 잘못된 잡이 큐를 영원히 맴돈다. 500 도 여기다:
    llama.cpp 가 스키마 오류(`pattern`)에 500 을 내는 경우가 있고, 그건 고쳐야 할
    버그이지 기다릴 일이 아니다.
    """
    client = LLMClient(_FakeConfig(), conn=conn, session=FakeSession(
        post_response=FakeResponse(status, None, text="permanent")))
    with pytest.raises(LLMError) as exc_info:
        client.chat([{"role": "user", "content": "hi"}], purpose="unit-test")
    assert not isinstance(exc_info.value, LLMUnavailable)


def test_chat_raises_llm_unavailable_on_connection_error_and_records(conn):
    cfg = _FakeConfig()
    session = FakeSession(post_exc=requests.exceptions.ConnectionError("connection refused"))
    client = LLMClient(cfg, conn=conn, session=session)

    with pytest.raises(LLMUnavailable):
        client.chat([{"role": "user", "content": "hi"}], purpose="unit-test")

    row = conn.execute("SELECT * FROM llm_call").fetchone()
    assert row is not None
    assert row["ok"] == 0


def test_chat_without_conn_does_not_touch_db():
    cfg = _FakeConfig()
    session = FakeSession(post_response=_ok_chat_response())
    client = LLMClient(cfg, session=session)  # conn 미지정 -> 계측 없이 정상 동작해야 함
    resp = client.chat([{"role": "user", "content": "hi"}])
    assert isinstance(resp, LLMResponse)


# ── health / model_id ────────────────────────────────────────────────────


def test_health_true_when_models_endpoint_ok():
    cfg = _FakeConfig()
    session = FakeSession(get_response=FakeResponse(200, {"data": [{"id": "qwen3-8b"}]}))
    client = LLMClient(cfg, session=session)
    assert client.health() is True


def test_health_false_when_server_unreachable():
    cfg = _FakeConfig()
    session = FakeSession(get_exc=requests.exceptions.ConnectionError("refused"))
    client = LLMClient(cfg, session=session)
    assert client.health() is False


def test_model_id_returns_actual_server_reported_id():
    cfg = _FakeConfig(llm=_FakeLLMConfig(model="qwen3-8b"))
    session = FakeSession(get_response=FakeResponse(200, {"data": [{"id": "qwen3-4b"}]}))
    client = LLMClient(cfg, session=session)
    # 실제 사고(operate/notes/agent-gateway.md §4-8): 포트는 같아도 다른 모델이 응답할 수 있다.
    # model_id() 는 서버가 실제로 보고한 id 를 그대로 돌려줘야 한다 (cfg.llm.model 이 아니라).
    assert client.model_id() == "qwen3-4b"
    assert client.model_id() != cfg.llm.model


def test_model_id_returns_none_when_unreachable():
    cfg = _FakeConfig()
    session = FakeSession(get_exc=requests.exceptions.ConnectionError("refused"))
    client = LLMClient(cfg, session=session)
    assert client.model_id() is None


# ── complete_json ────────────────────────────────────────────────────────


def test_complete_json_parses_dict():
    cfg = _FakeConfig()
    payload = {"category": "coding", "subcategory": None, "confidence": 0.9}
    session = FakeSession(post_response=_ok_chat_response(json.dumps(payload, ensure_ascii=False)))
    client = LLMClient(cfg, session=session)

    schema = json_schema_of(ActivityTag, "ActivityTag")
    result = client.complete_json("system", "user", schema, purpose="tag_test")

    assert result == payload
    # 응답에 실린 스키마 역시 pattern 없이 갔는지 확인 (회귀 방지)
    sent_schema = session.last_payload["response_format"]["json_schema"]["schema"]
    assert not _contains_pattern(sent_schema)


def test_complete_json_raises_llm_error_on_invalid_json():
    cfg = _FakeConfig()
    session = FakeSession(post_response=_ok_chat_response("이건 JSON 이 아닙니다"))
    client = LLMClient(cfg, session=session)

    schema = json_schema_of(ActivityTag, "ActivityTag")
    with pytest.raises(LLMError):
        client.complete_json("system", "user", schema, purpose="tag_test")


def test_complete_json_response_format_uses_strict_true():
    cfg = _FakeConfig()
    session = FakeSession(post_response=_ok_chat_response('{"category": "coding", "subcategory": null, "confidence": 0.5}'))
    client = LLMClient(cfg, session=session)

    schema = json_schema_of(ActivityTag, "ActivityTag")
    client.complete_json("system", "user", schema, purpose="tag_test")

    rf = session.last_payload["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
