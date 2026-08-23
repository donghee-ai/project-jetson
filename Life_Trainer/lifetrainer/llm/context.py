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
import re
import sqlite3
from datetime import date, timedelta
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


# 지금 시간대 계획을 **실을 만한** 말인가.
#
# ★ 왜 가리나: 잡담에도 계획이 실려 있으면 8B 가 그걸 답으로 삼는다. "심심해" 에
#   "지금은 문서 정리 시간입니다" 를 읊었다(2026-08-22 실측). 프롬프트로 "먼저 꺼내지
#   마라"를 넣어도 줄어들 뿐 안 없어졌다 — 프롬프트로 안 되는 것은 구조로 막는다.
#
# ★ 못 걸러도 안전한 이유: 조회 툴(`get_plans`)은 트리거와 무관하게 **항상** 실린다.
#   주입은 툴 한 바퀴를 아끼는 최적화지 유일한 경로가 아니다. 그래서 재현율보다
#   "안 물었으면 안 싣는다"를 우선한다.
# 사용자가 **오늘이 아닌 날**을 가리켰는지. 가리켰으면 그 날 계획을 함께 싣는다.
#
# ★ 실측 사고 (2026-08-23): "내일 계획 뭐 있어?" 에 8B 가 `get_plans` 를 안 부르고
#   시스템 프롬프트의 `[오늘 … 계획 — 사람이 세운 의도]` 블록을 **그대로 베껴** 답했다.
#   내일 계획이 DB 에 4건 있는데 오늘 것을 읊었고, 프롬프트 라벨까지 같이 나갔다.
#   "내일 일정 알려줘" 는 툴을 부른다 — 질문의 "계획" 이 주입 라벨의 "계획" 과 겹치는
#   순간에만 깨진다.
#
#   고치는 방향은 툴을 강제하는 것이 아니라 **물어본 날의 계획을 눈앞에 놓는 것**이다.
#   이 저장소의 원칙 그대로 — "항상 필요한 정보는 툴이 아니라 주입". 그러면 베껴도
#   맞는 것을 베낀다. 턴 단위 배경이라 하루 단위 프롬프트 캐시를 안 깬다.
_OTHER_DAY_OFFSETS = {"그저께": -2, "그제": -2, "어제": -1, "내일": 1, "모레": 2, "글피": 3}
_OTHER_DAY_RE = re.compile(
    r"(그저께|그제|어제|내일|모레|글피)|(\d{4}-\d{2}-\d{2})|(\d{1,2})\s*월\s*(\d{1,2})\s*일"
)


def referenced_day(today: str, user_text: str) -> str | None:
    """사용자가 가리킨 **오늘이 아닌** 날. 없으면 None.

    "오늘" 은 일부러 안 잡는다 — 이미 시스템 프롬프트에 실려 있다.
    """
    m = _OTHER_DAY_RE.search(user_text or "")
    if not m:
        return None
    if m.group(1):
        day = _shift(today, _OTHER_DAY_OFFSETS[m.group(1)])
    elif m.group(2):
        day = m.group(2)
    else:
        y = int(today.split("-")[0])
        try:
            day = date(y, int(m.group(3)), int(m.group(4))).isoformat()
        except ValueError:
            return None
    return day if day != today else None


def _shift(day: str, delta: int) -> str:
    y, mo, d = (int(x) for x in day.split("-"))
    return (date(y, mo, d) + timedelta(days=delta)).isoformat()


_PLAN_CONTEXT = re.compile(
    r"(계획|일정|스케줄|플래너|할\s*일|해야|남았|남은|진행|달성|목표|집중|"
    r"지금|이따|오늘|내일|어제|뭐\s*하|뭐\s*해|시간)"
)


def build_turn_context(
    conn: sqlite3.Connection,
    cfg: "Config",
    day: str,
    *,
    now: float | None = None,
    user_text: str = "",
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
    # 계획을 물어보지 않은 턴에는 싣지 않는다 (위 `_PLAN_CONTEXT` 주석 참조).
    if user_text and not _PLAN_CONTEXT.search(user_text):
        current = []
    if current:
        # ★ 남은 시간을 **코드가 계산해서 적는다.** 계획 시간대와 현재 시각만 주고
        #   빼기를 시키면 8B 가 틀린다 — 실측으로 세 번 연속 틀렸고 시간이 갈수록
        #   남은 시간이 늘어나기까지 했다 (20:19 "20분", 20:20 "40분", 21:04 "46분").
        #   계약서의 "모델에게 산술을 시키지 않는다" 가 이 경우다.
        titles = ", ".join(
            f"{pi.plan.title} ({_hhmm(pi.plan.start_min)}~{_hhmm(pi.plan.end_min)}, "
            f"{pi.plan.end_min - wall_min}분 남음)"
            for pi in current
        )
        parts.append(f"지금 시간대 계획: {titles}")

    parts.append(f"[오늘 실제 활동 — 기계 측정] {_today_activity(conn, cfg, day)}")

    # ★ 사용자가 다른 날을 물었으면 **그 날 계획을 여기 싣는다.** 없으면 8B 가
    #   오늘 블록을 베낀다 (`_OTHER_DAY_RE` 주석의 실측 사고).
    other = referenced_day(day, user_text)
    if other:
        try:
            rows = plans_for_day(conn, cfg, other)
        except sqlite3.Error as exc:
            logger.warning("다른 날 계획 조회 실패 (day=%s): %s", other, exc)
            rows = []
        if rows:
            items = "; ".join(
                f"{_hhmm(pi.plan.start_min)}~{_hhmm(pi.plan.end_min)} {pi.plan.title}"
                f" ({_STATUS_KO.get(pi.status, pi.status)})"
                for pi in rows[:MAX_PLANS]
            )
            parts.append(f"[{other} 계획 — 사람이 세운 의도] {items}")
        else:
            parts.append(f"[{other} 계획] 등록된 계획 없음")

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
