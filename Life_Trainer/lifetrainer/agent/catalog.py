"""에이전트에게 내보낼 툴 목록 — **토큰 예산을 코드가 지킨다.**

## 이 파일의 존재 이유는 예산이다

`openclaw-agent.md §3` 의 실측: 프레임워크 기본 구성의 시스템 프롬프트가
**12,541 토큰**이고 프롬프트 처리가 **295 tok/s** 라 첫 턴이 41.6초였다.
그중 툴 스키마가 8,833 토큰이고 `cron` 하나가 3,912 였다.

지금은 그때보다 **더 빡빡하다.** ctx 를 40,960 → 20,480 으로 내렸으므로
(`HANDOFF.md §2`) 12.5K 를 그대로 실으면 컨텍스트의 61% 가 첫 마디 전에 사라진다.

그래서 이 모듈은 툴을 고르기만 하는 것이 아니라 **얼마짜리인지 센다.**
`budget_report()` 가 그 숫자를 내고, 테스트가 상한을 지킨다. 눈대중으로 "이 정도면
되겠지" 하지 않는다 — 이 저장소의 반복된 실패 3번이 그것이다.

## 왜 `add_plan` 을 빼는가

`llm/tools.REGISTRY` 에는 `add_plan` 이 있지만 여기서는 안 내보낸다.
`/plan` 이 같은 일을 **파싱 → SQL** 로 끝내기 때문이다 (`slash.py` docstring).
같은 능력을 두 경로로 주면 스키마 토큰을 두 번 내고, 소형 모델은 둘 중 뭘 쓸지
헷갈린다. **슬래시로 되는 것은 슬래시로 간다.**

## 왜 우리 `web_search` 를 쓰는가

OpenClaw 도 `web_search`·`web_fetch` 를 갖고 있다. 우리 것을 쓰는 이유는 이미
Serper 키가 여기 꽂혀 있고(`lt doctor` OK), **SSRF 가드와 출처 라벨**이 붙어
있기 때문이다 (`llm/webfetch.py`). 출처 라벨 없이 결과를 주면 모델이 그것을
자기 지식으로 말한다 — 실제로 겪은 사고다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from lifetrainer.agent import sandbox as sandbox_mod
from lifetrainer.agent import slash as slash_mod
from lifetrainer.llm import tools as lt_tools

# MCP 서버 이름. OpenClaw 는 툴을 `<서버>__<툴>` 로 노출하므로 짧을수록 좋다 —
# 이름이 툴마다 한 번씩 프롬프트에 실린다.
SERVER_NAME = "lt"

# `llm/tools.REGISTRY` 에서 그대로 가져오는 것들. 순서가 프롬프트 순서다.
# **자주 쓰는 것을 앞에** 둔다 — 소형 모델은 목록 앞쪽을 더 잘 고른다.
REUSED_TOOLS: tuple[str, ...] = (
    "get_plans",
    "get_activity_summary",
    "search_docs",
    "compare_days",
    "schedule_reminder",
    "list_reminders",
    "cancel_reminder",
    "web_search",
    "fetch_url",
)

# 시스템 프롬프트에서 툴 스키마가 차지해도 되는 상한(토큰).
#
# 근거: ctx 20,480. OpenClaw 압축 설정이 `maxHistoryShare 0.7` + `reserveTokens 6000`
# 이라 히스토리에 14,336, 예비에 6,000 이 잡힌다. 시스템 프롬프트는 히스토리 몫에서
# 나가므로, 워크스페이스(약 700) + 툴 + 프레임워크 골격(약 1,500)을 합쳐 4,000 을
# 넘기지 않게 잡았다. 4,000 토큰이면 첫 턴 프롬프트 처리가 295 tok/s 로 13.6초다.
TOOL_TOKEN_BUDGET = 2000


@dataclass(frozen=True)
class AgentTool:
    """MCP 로 내보내는 툴 하나. `schema` 는 MCP 의 `inputSchema` 모양이다."""

    name: str
    description: str
    schema: dict
    handler: Callable[["AgentContext", dict], str]


@dataclass
class AgentContext:
    """툴 몸통이 쓰는 실행 환경. `llm/tools.ToolContext` 를 감싸고 감옥을 더한다."""

    cfg: Any
    sandbox: sandbox_mod.Sandbox
    channel: str | None = None
    actor: str = "agent"


# ── 새로 만드는 툴들 ──────────────────────────────────────────────────
#
# 스키마 설명문은 **그대로 프롬프트 토큰**이다 (`CLAUDE.md`). 근거는 주석에 쓰고
# 스키마 안에는 짧게 쓴다. `pattern` 은 어디에도 두지 않는다 — llama.cpp 가
# 요청 전체를 400 으로 거부한다.


def _handle_slash(ctx: AgentContext, args: dict) -> str:
    line = str(args.get("command") or "").strip()
    if not line:
        return "오류: command 가 비었습니다.\n" + slash_mod.usage()
    try:
        return slash_mod.run(ctx.cfg, line, actor=ctx.actor, channel=ctx.channel)
    except slash_mod.SlashError as exc:
        return f"오류: {exc}"


def _handle_read_file(ctx: AgentContext, args: dict) -> str:
    try:
        return sandbox_mod.read_text(ctx.sandbox, str(args.get("path") or ""))
    except sandbox_mod.SandboxError as exc:
        return f"오류: {exc}"


def _handle_list_dir(ctx: AgentContext, args: dict) -> str:
    try:
        return sandbox_mod.list_dir(ctx.sandbox, str(args.get("path") or "."))
    except sandbox_mod.SandboxError as exc:
        return f"오류: {exc}"


def _handle_write_file(ctx: AgentContext, args: dict) -> str:
    try:
        return sandbox_mod.write_text(
            ctx.sandbox, str(args.get("path") or ""), str(args.get("content") or "")
        )
    except sandbox_mod.SandboxError as exc:
        return f"오류: {exc}"


def _slash_description() -> str:
    """`/` 명령표를 툴 설명에 그대로 싣는다.

    ★ 명령표를 **시스템 프롬프트가 아니라 툴 설명에** 두는 이유: 툴 스키마는
    턴마다 실리지만 Qwen3 채팅 템플릿이 시스템 내용을 먼저 렌더링하고 `# Tools` 를
    뒤에 붙이므로, 여기 있는 글자가 바뀌어도 시스템 접두사 캐시는 안 깨진다
    (`llm/context.py` 모듈 docstring 의 3층 근거와 같은 이야기다).
    """
    lines = [f"{name} {desc}" for name, desc in slash_mod.COMMANDS.items()]
    return (
        "Life Trainer 슬래시 명령을 실행한다. 계획 추가·조회·완료·삭제·기록은 "
        "이 툴이 가장 빠르고 정확하다(밀리초, DB 직접). 명령표:\n" + "\n".join(lines)
    )


NEW_TOOLS: tuple[AgentTool, ...] = (
    AgentTool(
        name="slash",
        description=_slash_description(),
        schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "실행할 명령 한 줄. 예: /plan 프로젝트 보고서 @90m",
                }
            },
            "required": ["command"],
        },
        handler=_handle_slash,
    ),
    AgentTool(
        name="read_file",
        description="허용된 폴더 안의 파일을 읽는다. 밖은 거부된다.",
        schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "파일 경로"}},
            "required": ["path"],
        },
        handler=_handle_read_file,
    ),
    AgentTool(
        name="list_dir",
        description="허용된 폴더의 파일 목록을 본다.",
        schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "폴더 경로"}},
            "required": ["path"],
        },
        handler=_handle_list_dir,
    ),
    AgentTool(
        name="write_file",
        description="허용된 쓰기 폴더에 파일을 쓴다(덮어쓴다). 그 밖은 거부된다.",
        schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "파일 경로"},
                "content": {"type": "string", "description": "파일 내용 전체"},
            },
            "required": ["path", "content"],
        },
        handler=_handle_write_file,
    ),
)


# ── 조립 ──────────────────────────────────────────────────────────────


def _reused_tool(name: str) -> AgentTool:
    """`llm/tools.REGISTRY` 의 툴을 MCP 모양으로 옮긴다.

    OpenAI 툴 스키마(`{"type":"function","function":{...}}`)에서 MCP 의
    `{name, description, inputSchema}` 로 껍데기만 바꾼다. **설명과 파라미터는
    그대로 쓴다** — 이미 이 모델에 맞춰 다듬어진 문장이고, 여기서 다시 쓰면
    두 경로의 툴이 서로 다른 말을 하게 된다.
    """
    tool = lt_tools.REGISTRY[name]
    fn = tool.schema["function"]

    def handler(ctx: AgentContext, args: dict, _name: str = name) -> str:
        # ★ 커넥션을 반드시 닫는다. 이 프로세스는 세션 내내 살아 있어서, 툴 호출마다
        #   열고 안 닫으면 sqlite 핸들이 단조 증가한다 (`slackio/app.py` 의 dispatch
        #   들이 전부 try/finally 로 닫는 것과 같은 이유 — 거기는 스레드마다 새로
        #   열기 때문이고, 여기는 더 길게 산다).
        tool_ctx = _tool_context(ctx, args)
        try:
            return lt_tools.run_tool(tool_ctx, _name, args)
        finally:
            tool_ctx.conn.close()

    return AgentTool(
        name=name,
        description=fn.get("description", ""),
        schema=fn.get("parameters") or {"type": "object", "properties": {}},
        handler=handler,
    )


def _tool_context(ctx: AgentContext, args: dict) -> lt_tools.ToolContext:
    """`llm/tools` 가 기대하는 실행 환경을 만든다.

    ★ `user_text` 를 **모델이 준 인자에서** 만든다. `_tool_search_docs` 는 기간
    필터를 "사용자가 시점을 말했을 때만" 허용하는데, 그 판정에 원문을 쓴다
    (`llm/tools._mentions_time`). OpenClaw 경로에서는 우리가 원문을 못 보므로
    **모델이 넘긴 질의어를 원문 자리에 놓는다** — 모델이 스스로 "이번 주" 라고
    쓴 경우에만 기간이 걸린다는 뜻이라, 게이트의 취지(제멋대로 좁히지 못하게)가
    그대로 유지된다.
    """
    from lifetrainer import db, timeutil

    conn = db.open_db(ctx.cfg)
    today = timeutil.day_str(
        timeutil.now_ts(), ctx.cfg.tz, boundary_hour=ctx.cfg.rollup.day_boundary_hour
    )
    probe = " ".join(str(v) for v in args.values() if isinstance(v, str))
    return lt_tools.ToolContext(
        conn=conn, cfg=ctx.cfg, today=today, actor=ctx.actor, channel=ctx.channel, user_text=probe
    )


def build_tools() -> list[AgentTool]:
    """에이전트에게 내보낼 툴 전체. 순서가 곧 프롬프트 순서다."""
    return [*NEW_TOOLS, *(_reused_tool(name) for name in REUSED_TOOLS)]


def mcp_descriptors() -> list[dict]:
    """MCP `tools/list` 응답 모양."""
    return [
        {"name": t.name, "description": t.description, "inputSchema": t.schema}
        for t in build_tools()
    ]


# ── 예산 ──────────────────────────────────────────────────────────────


def budget_report() -> dict:
    """툴 스키마가 몇 토큰인지. 테스트와 `lt agent budget` 이 같은 값을 쓴다.

    직렬화한 JSON 을 재는 것은 근사다 — 실제로는 모델 템플릿이 다른 모양으로
    렌더링한다. `estimate_tokens` 가 **항상 실제보다 크게** 잡으므로
    (`llm/client.py`) 이 숫자를 상한으로 쓰는 것은 안전한 방향이다.
    """
    from lifetrainer.llm.client import estimate_tokens

    per_tool: dict[str, int] = {}
    for descriptor in mcp_descriptors():
        blob = json.dumps(descriptor, ensure_ascii=False)
        per_tool[descriptor["name"]] = int(round(estimate_tokens(blob)))
    return {
        "total": sum(per_tool.values()),
        "budget": TOOL_TOKEN_BUDGET,
        "count": len(per_tool),
        "per_tool": dict(sorted(per_tool.items(), key=lambda kv: -kv[1])),
    }


def assert_no_pattern() -> None:
    """스키마 어디에도 `pattern` 이 없는지 확인한다.

    `llm/client._check_no_pattern` 과 **같은 검사기를 쓴다.** 여기서 따로 구현하면
    두 경로의 판정이 갈린다. OpenClaw 의 `cron` 툴이 정확히 이것 하나로 죽어
    요청 전체가 400 이 됐다 (`openclaw-agent.md §4-5`).
    """
    from lifetrainer.llm.client import _check_no_pattern

    for descriptor in mcp_descriptors():
        _check_no_pattern(descriptor["inputSchema"], _path=f"$.{descriptor['name']}")
