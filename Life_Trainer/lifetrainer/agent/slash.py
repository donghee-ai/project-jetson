"""에이전트가 "/" 명령을 직접 실행하는 통로 — Slack 없이.

## 왜 이게 에이전트의 주 경로인가

`HANDOFF.md §1` 의 입력 경로 3개가 그대로 여기에 적용된다.

    /lt /plan /del …   파싱 → SQL          밀리초    ★ 주 경로
    자연어 + 툴         3층 프롬프트 → SQL   2~9초     폴백

에이전트에게도 같은 순서를 강제한다. 슬래시로 표현할 수 있는 것을 자연어로 돌리면
**모델이 "했습니다"로 끝내는 실패 모드**(`openclaw-agent.md §4-6`)가 되살아난다.
슬래시는 코드가 실행하므로 그 실패가 구조적으로 불가능하다.

## Slack 을 어떻게 떼어냈나

`slackio/app.py` 의 `_dispatch_*` 는 처음부터 `(cfg, …, respond)` 형태의 순수
함수로 분리돼 있었다 (그 모듈 docstring 의 설계 원칙). `respond` 자리에 수집기를
넣으면 Slack 없이 같은 코드가 돈다. **명령 파싱을 두 번 쓰지 않는다** — 같은 값을
여러 곳에서 각자 계산하는 것이 이 저장소의 반복된 실패 2번이다.

★ **발송은 원천 차단한다.** `cfg.slack.bot_token` 을 비운 사본으로 부른다.
`SlackNotifier.enabled` 가 False 가 되어 `post`/`upload_png` 가 전부 무발송으로
빠지고, `_dispatch_*` 는 준비된 폴백(`respond`)으로 결과를 넘긴다. 프롬프트로
"보내지 마"라고 부탁하는 대신 **경로를 없앤다** — `CLAUDE.md` 의 "Slack 발송은
명시적 지시가 있을 때만" 규칙을 코드로 지키는 방법이다.

## PNG 를 안 그리는 이유

`/view`·`/week` 은 Slack 에서는 PNG 를 올린다. 에이전트에게는 안 그린다.

1. 에이전트가 돌려주는 것은 **텍스트**다. 그림을 줘도 모델이 못 읽는다.
2. `report.planner` 를 import 하는 순간 상주가 **+44MB** 다 (실측: 12.4 → 56.4MB).
   상주 300MB 예산에서 못 읽을 그림에 쓸 자리가 아니다.

그래서 이 둘만 여기서 텍스트로 직접 조립한다. **숫자는 다시 계산하지 않는다** —
`plan.achieve` · `report.stats` 가 이미 센 것을 문장으로 옮기기만 한다.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from typing import Any, TYPE_CHECKING

from lifetrainer import db, timeutil

if TYPE_CHECKING:
    import sqlite3

    from lifetrainer.config import Config

logger = logging.getLogger(__name__)

# 에이전트에게 열어 주는 명령. Slack 앱의 11개 중 `/openclaw` 는 게이트웨이 자신의
# 것이라 빠지고, 나머지 10개가 그대로 온다.
COMMANDS: dict[str, str] = {
    "/lt": "ping|today|yesterday|week|status — 요약·상태 조회",
    "/log": "<카테고리> <기간> [메모] — 활동을 손으로 기록 (예: /log 운동 60m 헬스장)",
    "/plan": "<텍스트> — 계획 추가. #과목 @기간 !우선순위 HH:MM-HH:MM 문법",
    "/del": "[번호,번호] — 계획 삭제. 인자가 없으면 오늘 목록을 번호와 함께 보여준다",
    "/view": "[어제|YYYY-MM-DD] — 그날의 계획과 달성률",
    "/done": "<번호> — 완료 처리",
    "/doing": "<번호> — 진행 중으로 표시",
    "/defer": "<번호> — 내일로 연기",
    "/memo": "<텍스트> — 그날의 메모 저장",
    "/week": "주간 비교",
}

# 델리게이트 표 — 값은 `slackio.app` 의 함수 이름과 인자 모양이다.
# `/view`·`/week` 은 위 docstring 대로 여기서 직접 만든다.
_DELEGATED = {
    "/lt": "sub",
    "/log": "command",
    "/plan": "text",
    "/del": "command",
    "/memo": "text",
    "/done": "status",
    "/doing": "status",
    "/defer": "status",
}

MAX_OUTPUT_CHARS = 2000  # 결과가 다음 턴의 프롬프트가 된다. 깊이가 곧 비용이다.


class SlashError(Exception):
    """명령을 실행할 수 없다. 메시지가 그대로 모델에게 간다."""


# ── 수집기 ────────────────────────────────────────────────────────────


class _Collector:
    """`respond` 자리에 들어가 Slack 대신 문자열을 모은다.

    ★ **한 번 닫히면 더 안 받는다.** `_dispatch_del` 은 15초 뒤에 실행취소 안내를
    다시 보내려고 `threading.Timer` 를 건다 (`_schedule_undo_expiry`). 이 턴은 그
    전에 끝나므로, 닫은 뒤에 오는 호출은 조용히 버린다 — 안 그러면 다음 턴 결과에
    지난 턴의 안내가 섞인다.
    """

    def __init__(self) -> None:
        self.parts: list[str] = []
        self._closed = False

    def __call__(self, text: Any = "", **kwargs: Any) -> None:
        if self._closed:
            return
        body = kwargs.get("text", text)
        plain = body.strip() if isinstance(body, str) else ""
        rich = flatten_blocks(kwargs["blocks"]) if kwargs.get("blocks") else ""

        # ★ 블록이 있으면 블록을 쓴다. `text=` 는 Slack 이 알림 미리보기에 쓰는
        #   **폴백**이라 블록 첫 줄과 같은 문장인 경우가 대부분이다 — 둘 다 실으면
        #   모델이 같은 말을 두 번 읽는다.
        chunk = rich if rich and (not plain or plain in rich) else "\n".join(x for x in (plain, rich) if x)
        if chunk:
            self.parts.append(chunk)

    def close(self) -> str:
        self._closed = True
        return "\n".join(p for p in self.parts if p).strip()


def flatten_blocks(blocks: list[dict]) -> str:
    """Block Kit 을 평문으로. 화면 요소는 버리고 **글자만** 남긴다.

    타입별로 분기하지 않고 트리에서 `text` 문자열을 훑는다. 블록 종류가 늘어도
    (`blocks.py` 는 계속 자란다) 이 함수를 같이 고칠 필요가 없다.
    """
    seen: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            # 버튼은 **누를 수 없는** 요소다. 글자만 실으면 모델이 "삭제"를
            # 답변으로 옮겨 적는다. 토큰만 쓰고 해가 된다.
            # ★ 체크박스·셀렉트는 **뺄 수 없다** — `/del` 인자 없음의 계획 목록이
            #   그 옵션 라벨 안에 들어 있다. 여기까지 지우면 모델이 지울 번호를
            #   못 본다 (실제로 한 번 지웠다가 목록이 통째로 사라졌다).
            if node.get("type") in _INTERACTIVE_TYPES:
                return
            for key, value in node.items():
                if key == "text" and isinstance(value, str):
                    cleaned = _strip_mrkdwn(value)
                    if cleaned and cleaned not in seen:
                        seen.append(cleaned)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(blocks)
    return "\n".join(seen)


# 눌러야 동작하는 요소들 — 텍스트 채널에는 옮길 수 없다.
_INTERACTIVE_TYPES = frozenset({"button", "overflow", "datepicker"})

_LINK_RE = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")


def _strip_mrkdwn(text: str) -> str:
    """Slack mrkdwn 링크를 사람이 읽는 모양으로. 나머지 기호는 그대로 둔다."""
    return _LINK_RE.sub(r"\2 (\1)", text).strip()


# ── 실행 ──────────────────────────────────────────────────────────────


def parse(line: str) -> tuple[str, str]:
    """'/plan 과제 #프로젝트' -> ('/plan', '과제 #프로젝트').

    앞의 `/` 는 있어도 없어도 받는다 — 모델이 자주 빠뜨린다.
    """
    stripped = (line or "").strip()
    if not stripped:
        raise SlashError("빈 명령입니다. " + usage())
    if not stripped.startswith("/"):
        stripped = "/" + stripped
    head, _, rest = stripped.partition(" ")
    name = head.lower()
    if name not in COMMANDS:
        raise SlashError(f"'{head}' 은(는) 없는 명령입니다.\n" + usage())
    return name, rest.strip()


def usage() -> str:
    """모델에게 보여줄 명령표. 툴 설명에 실리므로 한 줄씩 짧게."""
    return "쓸 수 있는 명령:\n" + "\n".join(f"  {k} {v}" for k, v in COMMANDS.items())


def run(
    cfg: "Config",
    line: str,
    *,
    actor: str = "agent",
    channel: str | None = None,
    day: str | None = None,
) -> str:
    """슬래시 명령 한 줄을 실행하고 결과 텍스트를 돌려준다.

    `channel` 은 받되 **발송에는 쓰지 않는다** (위 docstring). 예약 알림처럼
    "어디로 보낼지"를 기록해 두는 툴이 나중에 쓸 수 있게 자리만 남긴다.

    `day` 는 `/plan` 이 **어느 날짜에** 넣을지다. 없으면 논리적 오늘.
    ★ 이것이 없을 때 "내일 09시에 딥워크 넣어줘" 가 **오늘에 들어갔다.**
    에러가 안 나고 "추가했습니다" 라고 답해서, 툴 이름만 보는 채점은 통과했다.
    """
    name, rest = parse(line)
    muted = _mute_slack(cfg)
    target_day = _normalize_day(cfg, day)

    if name == "/view":
        # 인자로 준 날짜(`/view 어제`)가 우선이고, 없으면 `day` 를 쓴다.
        # `target_day` 는 이미 해석된 YYYY-MM-DD 라 여기서 두 번 풀지 않는다.
        return _truncate(_view_text(muted, rest or (target_day or "")))
    if name == "/week":
        return _truncate(_week_text(muted))

    collector = _Collector()
    _delegate(muted, name, rest, collector, actor=actor, channel=channel, day=target_day)
    out = collector.close() or "(결과 없음)"
    return _truncate(out + _day_note(name, target_day))


# 날짜를 안 준 채로 쓰면 **오늘**에 들어가는 명령들.
_DAY_SENSITIVE = frozenset({"/plan", "/view", "/del", "/done", "/doing", "/defer", "/memo"})


def _day_note(name: str, day: str | None) -> str:
    """날짜를 안 줬을 때 **결과에** 그 사실을 적는다.

    ★ 프롬프트로는 안 잡혔다. "내일 09시에 딥워크 넣어줘" 에 모델이 `day` 를 빠뜨리는
    비율이 2회 중 1회였고(실측), 그러면 계획이 조용히 오늘에 들어간다.
    결과에는 이미 `계획을 추가했습니다 (2026-08-23)` 처럼 날짜가 찍히는데 모델이
    그걸 안 읽었다 — **가정을 명시적인 문장으로** 만들어 준다.

    이 저장소의 규칙 그대로다: 툴 결과는 "무엇인지"와 함께 "무엇이 없는지"를 말한다.
    모델이 이 줄을 읽고 스스로 고칠 수 있는 자리가 여기뿐이다.
    """
    if day is not None or name not in _DAY_SENSITIVE:
        return ""
    return "\n(날짜를 안 줘서 **오늘** 기준으로 처리했습니다. 다른 날이면 day 인자를 주세요.)"


def _normalize_day(cfg: "Config", day: str | None) -> str | None:
    """'내일' 같은 말도 날짜로 바꿔 준다. 못 읽으면 `SlashError`.

    모델은 `day="내일"` 을 넘긴다. 여기서 안 받으면 `plan_instance.day` 에
    '내일' 이라는 문자열이 들어가고, 그 계획은 **어느 날짜에도 안 뜬다.**
    """
    if not day or not str(day).strip():
        return None
    return _resolve_day(cfg, str(day))


def _delegate(
    cfg: "Config",
    name: str,
    rest: str,
    collector: _Collector,
    *,
    actor: str,
    channel: str | None,
    day: str | None = None,
) -> None:
    """`slackio.app` 의 dispatch 함수를 부른다. **import 는 여기서** (아래 참고)."""
    # ★ 지연 import. `slackio.app` 은 slack_bolt 를 끌어오고 그것만으로 상주가
    #   +27MB 다 (실측 12.4 → 39.2MB). 슬래시를 한 번도 안 쓰는 세션에서는
    #   그 27MB 를 안 쓴다.
    from lifetrainer.slackio import app as slack_app

    command = {"text": rest, "user_id": actor, "channel_id": channel}
    # ★ **`day` 를 전부에 넘긴다.** `/plan` 에만 넘겼을 때 `/done 1` 은 `day` 를
    #   조용히 버리고 **오늘의 1번**을 바꿨다 — 모델이 `day="내일"` 을 함께 보내는데도.
    #   `/plan` 에서 고친 것과 똑같은 버그를 옆 명령들이 그대로 갖고 있었다.
    #   `/lt`(요약)·`/log`(기록 시각을 본문에서 받는다)만 날짜를 안 쓴다.
    kind = _DELEGATED[name]
    if kind == "sub":
        sub = rest.split(maxsplit=1)[0].lower() if rest else "ping"
        slack_app._dispatch_lt(cfg, sub, collector)
    elif kind == "text":
        if name == "/plan":
            slack_app._dispatch_plan(cfg, rest, collector, day)
        else:
            slack_app._dispatch_memo(cfg, rest, collector, day)
    elif kind == "status":
        slack_app._dispatch_set_status(cfg, name.lstrip("/"), rest, collector, actor, day)
    elif name == "/del":
        slack_app._dispatch_del(cfg, command, collector, day)
    else:  # "/log" — 기록 시각은 본문의 `at HH:MM` 로 받는다
        slack_app._dispatch_log(cfg, command, collector)


def _mute_slack(cfg: "Config") -> "Config":
    """봇 토큰을 비운 설정 사본. 발송 경로가 코드 수준에서 사라진다."""
    return dataclasses.replace(cfg, slack=dataclasses.replace(cfg.slack, bot_token=""))


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n... (앞 {MAX_OUTPUT_CHARS}자만 실었습니다)"


# ── /view · /week 의 텍스트판 ──────────────────────────────────────────


def _today(cfg: "Config") -> str:
    return timeutil.day_str(timeutil.now_ts(), cfg.tz, boundary_hour=cfg.rollup.day_boundary_hour)


def _view_text(cfg: "Config", arg: str) -> str:
    """그날의 계획 목록 + 달성률. 숫자는 `plan.achieve` 가 센 것을 그대로 쓴다.

    ★ **번호는 `ordinal` 이다.** `/done N` · `/del N` 이 받는 것이 그 번호이므로,
    보여 주는 번호와 조작하는 번호가 반드시 같아야 한다 — 다르면 모델이 3번을
    읽고 3번을 지웠는데 다른 게 지워진다. `plan_instance.id` 를 보여 주고 싶어지지만
    (`llm/tools._tool_get_plans` 는 그쪽이다) 슬래시 경로에서는 틀린 번호다.
    """
    from lifetrainer.plan import models as plan_models
    from lifetrainer.plan.achieve import day_achievement

    day = _resolve_day(cfg, arg)
    conn = db.open_db(cfg)
    try:
        overall, achieved, total = day_achievement(conn, cfg, day)
        rows = plan_models.list_instances(conn, day, include_archived=False, include_done=True)
        ratios = {pi.instance_id: pi.achievement for pi in _instances(conn, cfg, day)}
    finally:
        conn.close()

    header = f"{day} 계획 {total}건 · 완료 {achieved}건 · 전체 달성률 {overall * 100:.0f}%"
    if not rows:
        return header + "\n(등록된 계획이 없습니다)"

    lines = [header]
    for row in rows:
        ratio = ratios.get(row.id)
        span = (
            f"{row.start_min // 60:02d}:{row.start_min % 60:02d}"
            f"~{row.end_min // 60:02d}:{row.end_min % 60:02d}"
        )
        tail = f" 달성 {ratio * 100:.0f}%" if ratio is not None else ""
        lines.append(f"  {row.ordinal}. {span} {row.title} ({row.status}){tail}")
    return "\n".join(lines)


def _instances(conn: "sqlite3.Connection", cfg: "Config", day: str) -> list:
    """달성률이 채워진 인스턴스들. 실패해도 목록 자체는 나와야 하므로 삼킨다."""
    from lifetrainer.plan.achieve import plans_for_day

    try:
        return plans_for_day(conn, cfg, day)
    except Exception:  # noqa: BLE001
        logger.exception("달성률 계산 실패 (day=%s) — 목록만 돌려준다", day)
        return []


# 모델이 실제로 쓰는 낱말들. 하루 계산은 `llm/tools._shift_day` 하나만 쓴다.
#
# ★ `today`/`오늘` 을 받는 이유는 실측이다. 모델이 `/view today` 를 불렀다가
#   거부당하고 `/view 2026-08-23` 으로 고쳐 부르느라 왕복을 한 번 더 돌았다.
#   이 기기에서 왕복 하나가 수 초다 — 거부할 이유가 없는 표현은 받는다.
_DAY_WORDS: dict[str, int] = {
    "오늘": 0, "today": 0,
    "어제": -1, "yesterday": -1,
    "내일": 1, "tomorrow": 1,
    "모레": 2,
}


def _resolve_day(cfg: "Config", arg: str) -> str:
    """'어제' · '오늘' · 'YYYY-MM-DD' · 빈 값(오늘) 을 날짜 문자열로."""
    text = (arg or "").strip()
    if not text:
        return _today(cfg)
    delta = _DAY_WORDS.get(text.lower())
    if delta is not None:
        from lifetrainer.llm.tools import _shift_day  # 하루 계산을 두 번 쓰지 않는다

        return _shift_day(_today(cfg), delta)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    raise SlashError(
        f"날짜를 해석할 수 없습니다: {text!r} "
        f"(쓸 수 있는 말: {', '.join(sorted(_DAY_WORDS))} 또는 2026-08-23 모양)"
    )


def _week_text(cfg: "Config") -> str:
    """주간 비교의 텍스트판. PNG 는 안 그린다 (위 docstring)."""
    from lifetrainer.report import stats as stats_mod
    from lifetrainer.rollup.classify import Classifier

    conn: "sqlite3.Connection" = db.open_db(cfg)
    try:
        classifier = Classifier.from_yaml(cfg.rollup.rules_path)
        week_stats = stats_mod.compute_weekly(conn, cfg, _today(cfg))
        return stats_mod.render_weekly_text(week_stats, classifier)
    finally:
        conn.close()
