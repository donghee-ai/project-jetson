"""Slack 슬래시 명령 텍스트 파서 (계약서 v2 §5) — 순수 함수, Slack 비의존.

**Slack 을 import 하지 않는다.** `slack_bolt`/`slack_sdk` 에 대한 의존이 전혀
없어야 `tests/test_slack_commands.py` 가 네트워크·Bolt 앱 기동 없이 이 모듈만
단독으로 테스트할 수 있다.

직접 파싱기를 새로 쓰지 않는다 — 기간은 `timeutil.parse_duration`, 시각 범위는
`plan.models.parse_time_range` 를 그대로 재사용한다(계약서 지시).

`/plan` 확장 문법 토큰:
    #<과목>   -> ParsedTask.subject   예) #수학
    @<기간>   -> ParsedTask.minutes   예) @60m, @1h30m (timeutil.parse_duration 재사용)
    !<우선순위> -> ParsedTask.priority  예) !high
    HH:MM-HH:MM -> ParsedTask.start_min/end_min (plan.models.parse_time_range 재사용)
토큰은 전부 title 에서 제거된다.

번호 목록('1. 첫째\n2. 둘째' 또는 한 줄에 '1. 첫째 2. 둘째')이면 여러 건을
일괄 파싱한다. 번호 표시가 전혀 없으면 전체 텍스트를 한 건으로 본다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from lifetrainer import timeutil
from lifetrainer.plan.models import parse_time_range


@dataclass
class ParsedTask:
    """`/plan` 텍스트 한 건을 파싱한 결과. `slackio.app` 이 `plan.models.add_instance` 로 넘긴다."""

    title: str
    subject: str | None
    minutes: int | None
    priority: str
    start_min: int | None
    end_min: int | None


# ── 번호 목록 분리 ────────────────────────────────────────────────────
#
# "N. " 또는 "N) " 형태의 항목 표식만 인정한다. 공백이 아닌 문자 바로 뒤에
# 붙은 숫자(예: "RPM 1-1"의 "1", 소수 "3.5"의 "3")는 표식으로 오인하지 않도록
# 앞에 (?<!\S)(공백이거나 문자열 시작)을 요구하고, 뒤에는 반드시 공백을 요구한다.
_ITEM_MARK_RE = re.compile(r"(?<!\S)(\d+)[.)]\s+")


def _split_numbered_items(text: str) -> list[str] | None:
    """번호 목록이면 항목 텍스트 리스트를, 아니면 None 을 반환한다."""
    matches = list(_ITEM_MARK_RE.finditer(text))
    if not matches:
        return None

    items: list[str] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunk = text[start:end].strip()
        if chunk:
            items.append(chunk)
    return items


# ── 확장 문법 토큰 ────────────────────────────────────────────────────

_SUBJECT_RE = re.compile(r"#(\S+)")
_DURATION_RE = re.compile(r"@(\S+)")
_PRIORITY_RE = re.compile(r"!(\S+)")
_TIME_RANGE_TOKEN_RE = re.compile(r"\d{1,2}:\d{2}-\d{1,2}:\d{2}")


def _strip_span(text: str, start: int, end: int) -> str:
    """[start, end) 구간을 지우고 공백 하나로 남긴다 (title 재조합 시 단어가 붙지 않게)."""
    return text[:start] + " " + text[end:]


def _parse_single(text: str) -> ParsedTask:
    """토큰이 섞인 항목 텍스트 하나를 `ParsedTask` 로. 인식된 토큰은 title 에서 제거한다."""
    remaining = text
    subject: str | None = None
    minutes: int | None = None
    priority = "normal"
    start_min: int | None = None
    end_min: int | None = None

    m = _TIME_RANGE_TOKEN_RE.search(remaining)
    if m:
        try:
            start_min, end_min = parse_time_range(m.group(0))
        except ValueError:
            start_min = end_min = None
        remaining = _strip_span(remaining, m.start(), m.end())

    m = _SUBJECT_RE.search(remaining)
    if m:
        subject = m.group(1)
        remaining = _strip_span(remaining, m.start(), m.end())

    m = _DURATION_RE.search(remaining)
    if m:
        try:
            seconds = timeutil.parse_duration(m.group(1))
            minutes = int(round(seconds / 60.0))
        except ValueError:
            minutes = None
        remaining = _strip_span(remaining, m.start(), m.end())

    m = _PRIORITY_RE.search(remaining)
    if m:
        priority = m.group(1).strip().lower()
        remaining = _strip_span(remaining, m.start(), m.end())

    title = re.sub(r"\s+", " ", remaining).strip()
    return ParsedTask(
        title=title,
        subject=subject,
        minutes=minutes,
        priority=priority,
        start_min=start_min,
        end_min=end_min,
    )


def parse_plan_text(text: str) -> list[ParsedTask]:
    """`/plan` 본문을 태스크 목록으로 파싱한다.

    번호 목록('1. 첫째\n2. 둘째')이면 항목별로 나눠 각각 확장 문법을 적용한다.
    번호가 없으면 전체를 한 건으로 보고 같은 확장 문법을 적용한다. 빈 입력은 빈 리스트.
    """
    stripped = text.strip()
    if not stripped:
        return []

    items = _split_numbered_items(stripped)
    if items is None:
        return [_parse_single(stripped)]
    return [_parse_single(item) for item in items if item.strip()]


# ── 번호 목록 파서 (`/del 3,5`) ────────────────────────────────────────

_INDEX_SPLIT_RE = re.compile(r"[,\s]+")


def parse_index_list(text: str) -> list[int]:
    """'3,5' / '3 5' / '3' -> [3, 5] / [3, 5] / [3]. 빈 문자열이면 빈 리스트.

    토큰 중 정수로 바뀌지 않는 것이 있으면 ValueError.
    """
    stripped = text.strip()
    if not stripped:
        return []

    tokens = [t for t in _INDEX_SPLIT_RE.split(stripped) if t]
    try:
        return [int(t) for t in tokens]
    except ValueError as exc:
        raise ValueError(f"번호 목록을 해석할 수 없습니다: {text!r} (예: '3,5' 또는 '3 5')") from exc
