"""lifetrainer.llm.trigger 테스트.

트리거는 **재현율 우선**이다 (`trigger.py` 모듈 docstring): 과잉 주입은 0.5초
손해지만 누락은 "알겠습니다"로 끝나는 무응답 기능 파손이다. 그래서 이 테스트는
"안 걸려야 할 것"보다 "반드시 걸려야 할 것"을 훨씬 촘촘히 본다.
"""

from __future__ import annotations

import pytest

from lifetrainer.llm import tools as tools_mod
from lifetrainer.llm.trigger import select_tools


# ── 기본 동작 ────────────────────────────────────────────────────────────


def test_조회_툴은_트리거_없이_항상_실린다():
    sel = select_tools("오늘 뭐 했지?")
    assert sel.fired == []
    assert set(sel.tool_names) == set(tools_mod.base_tool_names())


def test_빈_발화도_조회_툴은_실린다():
    sel = select_tools("")
    assert sel.tool_names == tools_mod.base_tool_names()
    assert sel.fired == []


def test_게이트된_툴은_기본_집합에_없다():
    assert "add_plan" not in tools_mod.base_tool_names()
    assert "schedule_reminder" not in tools_mod.base_tool_names()


# ── 예약·알림 트리거 (재현율) ────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "내일 3시에 알려줘",
        "10분 뒤에 스트레칭하라고 알람 줘",
        "07:30 에 깨워줘",
        "이따가 얘기하자고 리마인드 걸어줘",
        "예약한 알림 뭐 있지?",
        "2시간 후에 말해줘",
        "까먹지 말고 알려주라",
        "5분 뒤 알림",
        "회의 있다고 기억해 줘",
    ],
)
def test_예약_표현은_전부_reminder_툴을_싣는다(text):
    sel = select_tools(text)
    assert "reminder" in sel.fired, f"놓침: {text!r}"
    assert "schedule_reminder" in sel.tool_names
    assert "list_reminders" in sel.tool_names
    assert "cancel_reminder" in sel.tool_names


@pytest.mark.parametrize(
    "text",
    ["오늘 뭐 했지?", "어제랑 비교해줘", "논문 읽을 거 추천해줘", "코딩 얼마나 했어?"],
)
def test_평범한_조회는_예약_툴을_싣지_않는다(text):
    assert "reminder" not in select_tools(text).fired


# ── 계획 쓰기 트리거 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "내일 오전 딥워크 계획 추가해줘",
        "내일 오전에 회의 일정 하나만 좀 넣어줘",
        "계획 좀 만들어줄래?",
        "일정 등록해줘",
        "할 일 하나 잡아줘",
    ],
)
def test_계획_추가_의도는_add_plan_을_싣는다(text):
    sel = select_tools(text)
    assert "plan_write" in sel.fired, f"놓침: {text!r}"
    assert "add_plan" in sel.tool_names


@pytest.mark.parametrize("text", ["오늘 계획 뭐였지?", "계획대로 잘 했나?", "일정 알려줘"])
def test_계획_조회는_쓰기_툴을_싣지_않는다(text):
    # 조회는 항상 실려 있는 get_plans 로 충분하다. 쓰기 툴은 실리면 안 된다.
    assert "plan_write" not in select_tools(text).fired


def test_두_트리거가_동시에_걸린다():
    sel = select_tools("3시에 회의 일정 잡아주고 30분 전에 알려줘")
    assert sorted(sel.fired) == ["plan_write", "reminder"]
    assert "add_plan" in sel.tool_names
    assert "schedule_reminder" in sel.tool_names


# ── 스키마 건전성 ────────────────────────────────────────────────────────


def test_모든_스키마에_pattern_이_없다():
    """`pattern` 이 하나라도 있으면 llama.cpp 가 요청 전체를 400 으로 거부한다."""
    from lifetrainer.llm.client import _check_no_pattern

    _check_no_pattern(tools_mod.schemas_for(list(tools_mod.REGISTRY)))


def test_selection_schemas_는_이름_순서를_따른다():
    sel = select_tools("내일 3시에 알려줘")
    names = [s["function"]["name"] for s in sel.schemas]
    assert names == sel.tool_names


def test_중복_트리거가_툴을_중복으로_싣지_않는다():
    sel = select_tools("알림 알림 예약 예약 알려줘 알려줘")
    assert len(sel.tool_names) == len(set(sel.tool_names))


# ── 웹 조회 트리거 ───────────────────────────────────────────────────────
#
# 이 게이트가 없으면 모델이 "제공할 수 없습니다" 로 끝낸다 — 실제로 겪었다
# (사용자: "지금 깃허브 트렌드 1등이 누군지 알려줘" → 툴 0회, 즉답 거절).


@pytest.mark.parametrize(
    "text",
    [
        "지금 깃허브 트렌드 1등이 누군지 알려줘",
        "해커뉴스 요즘 뭐가 올라와?",
        "https://example.com 열어봐",
        "llama.cpp 최신 릴리스 뭐야?",
        "찾아봐 줘",
        "오늘자 뉴스 뭐 있어?",
        "이 링크 좀 읽어줘",
        "trending 1위 알려줘",
        # ★ 2026-08-21 실측으로 놓친 것이 드러난 표현들. 셋 다 검색 없이 답이 나갔고,
        #   모델이 leagueoflegends.com 을 지어냈다 (docs/progress/2026-08-21.md).
        "롤체는 무슨게임이야?",
        "게임 tft는?",
        "이게 무슨 뜻이야?",
    ],
)
def test_바깥_정보를_묻는_말은_fetch_url_을_싣는다(text):
    sel = select_tools(text)
    assert "web" in sel.fired, f"놓침: {text!r}"
    assert "fetch_url" in sel.tool_names


@pytest.mark.parametrize(
    "text",
    [
        "오늘 뭐 했지?",
        "어제랑 오늘 비교해줘",
        "코딩 얼마나 했어?",
        "계획대로 잘 했나?",
        # ★ "X는?" 되묻기 패턴을 넓힌 뒤에도 개인 기록은 계속 빠져야 한다.
        "내 계획은?",
        "어제 내 공부시간은?",
    ],
)
def test_내_기록_질문은_웹_툴을_안_싣는다(text):
    assert "web" not in select_tools(text).fired


def test_웹_툴은_기본_집합에_없다():
    assert "fetch_url" not in tools_mod.base_tool_names()


def test_알려줘가_붙으면_예약_트리거도_같이_켜진다():
    """의도된 오탐이다 — 안 쓰이면 200토큰 손해로 끝나고, 누락은 기능 파손이다."""
    sel = select_tools("지금 깃허브 트렌드 1등이 누군지 알려줘")
    assert sorted(sel.fired) == ["reminder", "web"]
