"""LLM 구조화 출력용 Pydantic 스키마.

llama.cpp 는 `--jinja` 로 JSON 스키마를 GBNF 문법으로 변환하는데, 정규식(`pattern`)을
제대로 다루지 못해 요청 전체를 400 으로 거부한다(실측 사고 —
`docs/openclaw-agent.md §4-5`). Pydantic 이 자동으로 붙이는 `pattern` 은 특히
`Literal`/`Enum`/제약 문자열 필드에서 흔히 생기므로, 스키마를 서버에 보내기 전에
반드시 이 모듈의 `json_schema_of` 를 거쳐 `pattern` 을 제거해야 한다.
`minLength`/`maxLength` 같은 다른 제약은 GBNF 로도 문제없이 변환되므로 남긴다.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ActivityTag(BaseModel):
    """`unclassified` 에 쌓인 (app, title) 지문 하나를 LLM 이 태깅한 결과."""

    category: str = Field(description="config/rules.yaml 에 정의된 카테고리 id")
    subcategory: str | None = Field(default=None, description="세부 분류, 모르면 null")
    confidence: float = Field(description="0.0~1.0 확신도")


class DocSummary(BaseModel):
    """`doc` 한 건을 3~4줄로 요약한 결과."""

    summary: str = Field(description="3~4문장 한국어 요약")
    tags: list[str] = Field(description="핵심 키워드 태그 목록")
    relevance: float = Field(description="관심사 대비 관련도 0.0~1.0")


def _strip_pattern(node: Any) -> Any:
    """스키마 트리를 재귀 순회하며 `pattern` 키를 제거한다.

    dict/list 를 모두 파고들어야 한다 — `pattern` 은 `properties.*`, `$defs.*`,
    `items`, `anyOf`/`allOf` 안 어디에도 숨어 있을 수 있다. 원본을 바꾸지 않고
    새 구조를 반환한다.
    """
    if isinstance(node, dict):
        return {
            key: _strip_pattern(value)
            for key, value in node.items()
            if key != "pattern"
        }
    if isinstance(node, list):
        return [_strip_pattern(item) for item in node]
    return node


def json_schema_of(model: type[BaseModel], name: str) -> dict:
    """Pydantic 모델 -> `pattern` 이 제거된 JSON 스키마.

    llama-server 의 `response_format.json_schema.schema` 에 그대로 넣을 수 있는
    형태다. `name` 은 스키마 이름표일 뿐 검증에는 쓰이지 않는다(호출부가
    `response_format` 조립 시 사용).
    """
    raw = model.model_json_schema()
    cleaned = _strip_pattern(raw)
    cleaned["title"] = name
    return cleaned
