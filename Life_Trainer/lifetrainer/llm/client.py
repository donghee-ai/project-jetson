"""로컬 llama-server(OpenAI 호환) 클라이언트.

이 프로젝트가 실제로 밟은 지뢰 세 개를 여기서 막는다(`docs/build/openclaw-agent.md §4`,
`docs/contracts.md §0/§8`):

1. **JSON 스키마에 `pattern` 이 있으면 요청 전체가 400.** llama.cpp 의 GBNF 변환기가
   정규식(앵커를 붙여도)을 못 다룬다. `chat()` 은 스키마 트리를 재귀 순회해
   `pattern` 이 있으면 서버에 보내기 전에 `ValueError` 로 즉시 막는다.
2. **thinking 을 끄지 않으면 JSON 파싱이 깨진다.** JSON 출력 요청에서는
   `cfg.llm.enable_thinking` 값과 무관하게 `chat_template_kwargs.enable_thinking` 을
   무조건 false 로 보낸다. 방어적으로 `<think>...</think>` 가 섞여 나와도 제거한다.
3. **컨텍스트 깊이가 곧 비용이다.** 깊이 3K 에서 10.9 tok/s, 32K 에서 3.96 tok/s (-70%).
   `cfg.llm.max_input_tokens` 를 넘는 입력은 잘라내고 경고 로그를 남긴다.
   한국어는 같은 내용에 토큰이 55% 더 들기 때문에 추정치를 넉넉히 잡는다.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

import requests

from lifetrainer.timeutil import now_ts

if TYPE_CHECKING:
    from lifetrainer.config import Config

logger = logging.getLogger(__name__)

# 한글(완성형 + 자모) 문자 판정용. 한국어는 문자당 약 0.6 토큰, 그 외(주로 영문)는
# 약 0.15 토큰으로 넉넉하게 잡는다 (research/performance.md §6 실측: 한국어 0.600, 영어 0.145).
_HANGUL_RE = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_STRAY_RE = re.compile(r"</?think>")


class LLMError(Exception):
    """LLM 호출 실패 (서버는 응답했으나 요청/응답이 잘못된 경우 포함)."""


class LLMUnavailable(LLMError):
    """LLM 서버에 연결할 수 없음. 호출부가 잡아서 잡을 재시도로 돌릴 수 있어야 한다."""


@dataclass
class ToolCall:
    """모델이 요청한 툴 호출 하나.

    `arguments` 는 파싱된 인자다. 소형 모델은 인자 JSON 을 깨뜨리는 일이 있으므로
    파싱에 실패해도 예외를 내지 않고 `arguments={}` + `parse_error` 로 넘긴다 —
    호출부가 "인자를 못 읽었다"고 되물을 수 있어야 하기 때문이다.
    """

    id: str
    name: str
    arguments: dict
    arguments_raw: str = ""
    parse_error: str | None = None


@dataclass
class LLMResponse:
    text: str
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int
    raw: dict
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None


def estimate_tokens(text: str) -> float:
    """대략적인 토큰 수 추정. 한국어는 문자당 0.6, 그 외는 0.15 로 넉넉하게 잡는다."""
    if not text:
        return 0.0
    hangul = len(_HANGUL_RE.findall(text))
    other = len(text) - hangul
    return hangul * 0.6 + other * 0.15


def _truncate_text(text: str, max_tokens: float) -> str:
    """`text` 를 추정 토큰 수가 `max_tokens` 이하가 되도록 앞부분만 남기고 자른다."""
    if max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def _message_tokens(m: dict) -> float:
    """메시지 하나의 추정 토큰. 툴 호출 메시지는 `content` 가 None 이고 인자가 따로 온다."""
    total = estimate_tokens(str(m.get("content") or ""))
    calls = m.get("tool_calls")
    if calls:
        total += estimate_tokens(json.dumps(calls, ensure_ascii=False))
    return total


def _truncate_messages(
    messages: list[dict], max_input_tokens: int, *, reserved_tokens: float = 0.0
) -> tuple[list[dict], bool]:
    """메시지 목록 전체의 추정 토큰 합이 예산을 넘지 않도록 자른다.

    앞쪽 메시지(시스템 프롬프트 등)를 우선 보존하고, 예산을 넘기는 지점부터
    잘라낸다. 잘림이 발생했는지 여부를 함께 반환한다 (호출부가 경고 로그를 남기도록).

    `reserved_tokens` 는 메시지 밖에서 프롬프트에 실리는 몫이다. 툴 스키마는
    `payload["tools"]` 로 따로 가지만 채팅 템플릿이 시스템 블록 뒤에 렌더링하므로
    (Qwen3 템플릿 `{%- if tools %}` 분기) 컨텍스트를 똑같이 먹는다. 여기서 빼두지
    않으면 예산을 조용히 초과한다.
    """
    budget = max_input_tokens - reserved_tokens
    total = sum(_message_tokens(m) for m in messages)
    if total <= budget:
        return messages, False

    result: list[dict] = []
    used = 0.0
    truncated = False
    for m in messages:
        t = _message_tokens(m)
        if used + t <= budget:
            result.append(m)
            used += t
            continue
        remaining = budget - used
        truncated = True
        # 툴 호출·툴 결과 메시지는 잘라내면 짝이 깨져 서버가 400 을 낸다. 통째로 버린다.
        if remaining > 0 and not m.get("tool_calls") and m.get("role") != "tool":
            new_content = _truncate_text(str(m.get("content") or ""), remaining)
            result.append({**m, "content": new_content})
            used += estimate_tokens(new_content)
        # 예산이 이미 바닥났으면 이 메시지 이후는 통째로 버린다.
        break
    return _drop_dangling_tool_calls(result), truncated


def _drop_dangling_tool_calls(messages: list[dict]) -> list[dict]:
    """짝이 맞지 않는 `tool_calls` / `tool` 메시지를 걷어낸다.

    잘라내기가 툴 결과만 떨어뜨리면 어시스턴트의 `tool_calls` 가 응답 없이 남는다.
    그 상태로 보내면 서버가 요청 전체를 400 으로 거부한다 — 컨텍스트를 아끼려다
    턴을 통째로 잃는 셈이다. 짝이 없는 쪽을 버려서 항상 유효한 대화만 나가게 한다.
    """
    responded = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    result: list[dict] = []
    kept_call_ids: set = set()

    for m in messages:
        calls = m.get("tool_calls")
        if calls:
            ids = {c.get("id") for c in calls if isinstance(c, dict)}
            if not ids <= responded:  # 결과가 하나라도 없으면 이 턴은 통째로 버린다
                continue
            kept_call_ids |= ids
        elif m.get("role") == "tool" and m.get("tool_call_id") not in kept_call_ids:
            continue  # 요청이 사라진 결과도 남겨두면 안 된다
        result.append(m)
    return result


def _strip_think(text: str) -> str:
    """응답에 섞인 `<think>...</think>` 사고 텍스트를 제거한다 (방어적 조치)."""
    cleaned = _THINK_BLOCK_RE.sub("", text)
    cleaned = _THINK_STRAY_RE.sub("", cleaned)
    return cleaned.strip()


def _check_no_pattern(node: Any, *, _path: str = "$") -> None:
    """JSON 스키마 트리를 재귀 순회해 `pattern` 키가 있으면 즉시 ValueError.

    llama.cpp 는 `--jinja` 로 구조화 출력을 만들 때 스키마를 GBNF 문법으로
    변환하는데, 정규식을 다루지 못해 앵커 유무와 무관하게 요청 전체를 400 으로
    거부한다 (실측: `docs/build/openclaw-agent.md §4-5`). `properties`/`$defs`/`items`/
    `anyOf` 등 어디에 숨어 있어도 잡아낸다.
    """
    if isinstance(node, dict):
        if "pattern" in node:
            raise ValueError(
                f"JSON 스키마에 'pattern' 키워드가 있습니다 ({_path}.pattern="
                f"{node['pattern']!r}). llama.cpp 가 요청 전체를 400 으로 거부합니다 — "
                "정규식 제약을 제거하고 minLength 등 대체 제약을 쓰세요."
            )
        for key, value in node.items():
            _check_no_pattern(value, _path=f"{_path}.{key}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _check_no_pattern(item, _path=f"{_path}[{i}]")


def _parse_tool_calls(message: dict) -> list[ToolCall]:
    """응답 메시지에서 `tool_calls` 를 꺼내 파싱한다.

    llama.cpp 는 `function.arguments` 를 **JSON 문자열**로 준다. 다만 소형 모델이
    깨진 JSON 을 뱉는 일이 있어 파싱 실패를 예외로 올리지 않는다 — 툴 하나가
    깨졌다고 응답 전체를 버리면 "왜 대답이 없지"가 되기 때문이다. 실패한 호출은
    `parse_error` 를 달고 넘어가고, 호출부가 사용자에게 되묻는다.
    """
    raw_calls = message.get("tool_calls") or []
    if not isinstance(raw_calls, list):
        return []

    calls: list[ToolCall] = []
    for i, item in enumerate(raw_calls):
        if not isinstance(item, dict):
            continue
        fn = item.get("function") or {}
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        raw_args = fn.get("arguments")
        args: dict = {}
        parse_error: str | None = None
        if isinstance(raw_args, dict):  # 스펙 밖이지만 실제로 이렇게 오는 구현이 있다
            args = raw_args
            raw_args = json.dumps(raw_args, ensure_ascii=False)
        elif raw_args in (None, ""):
            raw_args = ""
        else:
            raw_args = str(raw_args)
            try:
                parsed = json.loads(raw_args)
                if isinstance(parsed, dict):
                    args = parsed
                else:
                    parse_error = f"인자가 객체가 아닙니다: {type(parsed).__name__}"
            except json.JSONDecodeError as exc:
                parse_error = f"인자 JSON 파싱 실패: {exc}"
                logger.warning("툴 %r 의 인자 파싱 실패: %s (원문 %r)", name, exc, raw_args[:200])

        calls.append(
            ToolCall(
                id=str(item.get("id") or f"call_{i}"),
                name=name,
                arguments=args,
                arguments_raw=raw_args,
                parse_error=parse_error,
            )
        )
    return calls


class LLMClient:
    """OpenAI 호환 `POST {base_url}/chat/completions` 를 치는 얇은 클라이언트.

    `conn` 을 주면 성공·실패와 무관하게 모든 호출을 `llm_call` 테이블에 계측 기록한다.
    `session` 은 테스트에서 실제 네트워크 대신 가짜 세션을 주입하기 위한 것이다.
    """

    def __init__(
        self,
        cfg: "Config",
        *,
        conn: sqlite3.Connection | None = None,
        session: "requests.Session | None" = None,
    ) -> None:
        self._cfg = cfg
        self._conn = conn
        self._base_url = cfg.llm.base_url.rstrip("/")
        self._timeout = cfg.llm.timeout_sec
        self._session = session if session is not None else requests.Session()

    def _root_url(self) -> str:
        """`.../v1` 접미사를 뗀 서버 루트. `/health` 는 `/v1` 아래에 없다."""
        if self._base_url.endswith("/v1"):
            return self._base_url[: -len("/v1")]
        return self._base_url

    def health(self) -> bool:
        """서버가 살아 있는지 확인한다. 예외를 삼키고 bool 로만 반환한다."""
        try:
            resp = self._session.get(f"{self._base_url}/models", timeout=self._timeout)
            if resp.status_code < 400:
                return True
        except requests.RequestException:
            pass
        try:
            resp = self._session.get(f"{self._root_url()}/health", timeout=self._timeout)
            return resp.status_code < 400
        except requests.RequestException:
            return False

    def model_id(self) -> str | None:
        """`/v1/models` 가 보고하는 실제 모델 id.

        포트가 같아도 다른 모델이 떠 있을 수 있다(실제 사고 —
        `docs/build/openclaw-agent.md §4-8`). 호출부는 헬스체크 통과만 믿지 말고
        이 값을 `cfg.llm.model` 과 비교하는 것이 안전하다. 조회 실패 시 None.
        """
        try:
            resp = self._session.get(f"{self._base_url}/models", timeout=self._timeout)
            if resp.status_code >= 400:
                return None
            data = resp.json()
            items = data.get("data") or []
            if not items:
                return None
            model_id = items[0].get("id")
            return str(model_id) if model_id is not None else None
        except (requests.RequestException, ValueError):
            return None

    def _record_call(
        self,
        purpose: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        latency_ms: int,
        ok: bool,
        error: str | None,
    ) -> None:
        """`llm_call` 에 호출 결과를 계측 기록한다. `conn` 이 없으면 아무 것도 하지 않는다."""
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT INTO llm_call"
                "(job_id, purpose, model, prompt_tokens, completion_tokens, latency_ms, ok, error, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    None,
                    purpose,
                    self._cfg.llm.model,
                    prompt_tokens,
                    completion_tokens,
                    latency_ms,
                    1 if ok else 0,
                    error,
                    now_ts(),
                ),
            )
            self._conn.commit()
        except sqlite3.Error as exc:  # 계측 실패로 본 호출 결과까지 잃으면 안 된다.
            logger.warning("llm_call 계측 기록 실패: %s", exc)

    def chat(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 512,
        temperature: float = 0.7,
        json_schema: dict | None = None,
        purpose: str = "",
        stop: list[str] | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
    ) -> LLMResponse:
        """`chat/completions` 를 호출한다. 서버가 죽어 있으면 `LLMUnavailable`."""
        if json_schema is not None:
            _check_no_pattern(json_schema)
        if tools:
            # 툴 스키마도 GBNF 로 변환된다. `pattern` 이 하나라도 있으면 요청 전체가
            # 400 이 된다 — OpenClaw 의 cron 툴이 정확히 이걸로 죽었다 (§4-5).
            _check_no_pattern(tools)

        tool_tokens = estimate_tokens(json.dumps(tools, ensure_ascii=False)) if tools else 0.0
        truncated_messages, was_truncated = _truncate_messages(
            messages, self._cfg.llm.max_input_tokens, reserved_tokens=tool_tokens
        )
        if was_truncated:
            logger.warning(
                "LLM 입력이 max_input_tokens(%d, 툴 %d 토큰 포함)를 넘어 잘렸습니다 (purpose=%r)",
                self._cfg.llm.max_input_tokens,
                int(tool_tokens),
                purpose,
            )

        # JSON 출력·툴 콜링에서는 cfg 값과 무관하게 무조건 thinking 을 끈다 — 사고
        # 텍스트가 섞이면 파싱이 깨진다 (계약서 §0).
        enable_thinking = (
            False if (json_schema is not None or tools) else self._cfg.llm.enable_thinking
        )

        payload: dict[str, Any] = {
            "model": self._cfg.llm.model,
            "messages": truncated_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "chat_template_kwargs": {"enable_thinking": enable_thinking},
        }
        if stop:
            payload["stop"] = stop
        if tools:
            payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.get("title") or "response",
                    "schema": json_schema,
                    "strict": True,
                },
            }

        start = time.monotonic()
        try:
            resp = self._session.post(
                f"{self._base_url}/chat/completions", json=payload, timeout=self._timeout
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            error_text = f"LLM 서버에 연결할 수 없습니다 ({self._base_url}): {exc}"
            self._record_call(purpose, None, None, latency_ms, False, error_text)
            raise LLMUnavailable(error_text) from exc
        except requests.RequestException as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            error_text = f"LLM 요청 실패: {exc}"
            self._record_call(purpose, None, None, latency_ms, False, error_text)
            raise LLMError(error_text) from exc

        latency_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code >= 400:
            error_text = f"LLM 서버가 {resp.status_code} 를 반환했습니다: {resp.text[:500]}"
            self._record_call(purpose, None, None, latency_ms, False, error_text)
            raise LLMError(error_text)

        try:
            raw = resp.json()
        except ValueError as exc:
            error_text = f"LLM 응답 JSON 파싱 실패: {exc}"
            self._record_call(purpose, None, None, latency_ms, False, error_text)
            raise LLMError(error_text) from exc

        try:
            choice = raw["choices"][0]
            message = choice["message"]
            text = message.get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            error_text = f"LLM 응답 형식이 예상과 다릅니다: {exc}"
            self._record_call(purpose, None, None, latency_ms, False, error_text)
            raise LLMError(error_text) from exc

        text = _strip_think(text)
        tool_calls = _parse_tool_calls(message)

        usage = raw.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")

        self._record_call(purpose, prompt_tokens, completion_tokens, latency_ms, True, None)

        return LLMResponse(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            raw=raw,
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason"),
        )

    def complete_json(
        self, system: str, user: str, schema: dict, *, purpose: str, max_tokens: int = 512
    ) -> dict:
        """시스템/유저 메시지 한 쌍으로 구조화 JSON 출력을 받아 dict 로 파싱해 반환한다."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        response = self.chat(
            messages,
            max_tokens=max_tokens,
            temperature=0.2,
            json_schema=schema,
            purpose=purpose,
        )
        text = _strip_think(response.text)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(
                f"LLM JSON 응답 파싱 실패 (purpose={purpose!r}): {exc}\n원문: {text[:500]!r}"
            ) from exc
