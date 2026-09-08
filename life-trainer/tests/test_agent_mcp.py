"""`lifetrainer.agent` 의 카탈로그 · MCP 서버 · 워크스페이스 프롬프트.

이 파일이 지키려는 계약:

- **스키마에 `pattern` 이 없다.** 하나만 있어도 llama.cpp 가 요청 전체를 400 으로
  거부한다 (`operate/notes/agent-gateway.md §4-5`). OpenClaw 의 `cron` 툴이 정확히
  이걸로 죽었고, 그 사고 뒤에 만든 검사기를 **같이** 쓴다.
- **토큰 예산을 넘지 않는다.** 스키마 토큰은 그대로 첫 턴 지연이 된다
  (프롬프트 처리 295 tok/s 실측).
- **stdout 이 전송로다.** 한 줄 = 한 메시지. 툴이 실패해도 프레임이 안 깨진다.
- **프롬프트가 코드에서 파생된다.** 명령을 하나 더하면 프롬프트가 따라온다.
"""

from __future__ import annotations

import dataclasses
import io
import json

import pytest

from lifetrainer import db
from lifetrainer.agent import catalog as C
from lifetrainer.agent import mcp_server as M
from lifetrainer.agent import prompt as P
from lifetrainer.agent import sandbox as S
from lifetrainer.agent import slash as SL
from lifetrainer.config import load_config


@pytest.fixture()
def ctx(tmp_path):
    base = load_config()
    cfg = dataclasses.replace(
        base,
        db_path=tmp_path / "lt.db",
        data_dir=tmp_path,
        slack=dataclasses.replace(base.slack, bot_token="", default_channel=""),
    )
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    conn.close()
    (tmp_path / "work").mkdir()
    sandbox = S.Sandbox.build([tmp_path / "work"], [tmp_path / "work"], base=tmp_path)
    return C.AgentContext(cfg=cfg, sandbox=sandbox, actor="test")


# ── 카탈로그 ──────────────────────────────────────────────────────────


def test_no_regex_anywhere_in_the_schemas():
    """`cron` 툴 하나가 이것 때문에 요청 전체를 400 으로 만들었다."""
    C.assert_no_pattern()


def test_schemas_are_shared_with_the_conversation_path():
    """설명을 여기서 다시 쓰면 두 경로의 툴이 서로 다른 말을 하게 된다."""
    from lifetrainer.llm import tools as lt_tools

    exported = {d["name"]: d for d in C.mcp_descriptors()}
    for name in C.REUSED_TOOLS:
        assert exported[name]["description"] == lt_tools.REGISTRY[name].schema["function"]["description"]


def test_add_plan_is_not_exported_because_slash_covers_it():
    """같은 능력을 두 경로로 주면 스키마 토큰을 두 번 내고 모델이 헷갈린다."""
    names = {d["name"] for d in C.mcp_descriptors()}
    assert "add_plan" not in names
    assert "slash" in names


def test_tool_budget_holds():
    report = C.budget_report()
    assert report["total"] <= report["budget"], (
        f"툴 스키마가 {report['total']} 토큰 (예산 {report['budget']}). "
        f"큰 것부터: {list(report['per_tool'].items())[:3]}"
    )


def test_day_is_required_on_the_slash_tool():
    """★ 선택 인자로 두면 모델이 빠뜨리고, 계획이 **조용히 오늘에** 들어간다.

    실측: 선택일 때 "내일 09시에 딥워크 넣어줘" 에서 `day` 누락이 3/3 이었고,
    필수로 바꾼 뒤 0/3 이 됐다 (왕복도 하나 줄어 30.7 → 24.6초).
    프롬프트로도 툴 결과의 경고로도 안 잡혀서 스키마로 막았다.
    """
    slash_tool = next(d for d in C.mcp_descriptors() if d["name"] == "slash")
    assert set(slash_tool["inputSchema"]["required"]) == {"command", "day"}


def test_slash_tool_carries_the_command_table():
    """명령표가 프롬프트에 없으면 모델이 명령 이름을 지어낸다."""
    slash_tool = next(d for d in C.mcp_descriptors() if d["name"] == "slash")
    for name in SL.COMMANDS:
        assert name in slash_tool["description"]


# ── 프롬프트 ──────────────────────────────────────────────────────────


def test_prompt_is_derived_from_the_command_table(ctx):
    body = P.build_agents_md(ctx.sandbox)
    for name in SL.COMMANDS:
        assert name in body


def test_prompt_states_the_actual_jail(ctx):
    """프롬프트의 범위와 코드의 범위가 갈리면 모델이 매번 거부당한다."""
    body = P.build_agents_md(ctx.sandbox)
    assert ctx.sandbox.describe() in body


def test_prompt_budget_holds(ctx):
    from lifetrainer.llm.client import estimate_tokens

    tokens = estimate_tokens(P.build_agents_md(ctx.sandbox))
    assert tokens <= P.PROMPT_TOKEN_BUDGET, f"{tokens:.0f} 토큰 (예산 {P.PROMPT_TOKEN_BUDGET})"


def test_prompt_is_far_smaller_than_the_framework_default(ctx):
    """OpenClaw 기본 워크스페이스는 11,649바이트 = 7,199 토큰이었다 (실측)."""
    assert len(P.build_agents_md(ctx.sandbox).encode("utf-8")) < 11_649 // 2


# ── MCP 프로토콜 ──────────────────────────────────────────────────────


def _roundtrip(ctx, requests: list[dict]) -> list[dict]:
    stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
    stdout = io.StringIO()
    M.serve(ctx, stdin=stdin, stdout=stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


def test_initialize_echoes_a_supported_protocol(ctx):
    out = _roundtrip(ctx, [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2024-11-05"}}])
    assert out[0]["result"]["protocolVersion"] == "2024-11-05"
    assert out[0]["result"]["serverInfo"]["name"] == C.SERVER_NAME


def test_unknown_protocol_falls_back_to_ours(ctx):
    out = _roundtrip(ctx, [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "1999-01-01"}}])
    assert out[0]["result"]["protocolVersion"] == M.SUPPORTED_PROTOCOLS[0]


def test_notifications_get_no_reply(ctx):
    """알림에 답하면 클라이언트가 짝이 안 맞는 프레임을 받는다."""
    assert _roundtrip(ctx, [{"jsonrpc": "2.0", "method": "notifications/initialized"}]) == []


def test_tools_list_matches_the_catalog(ctx):
    out = _roundtrip(ctx, [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
    assert [t["name"] for t in out[0]["result"]["tools"]] == [d["name"] for d in C.mcp_descriptors()]


def test_tool_call_returns_text_content(ctx):
    out = _roundtrip(ctx, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "slash", "arguments": {"command": "/lt ping"}}}])
    result = out[0]["result"]
    assert result["isError"] is False
    assert "pong" in result["content"][0]["text"]


def test_failures_come_back_as_tool_text_not_rpc_errors(ctx):
    """프로토콜 에러로 올리면 게이트웨이가 턴을 끊어 모델이 다시 못 시도한다."""
    out = _roundtrip(ctx, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "nosuch", "arguments": {}}}])
    assert "error" not in out[0]
    assert out[0]["result"]["isError"] is True


def test_a_throwing_tool_does_not_kill_the_session(ctx, monkeypatch):
    def explode(_ctx, _args):
        raise RuntimeError("펑")

    monkeypatch.setitem(
        M.Server(ctx)._tools, "slash", dataclasses.replace(C.NEW_TOOLS[0], handler=explode)
    )
    server = M.Server(ctx)
    server._tools["slash"] = dataclasses.replace(C.NEW_TOOLS[0], handler=explode)
    first = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "slash", "arguments": {"command": "/lt ping"}}})
    assert first["result"]["isError"] is True
    assert "펑" in first["result"]["content"][0]["text"]
    # 세션은 계속 살아 있다
    assert server.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"] == {}


def test_bad_json_gets_a_parse_error_and_the_loop_continues(ctx):
    stdin = io.StringIO('not json\n{"jsonrpc": "2.0", "id": 2, "method": "ping"}\n')
    stdout = io.StringIO()
    M.serve(ctx, stdin=stdin, stdout=stdout)
    out = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert out[0]["error"]["code"] == -32700
    assert out[1]["result"] == {}


def test_unknown_method_is_an_rpc_error(ctx):
    out = _roundtrip(ctx, [{"jsonrpc": "2.0", "id": 1, "method": "nope/nope"}])
    assert out[0]["error"]["code"] == -32601


def test_every_line_is_one_json_object(ctx):
    """줄 단위 프레이밍이 깨지면 클라이언트가 전부 못 읽는다."""
    stdin = io.StringIO("".join(
        json.dumps({"jsonrpc": "2.0", "id": i, "method": "ping"}) + "\n" for i in range(5)
    ))
    stdout = io.StringIO()
    M.serve(ctx, stdin=stdin, stdout=stdout)
    lines = stdout.getvalue().splitlines()
    assert len(lines) == 5
    assert [json.loads(line)["id"] for line in lines] == list(range(5))


# ── 감옥이 MCP 를 통과해도 유지되는가 ───────────────────────────────────


@pytest.mark.parametrize("path", ["/etc/passwd", "../../../etc/hosts"])
def test_file_tools_stay_inside_the_jail_through_mcp(ctx, path):
    out = _roundtrip(ctx, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "read_file", "arguments": {"path": path}}}])
    assert "오류" in out[0]["result"]["content"][0]["text"]


def test_write_lands_in_the_write_root(ctx, tmp_path):
    _roundtrip(ctx, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "write_file", "arguments": {"path": "memo.md", "content": "적었다"}}}])
    assert (tmp_path / "work" / "memo.md").read_text(encoding="utf-8") == "적었다"
