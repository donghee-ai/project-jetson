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
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from lifetrainer.llm.interactive import interactive_turn

if TYPE_CHECKING:
    from lifetrainer.config import Config

logger = logging.getLogger(__name__)

# 게이트웨이가 뜬 뒤 한 턴의 상한. 실측 최악이 87초(RAG)라 넉넉히 잡되,
# 무한정 기다리게 두지 않는다 — Slack 에서 답이 영영 안 오는 것이 최악이다.
DEFAULT_TIMEOUT_SEC = 240.0

# ★ **`openclaw` 실행 파일을 어디서 찾는가** — 여기서 한 번 크게 틀렸다.
#
# 처음에는 PATH 앞에 `/usr/local/bin` 만 붙이고 `shutil.which` 로 찾았다.
# 내 터미널에서는 nvm PATH 가 잡혀 있어 통과했는데, **systemd 사용자 서비스의
# PATH 에는 nvm 경로가 없다.** 그래서 실사용에서 `available()` 이 False 가 되어
# 위임이 통째로 강등됐고 — 답이 빠르고 그럴듯해서 **아무도 눈치채지 못했다.**
# (로그에는 WARNING 이 찍혔지만 아무도 안 봤다.)
#
# `node` 는 `/usr/local/bin` 에 있는데(§4-10 에서 그렇게 옮겼다) **`openclaw` CLI
# 래퍼는 nvm 에만 있다.** 둘을 같이 옮긴 적이 없었다.
#
# 그래서 순서대로 뒤진다. 설정으로 못 박을 수도 있다.
_SEARCH_PREFIXES = ("/usr/local/bin", "/usr/bin")

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


def resolve_bin(explicit: str = "") -> str:
    """`openclaw` 실행 파일의 절대 경로. 못 찾으면 빈 문자열.

    ★ **PATH 에 기대지 않는다.** systemd 사용자 서비스의 PATH 에는 nvm 경로가
    없어서, 개발 셸에서 되던 것이 서비스에서 조용히 안 됐다 (위 상수 주석).
    찾은 경로를 그대로 `subprocess` 에 넘긴다.
    """
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else ""

    for prefix in _SEARCH_PREFIXES:
        candidate = os.path.join(prefix, "openclaw")
        if os.access(candidate, os.X_OK):
            return candidate

    found = shutil.which("openclaw")
    if found:
        return found

    # nvm 설치본. 여러 버전이 있으면 최신을 고른다 — 게이트웨이가 쓰는 것과
    # 다를 수 있지만, 없는 것보다 낫고 `lt doctor` 가 무엇을 쓰는지 보여준다.
    import glob

    nvm = sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/openclaw")))
    return nvm[-1] if nvm else ""


def available(explicit: str = "") -> bool:
    """`openclaw` CLI 가 있나. 없으면 위임을 아예 시도하지 않는다."""
    return bool(resolve_bin(explicit))


# ★ 컨텍스트가 넘쳤을 때 게이트웨이가 돌려주는 문장. 이걸 만나면 세션을 갈고
#   한 번 다시 시도한다 (아래 `ask` 참고).
_OVERFLOW_MARK = "Context overflow"

# 세션을 몇 턴마다 새로 여는가.
#
# ★ 압축 설정만으로는 못 막는다. 실측: ctx 20,480 에 시스템 프롬프트가 5,326 이라
#   `keepRecentTokens 8000` + `reserveTokens 6000` 이면 여유가 1,154 토큰뿐이었고,
#   한 시간쯤 대화하자 **"Context overflow: prompt too large"** 로 죽었다.
#   설정을 3000/3000 으로 낮춰 여유를 9,154 로 늘렸지만, 그것도 **한계를 미룰 뿐**
#   히스토리가 계속 자라면 언젠가 같은 자리에 닿는다.
#
#   이 저장소가 반복해서 배운 것 그대로다 — 설정으로 미루지 말고 구조로 막는다.
#   턴 수로 끊으면 히스토리 길이에 상한이 생긴다.
MAX_TURNS_PER_SESSION = 24

# 세션이 이만큼 조용했으면 새로 연다. 어제 대화가 오늘 답에 섞이지 않게 한다 —
# 실측으로, 지운 계획이 다음 날 답변에 "완료"로 나타난 적이 있다.
SESSION_IDLE_SEC = 1800.0

# 채널별 세션 상태. `{channel: (suffix, turns, last_ts)}`
_sessions: dict[str, tuple[int, int, float]] = {}
_sessions_lock = threading.Lock()


def session_key(agent_id: str, channel: str, suffix: int = 0) -> str:
    """채널 하나 = 세션 하나. 대화 맥락이 채널 밖으로 새지 않는다.

    `suffix` 는 같은 채널 안에서 세션을 갈아 끼울 때 쓴다 (턴 상한·유휴·오버플로).
    """
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", channel or "unknown")
    tail = f"-{suffix}" if suffix else ""
    return f"agent:{agent_id}:slack-{safe}{tail}"


def _next_key(agent_id: str, channel: str, *, rotate: bool = False) -> str:
    """이번 턴에 쓸 세션 키. 필요하면 세션을 갈아 끼운다."""
    now = time.monotonic()
    with _sessions_lock:
        suffix, turns, last = _sessions.get(channel, (0, 0, now))
        stale = (now - last) > SESSION_IDLE_SEC
        if rotate or stale or turns >= MAX_TURNS_PER_SESSION:
            suffix += 1
            turns = 0
            if rotate:
                logger.info("컨텍스트가 넘쳐 세션을 갈아 끼웁니다 (channel=%s → #%d)", channel, suffix)
            elif stale:
                logger.info("%.0f분 조용해서 새 세션 (channel=%s → #%d)", SESSION_IDLE_SEC / 60, channel, suffix)
            else:
                logger.info("%d턴을 채워 새 세션 (channel=%s → #%d)", MAX_TURNS_PER_SESSION, channel, suffix)
        _sessions[channel] = (suffix, turns + 1, now)
        return session_key(agent_id, channel, suffix)


def ask(
    cfg: "Config",
    text: str,
    *,
    channel: str,
    agent_id: str = "lifetrainer",
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    openclaw_bin: str = "",
) -> AgentReply:
    """자연어 한 턴을 에이전트에게 넘기고 답을 받는다.

    ★ 컨텍스트가 넘치면 **세션을 갈고 한 번 다시 시도한다.** 사용자에게
    "Context overflow: prompt too large…" 라는 영어 오류를 그대로 보여 주는 것은
    답이 아니다 — 그 문장은 우리가 고쳐야 할 설정 이야기지 사용자가 할 일이 아니다.

    ★ **`interactive_turn` 안에서 돈다.** 에이전트도 같은 `llama-server` 를 쓰는데,
    게이트웨이를 거치므로 우리 GPU 락을 안 지난다. 이걸 안 잡으면 야간 배치가
    요약을 집어 든 뒤에 사용자 질문이 줄을 서서 35초가 55초가 된다
    (`llm/interactive.py` 의 그 문제 그대로다).
    """
    if not text.strip():
        raise DelegateError("빈 질문입니다.")
    binary = resolve_bin(openclaw_bin)
    if not binary:
        raise DelegateError("openclaw CLI 를 찾지 못했습니다.")

    reply = _run(cfg, binary, agent_id, channel, text, timeout_sec, rotate=False)
    if reply.ok or _OVERFLOW_MARK not in reply.text:
        return reply

    # 넘쳤다 — 세션을 갈고 한 번만 더. 두 번은 안 한다(사용자가 1분을 넘게 기다린다).
    logger.warning("컨텍스트 초과로 세션을 갈고 재시도합니다 (channel=%s)", channel)
    return _run(cfg, binary, agent_id, channel, text, timeout_sec, rotate=True)


def _run(
    cfg: "Config",
    binary: str,
    agent_id: str,
    channel: str,
    text: str,
    timeout_sec: float,
    *,
    rotate: bool,
) -> AgentReply:
    """실제 한 번의 호출."""
    key = _next_key(agent_id, channel, rotate=rotate)
    command = [
        binary, "agent",
        "--agent", agent_id,
        "--session-key", key,
        "--message", text,
        "--json",
    ]
    # 자식이 부를 `node` 도 시스템 것을 먼저 보게 한다 (§4-10).
    env = {**os.environ, "PATH": ":".join((*_SEARCH_PREFIXES, os.environ.get("PATH", "")))}

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

    reply = _parse(proc, elapsed, key, channel)
    # 게이트웨이는 오버플로를 **정상 응답 본문**으로 준다 (rc=0). 여기서 잡아낸다.
    if reply.ok and _OVERFLOW_MARK in reply.text:
        return AgentReply(text=reply.text, ok=False, seconds=reply.seconds, session_key=key)
    return reply


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
