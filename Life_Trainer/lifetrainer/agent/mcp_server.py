"""MCP stdio 서버 — OpenClaw 가 Life Trainer 의 능력을 부르는 통로.

## 왜 SDK 를 안 쓰는가

MCP 는 **줄 단위 JSON-RPC 2.0** 이다. 필요한 메서드가 `initialize` ·
`tools/list` · `tools/call` · `ping` 넷뿐이라 SDK 하나를 더 들이는 값이 안 나온다.
이 기기의 예산이 그 판단의 근거다:

    가용 메모리        약 4.5GB (llama-server 6.7 + 임베딩 1.8 + 게이트웨이 0.3 제외)
    파이썬 상주 규칙    300MB 이하 (`CLAUDE.md`)
    실측 상주          bare python 9.0MB → +config 12.4 → +llm.tools 13.2

의존성을 안 늘리면 이 프로세스는 **15MB 언저리에서 시작**한다. 무거운 것
(`slack_bolt` +27MB · `matplotlib` +44MB)은 전부 지연 import 라 안 쓰면 안 문다.

## stdout 은 전송로다

★ **stdout 에 아무것도 쓰면 안 된다.** print 한 줄, 로그 한 줄이 곧 프로토콜
파괴다. 이 모듈은 로깅을 stderr 로 못박고, 툴 몸통이 print 를 하더라도 프레임이
깨지지 않도록 **응답을 조립한 뒤 한 번에** 쓴다.

## 실패를 어떻게 다루는가

툴 실행 실패는 **JSON-RPC 에러가 아니라 툴 결과 텍스트**로 돌려준다
(`isError: true`). 프로토콜 에러로 올리면 게이트웨이가 턴을 끊어 버려서, 모델이
"뭐가 잘못됐는지"를 읽고 다시 시도할 기회가 사라진다. `llm/tools.run_tool` 이
같은 판단을 이미 하고 있다 — 두 경로가 같은 규칙을 쓴다.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, TextIO

from lifetrainer.agent import catalog as catalog_mod

logger = logging.getLogger(__name__)

SERVER_VERSION = "0.1.0"

# 우리가 말할 줄 아는 프로토콜 판본. 클라이언트가 이 중 하나를 부르면 그대로 받고,
# 모르는 것을 부르면 맨 앞 것으로 답한다 (MCP 의 협상 규약).
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

# JSON-RPC 표준 오류 코드.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INTERNAL_ERROR = -32603


class Server:
    """한 프로세스 = 한 세션. 툴 목록은 기동 시 한 번만 만든다."""

    def __init__(self, ctx: catalog_mod.AgentContext) -> None:
        self.ctx = ctx
        self._tools = {tool.name: tool for tool in catalog_mod.build_tools()}
        self._descriptors = catalog_mod.mcp_descriptors()

    # ── 디스패치 ──────────────────────────────────────────────────────
    def handle(self, request: dict) -> dict | None:
        """요청 하나를 처리한다. 알림(id 없음)이면 None 을 돌려준다."""
        method = request.get("method")
        req_id = request.get("id")
        is_notification = req_id is None

        if method == "initialize":
            result = self._initialize(request.get("params") or {})
        elif method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": self._descriptors}
        elif method == "tools/call":
            result = self._call(request.get("params") or {})
        elif method in {"resources/list", "prompts/list"}:
            # 우리는 이 기능을 광고하지 않지만, 그래도 묻는 클라이언트가 있다.
            # 에러 대신 빈 목록을 주는 편이 조용하다.
            result = {"resources": [], "prompts": []}
        else:
            if is_notification:
                return None
            return _error(req_id, _METHOD_NOT_FOUND, f"알 수 없는 메서드: {method}")

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _initialize(self, params: dict) -> dict:
        wanted = params.get("protocolVersion")
        version = wanted if wanted in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": catalog_mod.SERVER_NAME, "version": SERVER_VERSION},
        }

    def _call(self, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments")
        if not isinstance(args, dict):
            args = {}

        tool = self._tools.get(name)
        if tool is None:
            known = ", ".join(sorted(self._tools))
            return _tool_result(f"오류: '{name}' 이라는 툴은 없습니다. 있는 것: {known}", error=True)

        try:
            text = tool.handler(self.ctx, args)
        except Exception as exc:  # noqa: BLE001 - 툴 하나가 세션을 죽이면 안 된다
            logger.exception("툴 %r 실행 실패 (args=%r)", name, args)
            return _tool_result(f"오류: {type(exc).__name__}: {exc}", error=True)
        return _tool_result(text if isinstance(text, str) else json.dumps(text, ensure_ascii=False))


def _tool_result(text: str, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _error(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


# ── 루프 ──────────────────────────────────────────────────────────────


def serve(ctx: catalog_mod.AgentContext, *, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    """stdin 의 줄들을 읽어 stdout 으로 답한다. EOF 면 끝난다.

    테스트가 파일 객체를 주입할 수 있게 스트림을 인자로 받는다 — 실제 파이프를
    띄우지 않고도 프로토콜 전체를 돌려볼 수 있어야 한다.
    """
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    server = Server(ctx)

    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            _write(sink, _error(None, _PARSE_ERROR, f"JSON 파싱 실패: {exc}"))
            continue

        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            _write(sink, _error(request.get("id") if isinstance(request, dict) else None,
                                _INVALID_REQUEST, "jsonrpc 2.0 요청이 아닙니다."))
            continue

        try:
            response = server.handle(request)
        except Exception as exc:  # noqa: BLE001
            logger.exception("요청 처리 실패: %r", request)
            response = _error(request.get("id"), _INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

        if response is not None:
            _write(sink, response)


def _write(sink: TextIO, payload: dict) -> None:
    """한 줄 = 한 메시지. **조립한 뒤 한 번에 쓴다** (모듈 docstring 참고)."""
    sink.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sink.flush()


def main(argv: list[str] | None = None) -> int:
    """`lt-agent-mcp` 진입점. 게이트웨이가 stdio 로 이 프로세스를 띄운다."""
    import argparse

    from lifetrainer.agent.config import build_context

    parser = argparse.ArgumentParser(description="Life Trainer MCP 서버 (stdio)")
    parser.add_argument("--config", default=None, help="lifetrainer.toml 경로")
    args = parser.parse_args(argv)

    # ★ 로깅을 stderr 로 못박는다. basicConfig 의 기본 대상은 stderr 지만,
    #   어딘가에서 이미 stdout 핸들러가 붙었을 수 있어 명시한다.
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    ctx = build_context(args.config)
    logger.info("MCP 서버 시작 — 툴 %d개, %s", len(catalog_mod.build_tools()), ctx.sandbox.describe().replace("\n", " · "))
    serve(ctx)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
