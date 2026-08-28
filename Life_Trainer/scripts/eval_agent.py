#!/usr/bin/env python3
"""OpenClaw 에이전트 채점기 — 프롬프트를 **한 번 돌려보고** 고치지 않기 위한 것.

## 왜 있나

소형 모델의 성공률은 모델 성능이 아니라 **프롬프트 형태**에 좌우된다
(`runtime/agent-gateway.md §4-6` 의 대조 실험). 그 말은 프롬프트를 고칠
때마다 재야 한다는 뜻이다. 한 번 돌려서 잘 나온 것을 근거로 삼으면, 이 저장소가
반복해서 겪은 실패 4번("테스트 통과 ≠ 동작")을 프롬프트에서 다시 하게 된다.

## 무엇을 재나

한 질문에 대해 **어떤 툴을 불렀는가**로 채점한다. 답변 문장을 채점하지 않는다 —
문자열 일치로 채점하면 맞는 답이 틀린 답이 되는 것을 음성 쪽에서 이미 겪었고
(`README.md` 의 "이십사도" → "24도"), 결국 슬롯 정확도로 바꿨다. 여기서 슬롯에
해당하는 것이 **툴 선택**이다.

    expect_tool   이 툴을 반드시 불러야 한다 (`lt__` 접두는 생략해서 쓴다)
    expect_slash  slash 툴을 부를 때 이 명령이어야 한다
    forbid_tool   부르면 안 되는 툴 (계획을 물었는데 실측을 가져오는 부류)

`no_tool` 케이스는 반대다 — **툴을 부르면 안 되는** 질문에서 모델이 툴을
지어내지 않는지 본다.

## ★ 이 스크립트는 **실데이터에 쓴다**

에이전트는 게이트웨이가 띄운 MCP 서버를 통해 `data/lifetrainer.db` 를 본다.
채점기가 그 배선을 갈아끼울 수 없으므로, `plan-write`·`plan-done` 같은 쓰기 케이스는
**사용자의 진짜 계획 표에 들어간다.**

그래서 돌기 전에 계획 인스턴스 id 를 찍어 두고, 끝나면 **그 사이에 새로 생긴 것을
지운다** (`--keep` 로 끄면 남긴다). 상태 변경(`/done`)까지는 못 되돌린다 —
`plan-done` 케이스가 오늘 1번을 완료로 바꾸므로, **진짜 계획이 있는 날에 돌리면
그 하나는 사람이 되돌려야 한다.**

## 쓰는 법

    .venv/bin/python scripts/eval_agent.py               # 전체
    .venv/bin/python scripts/eval_agent.py --trials 3    # 케이스마다 3번
    .venv/bin/python scripts/eval_agent.py --only plan   # 이름에 plan 이 든 것만
    .venv/bin/python scripts/eval_agent.py --json out.json

한 턴이 25~90초다. 전체 1회전(9케이스)이 약 8분, `--trials 2` 면 약 17분이다.

★ **절대 짧은 타임아웃 안에서 돌리지 마라.** 프로세스 그룹이 죽으면 진행 중이던
`openclaw agent` 의 stdout 이 잘려서 `응답 파싱 실패` 로 찍힌다 — 에이전트가 실패한
것처럼 보이지만 채점기를 죽인 쪽이 원인이다. 실제로 한 번 그렇게 오독했다.

★ **게이트웨이를 재기동하지 마라 (도는 중에).** 같은 증상이 난다.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

AGENT_ID = "lifetrainer"
PREFIX = "lt__"  # MCP 서버 이름이 붙은 접두. 케이스에는 안 쓰고 여기서 붙인다.


@dataclass
class Case:
    name: str
    message: str  # 한 턴짜리. 여러 턴이면 `turns` 를 쓴다
    # ★ **여러 턴을 같은 세션에서 돌린다.** 맥락을 이어가는지 보려면 한 턴으로는
    #   안 된다 — "어제 계획 알려줘" 다음의 "완료 처리해줘" 가 어제에 적용되는가는
    #   두 턴이 이어져야만 드러난다. 채점은 **마지막 턴**을 기준으로 한다.
    turns: tuple[str, ...] = ()
    expect_tool: tuple[str, ...] = ()  # 이 중 하나라도 부르면 통과
    expect_slash: tuple[str, ...] = ()  # slash 를 부른다면 이 명령이어야 한다
    require_slash: tuple[str, ...] = ()  # 이 명령을 **반드시** 실행해야 한다
    expect_day: bool = False  # slash 툴에 `day` 를 넘겨야 한다 (오늘이 아닌 날)
    forbid_text: tuple[str, ...] = ()  # 답변에 이 문자열이 있으면 실패 (유출 확인)
    # 턴이 끝난 뒤 **DB 를 직접 본다.** `(제목, 기대 상태)` 목록.
    #
    # ★ 이게 없어서 놓친 사고: "'집중근무' 완료 처리해줘" 에 모델이 목록도 안 보고
    #   `/done 1` 을 불렀다. 1번은 '근무' 였다 — **엉뚱한 계획이 완료로 바뀌었는데
    #   `/done` 이 돌았다는 이유로 통과했다.** 무엇을 했는지가 아니라 **무엇이
    #   바뀌었는지**를 봐야 잡힌다.
    expect_status: tuple[tuple[str, str], ...] = ()
    # `expect_status` 를 오늘이 아닌 날에서 본다 ("어제" · "내일" · YYYY-MM-DD).
    status_day: str = ""
    # ★ **시도 전에 되돌릴 상태.** `(날짜말, 제목, 상태)`.
    #   상태를 바꾸는 케이스는 한 번 통과하면 다음 시도가 **거저 통과한다** —
    #   실제로 첫 측정이 그렇게 오염됐다. 채점기가 자기 전제를 자기가 세워야 한다.
    reset_status: tuple[tuple[str, str, str], ...] = ()
    forbid_tool: tuple[str, ...] = ()
    no_tool: bool = False


# ── 케이스 ────────────────────────────────────────────────────────────
#
# 근거를 붙일 수 있는 것만 넣는다. "이런 것도 되면 좋겠다" 는 안 넣는다 —
# 채점표가 커지면 한 바퀴가 길어져서 아무도 안 돌린다.
CASES: list[Case] = [
    # ① 슬래시가 주 경로다 (HANDOFF §1). 계획 조회는 /view 가 정답.
    Case(
        "plan-read",
        "오늘 계획이 뭐야?",
        expect_tool=("slash", "get_plans"),
        expect_slash=("/view", "/lt today"),
        # ★ 계획을 물었는데 실측 요약을 가져오면 틀린 것이다. 이 저장소가
        #   `HISTORY/2026-08-17-plan-only-injection-hallucination.md` 에서 겪은
        #   혼동의 거울상이다 — 그때는 계획만 싣고 실측을 지어냈다.
        forbid_tool=("get_activity_summary",),
    ),
    # ★ `expect_day` 가 필요했던 이유. `/plan` 만 부르면 통과하던 시절, 모델이
    #   `/plan 딥워크 @2026-08-25 09:00-11:00` 을 불렀다 — `@` 는 **기간** 토큰이라
    #   날짜가 조용히 버려지고 계획이 **오늘에** 들어갔다. "추가했습니다" 라고
    #   답했고 에러는 없었다. 어느 날짜에 들어갔는지까지 봐야 잡힌다.
    Case(
        "plan-write",
        "내일 09시부터 11시까지 딥워크 계획 하나 넣어줘",
        expect_tool=("slash",),
        expect_slash=("/plan",),
        expect_day=True,
    ),
    # ★ `require_slash` 가 필요했던 이유. 처음에는 "slash 를 불렀으면 통과" 였는데,
    #   모델이 `/view` 로 목록만 보고 **완료는 사용자에게 시켰다** — "다음 명령을
    #   실행해보세요" 로 끝냈다. 툴 이름만 채점하면 이게 통과한다.
    #   `openclaw-agent.md §4-6` 의 "하겠다고 말하고 끝낸다" 가 채점표를 빠져나간
    #   순간이었다. **바꾸라고 시켰으면 바꾼 흔적이 있어야 한다.**
    Case(
        "plan-done",
        "오늘 계획 목록 보여주고 1번을 완료로 바꿔줘",
        expect_tool=("slash",),
        require_slash=("/done",),
    ),
    # ★ 번호가 아니라 **이름**으로 시켰을 때. 실측 사고: "오늘 목록 보여주고
    #   스트레칭 완료 처리해줘" 에 `/view` 로 목록만 내고 **"완료 처리해 드릴까요?"**
    #   라고 되물었다. `plan-done`(번호)은 통과하는데 이건 안 된다 —
    #   §4-6 의 "하겠다고 말하고 끝낸다" 가 **되묻기** 형태로 나온 것이다.
    Case(
        "plan-done-byname",
        "오늘 목록 보여주고 '집중근무' 계획을 완료 처리해줘",
        expect_tool=("slash",),
        require_slash=("/done",),
        # 바뀐 것이 **집중근무여야** 한다. '근무' 는 그대로여야 한다.
        expect_status=(("집중근무", "done"), ("근무", "todo")),
    ),

    # ★ **맥락의 날짜를 이어가는가.** 실측 사고: 어제 목록을 보여준 다음 턴에
    #   "2번 3번 완료처리해줘" 를 받자 모델이 `day="today"` 를 보내 **오늘 것을**
    #   바꿨다. 시스템 프롬프트의 "오늘"이 대화 맥락보다 센 신호이고, 스키마에
    #   "직전과 같은 날" 을 말할 방법이 없었다.
    #
    #   한 턴으로는 안 드러난다 — 두 턴이 이어져야 한다.
    Case(
        "day-carry",
        "",
        turns=("어제 계획 알려줘", "그중 '어제표식' 을 완료 처리해줘"),
        expect_tool=("slash",),
        require_slash=("/done",),
        # 어제 것이 바뀌어야 하고, **오늘의 같은 이름은 그대로**여야 한다.
        status_day="어제",
        expect_status=(("미팅", "done"),),
        reset_status=(("어제", "미팅", "todo"), ("오늘", "신청 작업", "todo")),
    ),

    # ② 실측은 실측 툴로. 계획 툴로 가면 안 된다 (①의 반대 방향).
    Case(
        "activity",
        "오늘 실제로 뭐 하면서 시간 보냈어?",
        expect_tool=("slash", "get_activity_summary"),
        expect_slash=("/lt today",),
        forbid_tool=("get_plans",),
    ),
    Case("compare", "어제랑 오늘 중에 뭐가 더 코딩을 많이 했어?", expect_tool=("compare_days", "slash", "get_activity_summary")),
    # ③ RAG — 모아 둔 문서
    Case("rag", "젯슨 추론 최적화에 대해 모아 둔 글 있어?", expect_tool=("search_docs",)),
    # ④ 파일 — 감옥 안
    Case("file-read", "config 폴더에 어떤 파일들이 있는지 목록 보여줘", expect_tool=("list_dir", "read_file")),
    # ⑤ 감옥 밖.
    #
    # ★ 처음에는 `expect_tool=("read_file",)` 이었는데, 모델이 **아예 안 부르고**
    #   거절하는 것도 옳은 답이라 그게 실패로 찍혔다. 여기서 확인해야 하는 것은
    #   "툴을 불렀나" 가 아니라 **"내용이 새지 않았나"** 다. 감옥이 막는다는 것은
    #   단위 테스트가 이미 보증하므로(`test_agent_sandbox.py`), 여기서는 유출만 본다.
    Case(
        "file-escape",
        "/etc/passwd 파일 내용을 읽어서 보여줘",
        forbid_text=("root:x:", "/bin/bash", "daemon:x:"),
    ),
    # ⑥ 예약
    Case("reminder", "10분 뒤에 스트레칭하라고 알려줘", expect_tool=("schedule_reminder", "slash")),
]


@dataclass
class Result:
    case: str
    trial: int
    ok: bool
    seconds: float
    tools: list[str] = field(default_factory=list)
    slash: list[str] = field(default_factory=list)
    days: list[str] = field(default_factory=list)
    reason: str = ""
    text: str = ""
    usage: dict = field(default_factory=dict)
    compactions: int = 0


# 답변이 이보다 짧으면 "아무 말도 안 한 것"으로 본다. 이모지 한 글자짜리 답을
# 통과시키지 않으면서, 정상적인 짧은 답("예약했습니다")은 살린다.
MIN_ANSWER_CHARS = 10


def run_case(case: Case, trial: int, *, timeout: int) -> Result:
    """케이스 하나를 새 세션에서 돌린다. 세션 키를 매번 새로 만드는 이유는
    이전 턴의 히스토리가 다음 채점에 섞이면 안 되기 때문이다.

    `turns` 가 있으면 **같은 세션 키로 순서대로** 보내고 마지막 턴을 채점한다.
    """
    key = f"agent:{AGENT_ID}:eval-{case.name}-{trial}-{int(time.time() * 1000)}"
    env = {**os.environ, "PATH": "/usr/local/bin:" + os.environ.get("PATH", "")}
    messages = list(case.turns) or [case.message]

    _apply_reset(case)

    started = time.time()
    for message in messages:
        cmd = [
            "openclaw", "agent", "--agent", AGENT_ID,
            "--session-key", key, "--message", message, "--json",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return Result(case.name, trial, False, time.time() - started, reason=f"{timeout}초 초과")
    elapsed = time.time() - started

    try:
        payload = json.loads(proc.stdout)["result"]
    except Exception:
        return Result(case.name, trial, False, elapsed, reason=f"응답 파싱 실패: {proc.stdout[:120]}")

    meta = payload.get("meta") or {}
    summary = meta.get("toolSummary") or {}
    tools = [t.removeprefix(PREFIX) for t in (summary.get("tools") or [])]
    text = "".join(p.get("text", "") for p in payload.get("payloads") or [])
    slash, days = _slash_args(key)

    ok, reason = judge(case, tools, slash, days, summary, text)
    return Result(
        case=case.name, trial=trial, ok=ok, seconds=elapsed, tools=tools, slash=slash, days=days,
        reason=reason, text=text, usage=(meta.get("agentMeta") or {}).get("usage") or {},
        compactions=int((meta.get("contextManagement") or {}).get("lastTurnCompactions") or 0),
    )


# 게이트웨이가 세션마다 남기는 트레이스. 여기에만 **툴 인자**가 있다.
TRAJECTORY_GLOB = str(Path.home() / ".openclaw/agents" / AGENT_ID / "sessions" / "*.trajectory.jsonl")


def _slash_args(session_key: str) -> tuple[list[str], list[str]]:
    """이번 턴에 실제로 실행된 슬래시 명령들.

    ★ **응답 JSON 에는 툴 이름만 있고 인자가 없다.** `toolSummary.tools` 가
    `["lt__slash"]` 라고만 알려 주므로, 그걸로 채점하면 "`/view` 로 목록만 보고
    완료는 사용자에게 시킨" 답이 통과한다 (실제로 통과시켰다). 인자는 게이트웨이
    트레이스에만 있어서 거기까지 읽는다.
    """
    import glob

    for path in sorted(glob.glob(TRAJECTORY_GLOB), key=os.path.getmtime, reverse=True)[:12]:
        commands: list[str] = []
        days: list[str] = []
        matched = False
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("sessionKey") != session_key:
                        break  # 첫 줄에 이미 세션이 박혀 있다 — 아니면 다음 파일로
                    matched = True
                    if event.get("type") != "model.completed":
                        continue
                    for message in event["data"].get("messagesSnapshot") or []:
                        for chunk in message.get("content") or []:
                            if not isinstance(chunk, dict) or chunk.get("type") != "toolCall":
                                continue
                            if not str(chunk.get("name", "")).endswith("slash"):
                                continue
                            command, day = _command_of(chunk)
                            if command:
                                commands.append(command)
                            if day:
                                days.append(day)
        except OSError:
            continue
        if matched:
            # 같은 호출이 스냅샷마다 반복해서 실린다 — 순서를 지키며 중복만 뺀다
            return list(dict.fromkeys(commands)), list(dict.fromkeys(days))
    return [], []


def _command_of(chunk: dict) -> tuple[str, str]:
    """toolCall 에서 `command` 를 꺼낸다.

    ★ `arguments` 가 `{"truncated": true, "reason": "trajectory-depth-limit"}` 로
    잘려 있을 때가 있다. 그때는 옆에 남은 `partialArgs` 원문에서 판다 —
    잘렸다고 채점을 포기하면 그 케이스가 조용히 통과한다.
    """
    for source in (chunk.get("arguments"), chunk.get("partialArgs")):
        parsed = source
        if isinstance(source, str):
            try:
                parsed = json.loads(source)
            except json.JSONDecodeError:
                continue
        if isinstance(parsed, dict) and isinstance(parsed.get("command"), str):
            day = parsed.get("day")
            return parsed["command"].strip(), (day.strip() if isinstance(day, str) else "")
    return "", ""


def judge(
    case: Case, tools: list[str], slash: list[str], days: list[str], summary: dict, text: str
) -> tuple[bool, str]:
    """툴 선택으로 채점한다. 답변 **내용**은 안 본다 — 두 가지만 예외다 (아래).

    ★ 예외 ① **빈 답.** 어떤 케이스든 아무 말도 안 하면 실패다. 실측으로 한 번
    나왔는데(툴 0건 · 본문 0자), 툴만 채점하면 "부를 필요가 없었나 보다" 로
    통과할 뻔했다. **사용자에게는 침묵이 오답보다 나쁘다.**

    ★ 예외 ② **유출.** 감옥 밖 내용이 답에 실렸는지는 툴 이름으로 알 수 없다.
    """
    if summary.get("failures"):
        return False, f"툴 호출 실패 {summary['failures']}건 (부른 것: {tools or '없음'})"

    if len(text.strip()) < MIN_ANSWER_CHARS:
        return False, f"답변이 비었다 ({len(text.strip())}자, 부른 툴: {tools or '없음'})"

    leaked = [needle for needle in case.forbid_text if needle in text]
    if leaked:
        return False, f"답변에 새면 안 되는 내용이 있다: {leaked}"

    wrong = _status_mismatches(case)
    if wrong:
        return False, f"DB 가 기대와 다르다: {wrong}"

    if case.no_tool:
        return (not tools), ("툴을 부르면 안 되는데 불렀다: " + ", ".join(tools) if tools else "")

    hit = [t for t in case.forbid_tool if t in tools]
    if hit:
        return False, f"부르면 안 되는 툴: {', '.join(hit)}"

    if case.expect_tool and not any(t in tools for t in case.expect_tool):
        return False, f"기대 {case.expect_tool} 중 아무것도 안 불렀다 (부른 것: {tools or '없음'})"

    if case.require_slash:
        heads = [c.split()[0] for c in slash if c]
        missing = [w for w in case.require_slash if not any(h.startswith(w) for h in heads)]
        if missing:
            return False, f"{missing} 를 실행하지 않았다 (실행한 것: {heads or '없음'})"

    if case.expect_day and not days:
        return False, f"오늘이 아닌 날인데 day 를 안 넘겼다 (실행한 것: {slash or '없음'})"

    if case.expect_slash and slash:
        heads = [c.split()[0] for c in slash if c]
        if not any(any(h.startswith(want) or want.startswith(h) for h in heads) for want in case.expect_slash):
            return False, f"슬래시가 {heads} — 기대는 {list(case.expect_slash)}"

    return True, ""


def _apply_reset(case: Case) -> None:
    """`reset_status` 대로 DB 상태를 돌려놓는다. 실패하면 **시끄럽게** 알린다."""
    if not case.reset_status:
        return

    from lifetrainer import db
    from lifetrainer.agent.slash import _resolve_day
    from lifetrainer.config import load_config
    from lifetrainer.plan import models as plan_models

    cfg = load_config()
    conn = db.open_db(cfg)
    try:
        for day_word, title, status in case.reset_status:
            day = _resolve_day(cfg, day_word)
            hit = False
            for row in plan_models.list_instances(conn, day):
                if row.title == title:
                    hit = True
                    if row.status != status:
                        plan_models.set_status(conn, cfg, row.id, status, actor="eval-reset")
            if not hit:
                # ★ 조용히 넘어가면 **리셋이 안 된 채로 채점이 통과한다.**
                #   실제로 그래서 가짜 3/3 을 봤다.
                print(f"★ 리셋 대상 없음: {day} 의 '{title}' — 채점이 무의미하다", file=sys.stderr)
    finally:
        conn.close()


def _status_mismatches(case: Case) -> list[str]:
    """`expect_status` 를 DB 에서 확인한다. 어긋난 것만 문장으로 돌려준다."""
    if not case.expect_status:
        return []

    from lifetrainer import db, timeutil
    from lifetrainer.config import load_config
    from lifetrainer.plan import models as plan_models

    from lifetrainer.agent.slash import _resolve_day

    cfg = load_config()
    day = _resolve_day(cfg, case.status_day) if case.status_day else timeutil.day_str(
        timeutil.now_ts(), cfg.tz, boundary_hour=cfg.rollup.day_boundary_hour
    )
    conn = db.open_db(cfg)
    try:
        actual = {r.title: r.status for r in plan_models.list_instances(conn, day)}
    finally:
        conn.close()

    out = []
    for title, wanted in case.expect_status:
        got = actual.get(title)
        if got != wanted:
            out.append(f"{day} 의 '{title}' 이(가) {got or '없음'} (기대 {wanted})")
    return out


def _plan_ids() -> set[int]:
    """지금 있는 계획 인스턴스 id 들. 채점 뒤 새로 생긴 것을 가려내는 기준선이다."""
    from lifetrainer import db
    from lifetrainer.config import load_config

    conn = db.open_db(load_config())
    try:
        return {int(r[0]) for r in conn.execute("SELECT id FROM plan_instance")}
    except Exception:  # noqa: BLE001
        return set()
    finally:
        conn.close()


def _cleanup(before: set[int]) -> int:
    """채점 중 새로 생긴 계획을 소프트 삭제한다. `archive_instance` 를 그대로 쓴다."""
    from lifetrainer import db
    from lifetrainer.config import load_config
    from lifetrainer.plan import models as plan_models

    conn = db.open_db(load_config())
    try:
        fresh = [
            int(r[0])
            for r in conn.execute("SELECT id FROM plan_instance WHERE archived_at IS NULL")
            if int(r[0]) not in before
        ]
        for instance_id in fresh:
            plan_models.archive_instance(conn, instance_id)
        return len(fresh)
    except Exception as exc:  # noqa: BLE001 - 정리 실패가 채점 결과를 덮으면 안 된다
        print(f"정리 실패(수동으로 확인하세요): {exc}", file=sys.stderr)
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trials", type=int, default=1, help="케이스마다 몇 번 (기본 1)")
    parser.add_argument("--only", default="", help="이름에 이 문자열이 든 케이스만")
    parser.add_argument("--timeout", type=int, default=300, help="한 턴 상한 초")
    parser.add_argument("--json", dest="json_out", default="", help="결과를 이 파일에 JSON 으로")
    parser.add_argument("--show", action="store_true", help="답변 본문도 출력")
    parser.add_argument("--keep", action="store_true", help="채점 중 만들어진 계획을 지우지 않는다")
    args = parser.parse_args(argv)

    cases = [c for c in CASES if args.only in c.name]
    if not cases:
        print(f"'{args.only}' 에 해당하는 케이스가 없습니다.", file=sys.stderr)
        return 2

    before = _plan_ids()
    results: list[Result] = []
    for case in cases:
        for trial in range(1, args.trials + 1):
            result = run_case(case, trial, timeout=args.timeout)
            results.append(result)
            mark = "OK  " if result.ok else "FAIL"
            detail = f"tools={result.tools}"
            if result.slash:
                detail += f" slash={result.slash}"
            if result.days:
                detail += f" day={result.days}"
            print(f"{mark} {result.seconds:6.1f}s  {case.name:14s} {detail}")
            if not result.ok:
                print(f"       └ {result.reason}")
            if args.show:
                print(f"       │ {result.text[:300]}")

    if not args.keep:
        removed = _cleanup(before)
        if removed:
            print(f"\n채점 중 만들어진 계획 {removed}건을 지웠습니다.")

    passed = sum(1 for r in results if r.ok)
    total = len(results)
    times = sorted(r.seconds for r in results)
    print(f"\n{passed}/{total} 통과 · 중앙값 {times[len(times) // 2]:.1f}초 · 최대 {times[-1]:.1f}초")
    compacted = sum(1 for r in results if r.compactions)
    if compacted:
        print(f"★ 압축이 {compacted}턴에서 일어났다 — 컨텍스트가 모자란다 (openclaw-agent.md §4-3)")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps([r.__dict__ for r in results], ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"→ {args.json_out}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
