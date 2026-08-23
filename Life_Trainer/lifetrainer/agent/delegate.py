"""Slack 대화 한 턴을 OpenClaw 에이전트에게 넘긴다.

## 왜 이 파일이 생겼나

`llm/converse.py` 는 **규칙**(`llm/trigger.py`)이 툴을 고른다. 그 규칙이 못 잡는
요청이 있다 — 대표적으로 **쓰기**다. 대화 경로의 툴 10개에는 상태 변경·삭제·
메모·기록이 아예 없어서, "1번 완료 처리해줘" 를 물으면 8B 가 툴 없이
"완료했습니다" 라고 답한다. **에러가 안 난다** (`openclaw-agent.md §4-6`).

에이전트는 슬래시 10개를 툴로 갖고 있고 **모델이 직접 고른다.** 규칙을 하나 더
쓰지 않는다 — 자연어 DM 은 전부 여기로 온다.

## 대가를 숨기지 않는다

    슬래시 명령      밀리초        ← 바뀌지 않는다. Slack 슬래시는 다른 이벤트다
    자연어 DM        2~9초 → 약 35초 (실측 24~87초)

그 35초 중 **약 8초가 `openclaw` CLI(node) 기동**이다. 게이트웨이 WS 에 직접
붙으면 없앨 수 있지만 (`known-issues §7`) 지금은 CLI 를 부른다 — 검증된 경로이고
새 의존성이 없다.

## 세션을 게이트웨이에 맡긴다

`--session-key` 를 채널마다 고정하면 **히스토리를 OpenClaw 가 관리한다.**
`slackio/app._history` 를 쓰지 않는다 — 두 곳이 각자 기억하면 압축 시점이 갈려서
모델이 보는 맥락과 우리가 믿는 맥락이 달라진다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from lifetrainer.llm.interactive import interactive_turn

if TYPE_CHECKING:
    from lifetrainer.config import Config

logger = logging.getLogger(__name__)

# 게이트웨이가 뜬 뒤 한 턴의 상한. 실측 최악이 87초(RAG)라 넉넉히 잡되,
# 무한정 기다리게 두지 않는다 — Slack 에서 답이 영영 안 오는 것이 최악이다.
DEFAULT_TIMEOUT_SEC = 240.0

# 시스템 Node 를 먼저 찾게 한다. nvm 셸에서 서비스가 뜨면 게이트웨이와 다른
# 런타임을 잡는다 (`openclaw-agent.md §4-10`).
_PATH_PREFIX = "/usr/local/bin"

# 답변에서 지우는 것 — 프레임워크가 붙이는 첨부 지시자. 채팅에 그대로 나가면
# 사용자가 읽을 수 없는 줄이 된다.
_DIRECTIVE_RE = re.compile(r"^\s*(MEDIA:\S+|\[\[[a-z_]+(?::[^\]]+)?\]\])\s*$", re.M)


class DelegateError(Exception):
    """에이전트에게 넘기지 못했다. 호출부가 사용자에게 보일 문장을 만든다."""


@dataclass
class AgentReply:
    """에이전트 한 턴의 결과. 실패해도 `text` 는 항상 사람이 읽을 문장이다."""

    text: str
    ok: bool
    seconds: float
    tools_used: list[str] = field(default_factory=list)
    session_key: str = ""


def available() -> bool:
    """`openclaw` CLI 가 있나. 없으면 위임을 아예 시도하지 않는다."""
    return shutil.which("openclaw", path=f"{_PATH_PREFIX}:{os.environ.get('PATH', '')}") is not None


def session_key(agent_id: str, channel: str) -> str:
    """채널 하나 = 세션 하나. 대화 맥락이 채널 밖으로 새지 않는다."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", channel or "unknown")
    return f"agent:{agent_id}:slack-{safe}"


def ask(
    cfg: "Config",
    text: str,
    *,
    channel: str,
    agent_id: str = "lifetrainer",
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
) -> AgentReply:
    """자연어 한 턴을 에이전트에게 넘기고 답을 받는다.

    ★ **`interactive_turn` 안에서 돈다.** 에이전트도 같은 `llama-server` 를 쓰는데,
    게이트웨이를 거치므로 우리 GPU 락을 안 지난다. 이걸 안 잡으면 야간 배치가
    요약을 집어 든 뒤에 사용자 질문이 줄을 서서 35초가 55초가 된다
    (`llm/interactive.py` 의 그 문제 그대로다).
    """
    import time

    if not text.strip():
        raise DelegateError("빈 질문입니다.")
    if not available():
        raise DelegateError("openclaw CLI 를 찾지 못했습니다.")

    key = session_key(agent_id, channel)
    command = [
        "openclaw", "agent",
        "--agent", agent_id,
        "--session-key", key,
        "--message", text,
        "--json",
    ]
    env = {**os.environ, "PATH": f"{_PATH_PREFIX}:{os.environ.get('PATH', '')}"}

    started = time.monotonic()
    with interactive_turn(cfg):
        try:
            proc = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout_sec, env=env
            )
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started
            logger.warning("에이전트 턴이 %.0f초를 넘겨 중단했습니다 (channel=%s)", elapsed, channel)
            return AgentReply(
                text=f"에이전트가 {timeout_sec:.0f}초 안에 답하지 못했습니다. 짧게 다시 물어봐 주세요.",
                ok=False,
                seconds=elapsed,
                session_key=key,
            )
    elapsed = time.monotonic() - started

    return _parse(proc, elapsed, key, channel)


def _parse(proc: "subprocess.CompletedProcess[str]", elapsed: float, key: str, channel: str) -> AgentReply:
    """CLI 출력을 답으로. **어떤 실패에도 사람이 읽을 문장을 만든다.**"""
    try:
        payload = json.loads(proc.stdout)["result"]
    except Exception:
        logger.error(
            "에이전트 응답을 해석하지 못했습니다 (channel=%s, rc=%s): out=%.200s err=%.200s",
            channel, proc.returncode, proc.stdout, proc.stderr,
        )
        return AgentReply(
            text="에이전트 응답을 해석하지 못했습니다. 서버 로그를 확인해 주세요.",
            ok=False,
            seconds=elapsed,
            session_key=key,
        )

    meta = payload.get("meta") or {}
    summary = meta.get("toolSummary") or {}
    tools = [str(t).removeprefix("lt__") for t in (summary.get("tools") or [])]
    text = clean("".join(p.get("text", "") for p in payload.get("payloads") or []))

    if not text:
        # 실측으로 한 번 나왔다 — 툴 0건 · 본문 0자. 침묵이 오답보다 나쁘다.
        logger.warning("에이전트가 빈 답을 냈습니다 (channel=%s, 툴=%s)", channel, tools)
        return AgentReply(
            text="에이전트가 답을 만들지 못했습니다. 다시 물어봐 주세요.",
            ok=False,
            seconds=elapsed,
            tools_used=tools,
            session_key=key,
        )

    logger.info(
        "에이전트 위임 완료 (channel=%s, %.1f초, 툴 %s, 실패 %s)",
        channel, elapsed, tools or "없음", summary.get("failures", 0),
    )
    return AgentReply(text=text, ok=True, seconds=elapsed, tools_used=tools, session_key=key)


def clean(text: str) -> str:
    """채팅에 그대로 낼 수 없는 줄을 지운다.

    OpenClaw 는 `MEDIA:<경로>` · `[[reply_to_current]]` 같은 지시자를 본문에 섞는다
    (게이트웨이가 자기 채널로 보낼 때 떼어낸다). 우리는 본문만 받아 직접 올리므로
    **여기서 떼야 한다** — 안 그러면 사용자 화면에 `MEDIA:/home/…` 이 뜬다.
    """
    return _DIRECTIVE_RE.sub("", text or "").strip()
