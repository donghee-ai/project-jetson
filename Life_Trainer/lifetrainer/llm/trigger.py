"""트리거 게이트 — 발화를 보고 이번 턴에 실을 툴을 규칙으로 고른다.

## 왜 규칙인가

같은 구조를 음성 쪽에서 이미 실측했다 (`docs/voice-agent-plan.md §2-5`):
핫패스 규칙 라우터가 15/15 정확도에 평균 0.193ms 로, 같은 판단을 LLM 에 시켰을 때
(1,270ms)보다 약 6,500배 빨랐다. 툴을 고르자고 LLM 을 한 번 더 부르는 것은
"지연을 줄이려고 지연을 추가하는" 짓이다.

## 왜 전부 싣지 않는가

툴 스키마는 매 턴 프롬프트에 실린다. 프롬프트 처리 실측이 295 tok/s
(`docs/openclaw-agent.md §3`)이므로 스키마 토큰은 그대로 지연이 된다.
OpenClaw 의 `cron` 툴 하나가 3,912 토큰(툴 전체의 44%)이라 매 턴 13초를 더했다.
필요할 때만 실으면 그 13초가 0 이 된다.

## 왜 관대하게 잡는가 — 비용이 비대칭이다

    과잉 주입(FP)   안 쓰이는 스키마가 실려 0.5초 손해. 그걸로 끝.
    누락(FN)        "내일 3시에 알려줘" 에 툴이 없으면 모델이 "알겠습니다" 로
                    대답을 끝낸다. 그리고 **에러가 나지 않는다**
                    (`docs/openclaw-agent.md §4-6` 의 실패 모드).

0.5초와 기능 파손을 맞바꾸는 것이므로 **재현율을 우선한다.** 애매하면 싣는다.

## 캐시를 깨지 않는 이유

툴을 갈아끼워도 시스템 프롬프트 앞부분은 캐시가 유지된다. Qwen3 채팅 템플릿의
`{%- if tools %}` 블록이 시스템 내용을 먼저 렌더링하고 `# Tools` 를 그 뒤에 붙이기
때문이다 (`llm/context.py` 모듈 docstring 참고).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lifetrainer.llm import tools as tools_mod

# ── 예약·알림 ──────────────────────────────────────────────────────────
# 1) 명시적인 알림 낱말, 2) 시간 표현. 둘 중 하나만 걸려도 싣는다.
_REMINDER_PATTERNS = [
    re.compile(r"알림|알려\s*줘|알려\s*주|알려\s*달|알람|깨워|리마인드|remind", re.I),
    re.compile(r"예약|스케줄|잊지|까먹지|기억해\s*줘"),
    re.compile(r"\d+\s*(분|시간|초|일)\s*(뒤|후|있다가|지나)"),
    re.compile(r"\d+\s*시\s*\d*\s*분?\s*에"),
    re.compile(r"\d{1,2}:\d{2}\s*에"),
    re.compile(r"(이따|나중에|있다가)\s*(가|서)?\s*(알|말|얘기|연락)"),
]

# ── 계획 추가 ──────────────────────────────────────────────────────────
# 쓰기 툴이라 알림보다 조금 더 좁게 잡는다. 대상 낱말 + 동작 낱말이 함께 있어야 한다.
_PLAN_NOUN = re.compile(r"계획|일정|스케줄|플랜|플래너|할\s*일|투두|todo", re.I)
_PLAN_VERB = re.compile(r"추가|넣어|넣자|잡아|잡자|만들|등록|생성|올려|세워|짜")
_PLAN_PATTERNS = [
    re.compile(r"(계획|일정|스케줄|플랜).{0,12}(추가|등록|생성|만들|잡|넣)"),
    re.compile(r"(추가|등록|생성|만들|잡아|넣어).{0,12}(계획|일정|스케줄|플랜)"),
]


# ── 웹 조회 ────────────────────────────────────────────────────────────
# "지금 이 순간의 바깥 정보" 를 묻는 표현. 이것도 재현율 우선이다 — 안 실리면
# 모델이 "제공할 수 없습니다" 로 끝내버린다(실제로 겪은 실패다).
_WEB_PATTERNS = [
    re.compile(r"https?://"),
    re.compile(r"트렌딩|트렌드|trending", re.I),
    re.compile(r"뉴스|기사|헤드라인|속보"),
    re.compile(r"(최신|요즘|오늘자|지금).{0,8}(뭐|무엇|어떤|순위|버전|소식|이슈)"),
    re.compile(r"(찾아|검색해|알아봐|확인해)\s*(줘|봐|주라|보라)"),
    re.compile(r"(사이트|웹페이지|페이지|링크|주소).{0,6}(열|읽|봐|확인|가져|보내)"),
    re.compile(r"릴리스|릴리즈|release\s*note", re.I),
    # ★ 아래는 실제로 놓쳐서 환각이 나온 표현들이다. "한국 유튜브 기준 실시간 급상승
    # 1위는?" 이 위 패턴 어디에도 안 걸려 툴이 안 실렸고, 모델이 조회수까지 지어냈다.
    re.compile(r"급상승|인기\s*급|실시간\s*(순위|검색|차트)|차트"),
    re.compile(r"\d+\s*(위|등)\b|[1-9]위|[1-9]등"),
    re.compile(r"순위|랭킹|ranking", re.I),
    re.compile(r"유튜브|youtube|구글|google|네이버|깃허브|github|해커뉴스|hacker\s*news", re.I),
    # ★ "시립도서관 이번 주 휴관일에 뭐라고 적혀있어?" 가 위 패턴 어디에도 안 걸려
    # search_docs 로 새고, 엉뚱한 NVIDIA 포럼 글을 물고 왔다. 특정 사이트의 게시물을
    # 묻는 말은 거의 항상 바깥 조회다.
    re.compile(r"홈페이지|웹사이트|공식\s*사이트|누리집"),
    re.compile(r"공지|게시판|소식|뉴스룸|보도자료|공고"),
    re.compile(r"(뭐라고|무엇이라|뭐가|무슨\s*내용)\s*(적|써|올라|나와)"),
]


@dataclass
class TriggerGroup:
    """한 덩어리로 함께 실리는 툴들과, 그것을 켜는 규칙."""

    name: str
    tools: list[str]
    patterns: list[re.Pattern] = field(default_factory=list)

    def matches(self, text: str) -> bool:
        return any(p.search(text) for p in self.patterns)


GROUPS: list[TriggerGroup] = [
    TriggerGroup(
        name="reminder",
        tools=["schedule_reminder", "list_reminders", "cancel_reminder"],
        patterns=_REMINDER_PATTERNS,
    ),
    TriggerGroup(name="plan_write", tools=["add_plan"], patterns=_PLAN_PATTERNS),
    TriggerGroup(name="web", tools=["fetch_url"], patterns=_WEB_PATTERNS),
]


@dataclass
class Selection:
    """이번 턴에 실을 툴과, 왜 실렸는지."""

    tool_names: list[str]
    fired: list[str] = field(default_factory=list)

    @property
    def schemas(self) -> list[dict]:
        return tools_mod.schemas_for(self.tool_names)


def select_tools(text: str) -> Selection:
    """발화 하나를 보고 이번 턴에 실을 툴 이름 목록을 고른다.

    조회 툴은 항상 실린다 — 활동·계획·문서 질의는 대화의 기본값이라 게이트를
    두면 누락 비용만 커진다. 게이트는 쓰기·예약처럼 무겁거나 되돌리기 어려운
    쪽에만 건다.
    """
    names = list(tools_mod.base_tool_names())
    fired: list[str] = []
    if not text:
        return Selection(tool_names=names)

    for group in GROUPS:
        if group.matches(text):
            fired.append(group.name)
            for tool_name in group.tools:
                if tool_name not in names:
                    names.append(tool_name)

    # `_PLAN_PATTERNS` 는 붙어 있는 표현만 잡는다. 낱말이 멀리 떨어진
    # "내일 오전에 회의 일정 하나만 좀 넣어줘" 같은 문장을 놓치지 않도록 한 번 더 본다.
    if "plan_write" not in fired and _PLAN_NOUN.search(text) and _PLAN_VERB.search(text):
        fired.append("plan_write")
        if "add_plan" not in names:
            names.append("add_plan")

    return Selection(tool_names=names, fired=fired)
