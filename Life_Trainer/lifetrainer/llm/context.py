"""대화 프롬프트에 실을 컨텍스트를 조립한다 (계획·관심사·현재 상태).

## 왜 툴이 아니라 주입인가

오늘 일정처럼 **거의 매 턴 필요한 정보는 툴로 노출하지 않고 프롬프트에 미리
넣는다.** 근거는 이 기기에서 실제로 겪은 실패 모드다 — 소형 모델은 툴을 부르지
않고 "확인했습니다"로 대답을 끝내며, **그때 에러가 나지 않는다**
(`docs/build/openclaw-agent.md §4-6`). 항상 필요한 값을 툴에 맡기면 그 실패 모드에
그대로 노출된다. 주입해 두면 구조적으로 불가능해진다.

## 3층 구조 — 프롬프트 캐시를 깨지 않기 위한 배치

llama.cpp 는 공통 접두사를 앞에서부터 재사용한다. 그래서 **변하는 주기가 짧은
것일수록 뒤에** 놓아야 한다.

    ① 시스템 (하루 단위로만 변함)  역할 + 오늘 계획 + 관심사   ← build_day_context
    ② 툴 스키마 (트리거로 주입)     필요할 때만                ← trigger.py
    ③ user 메시지 (매 턴 변함)      현재 시각·진행 중인 일     ← build_turn_context

②가 ①을 깨지 않는 것은 우리가 순서를 지켜서가 아니라 Qwen3 채팅 템플릿이
`{%- if tools %}` 블록에서 시스템 내용을 먼저 렌더링하고 `# Tools` 를 그 뒤에
붙이기 때문이다. 현재 시각을 ①에 넣으면 하루 종일 캐시가 깨진다 — ③에 넣으면
user 메시지는 어차피 매 턴 새것이라 손해가 0이다.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from lifetrainer import timeutil
from lifetrainer.plan.achieve import plans_for_day

if TYPE_CHECKING:
    from lifetrainer.config import Config
    from lifetrainer.plan.models import PlanInstance

logger = logging.getLogger(__name__)

# 프롬프트에 실을 상한. 넘으면 잘라낸다 — 시스템층 목표는 300~500 토큰이다.
MAX_PLANS = 12
MAX_INTERESTS = 10
MAX_ACTIVITY_CATEGORIES = 5

_STATUS_KO = {
    "todo": "예정",
    "doing": "진행 중",
    "done": "완료",
    "partial": "부분 완료",
    "deferred": "연기",
    "canceled": "취소",
}


def _hhmm(minute: int) -> str:
    """벽시계 분(0~1439)을 HH:MM 으로. 하루 경계와 무관한 표기용이다."""
    return f"{minute // 60:02d}:{minute % 60:02d}"


@dataclass
class DayContext:
    """하루 단위로만 변하는 대화 컨텍스트. 시스템 프롬프트에 실린다."""

    day: str
    plans: list["PlanInstance"] = field(default_factory=list)
    interests: list[tuple[str, float]] = field(default_factory=list)
    truncated_plans: int = 0

    def to_prompt(self) -> str:
        """시스템 프롬프트에 붙일 텍스트. 계획이 없으면 그 사실을 명시한다.

        제목에 "사람이 세운 의도"를 박아 둔다. 계획만 주입하고 실측을 빼면 모델이
        계획 목록을 읽고 "다 했다"고 지어낸다(실제로 겪음) — 계약서 §3 의
        `plan_instance`(의도)와 `slot`(측정)을 섞지 않는다는 원칙이 프롬프트
        층에서도 지켜져야 한다. 측정치는 `build_turn_context` 가 따로 붙인다.
        """
        lines: list[str] = [f"[오늘 {self.day} 계획 — 사람이 세운 의도]"]
        if not self.plans:
            lines.append("- (등록된 계획 없음)")
        else:
            for pi in self.plans:
                status = _STATUS_KO.get(pi.status, pi.status)
                category = pi.plan.category or "미분류"
                lines.append(
                    f"- {_hhmm(pi.plan.start_min)}~{_hhmm(pi.plan.end_min)} "
                    f"{pi.plan.title} ({category}, {status}, 달성 {pi.achievement * 100:.0f}%)"
                )
            if self.truncated_plans:
                lines.append(f"- (외 {self.truncated_plans}건 생략)")

        if self.interests:
            terms = ", ".join(term for term, _ in self.interests)
            lines.append("")
            lines.append(f"[관심사] {terms}")

        return "\n".join(lines)


def build_day_context(conn: sqlite3.Connection, cfg: "Config", day: str) -> DayContext:
    """하루 단위 고정층을 만든다.

    `plans_for_day` 는 진입 시 `materialize_day` 를 부르므로(멱등) 그날 뜨는
    템플릿이 아직 전개되지 않았어도 안전하다.
    """
    try:
        plans = plans_for_day(conn, cfg, day)
    except sqlite3.Error as exc:
        logger.warning("계획 조회 실패 (day=%s): %s", day, exc)
        plans = []

    truncated = max(0, len(plans) - MAX_PLANS)
    plans = plans[:MAX_PLANS]

    try:
        rows = conn.execute(
            "SELECT term, weight FROM interest ORDER BY weight DESC, term ASC LIMIT ?",
            (MAX_INTERESTS,),
        ).fetchall()
        interests = [(str(r[0]), float(r[1])) for r in rows]
    except sqlite3.Error as exc:
        logger.warning("관심사 조회 실패: %s", exc)
        interests = []

    return DayContext(day=day, plans=plans, interests=interests, truncated_plans=truncated)


def build_turn_context(
    conn: sqlite3.Connection, cfg: "Config", day: str, *, now: float | None = None
) -> str:
    """매 턴 변하는 것 — 현재 시각, 지금 시간대 계획, **오늘 실제 활동 요약**.

    시스템 프롬프트가 아니라 user 메시지에 붙인다(모듈 docstring §3층). 활동
    집계는 10분마다 갱신되므로 ①층에 넣으면 하루에도 몇십 번씩 캐시가 깨진다.
    user 메시지는 어차피 매 턴 새것이라 여기 붙이면 손해가 0이다.

    실측을 반드시 함께 싣는 이유: 계획만 주면 모델이 그걸 읽고 "다 했다"고
    지어낸다. 의도와 측정을 나란히 놓아야 대조가 성립한다.
    """
    ts = timeutil.now_ts() if now is None else now
    local = timeutil.from_ts(ts, cfg.tz)
    wall_min = local.hour * 60 + local.minute

    parts = [f"(현재 {local.strftime('%Y-%m-%d %H:%M')}, 논리적 하루 {day})"]

    try:
        plans = plans_for_day(conn, cfg, day)
    except sqlite3.Error as exc:
        logger.warning("현재 계획 조회 실패 (day=%s): %s", day, exc)
        plans = []

    current = [pi for pi in plans if pi.plan.start_min <= wall_min < pi.plan.end_min]
    if current:
        titles = ", ".join(pi.plan.title for pi in current)
        parts.append(f"지금 시간대 계획: {titles}")

    parts.append(f"[오늘 실제 활동 — 기계 측정] {_today_activity(conn, cfg, day)}")
    return "\n".join(parts)


def _today_activity(conn: sqlite3.Connection, cfg: "Config", day: str) -> str:
    """오늘 실측 활동을 한 줄로. 집계는 `compute_daily` 가 SQL 로 이미 해 둔 것을 쓴다."""
    from lifetrainer.report.stats import compute_daily, format_hm

    try:
        stats = compute_daily(conn, cfg, day)
    except sqlite3.Error as exc:
        logger.warning("활동 집계 실패 (day=%s): %s", day, exc)
        return "집계 실패"

    if stats.active_sec <= 0:
        return "아직 계측된 활동 없음"

    cats = ", ".join(
        f"{c.category} {format_hm(c.seconds)}" for c in stats.by_category[:MAX_ACTIVITY_CATEGORIES]
    )
    return f"총 {format_hm(stats.active_sec)} — {cats}"
