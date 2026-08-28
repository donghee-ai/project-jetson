"""에이전트 설정 — **지정된 폴더가 어디인지**를 한 곳에서 정한다.

## 기본값의 근거

    읽기   docs/  HISTORY/  config/  data/agent/
    쓰기   data/agent/

읽기를 넓게 잡은 것은 에이전트가 답할 수 있어야 하기 때문이다 — "분류 규칙이
어떻게 돼 있어?" 에 답하려면 `config/rules.yaml` 을 봐야 한다. 쓰기를 한 곳으로
좁힌 것은 반대 이유다: 규칙 파일이 조용히 바뀌면 **롤업 결과 전체가 바뀌는데
아무도 모른다.** 소스 코드(`lifetrainer/`)와 테스트는 어느 쪽에도 없다 —
`runtime/agent-gateway.md §B` 의 결론(코딩은 이 모델에게 맡기지 않는다)이
그대로 경계가 된다.

★ `config/` 는 읽기에 있지만 `config/lifetrainer.toml` 은 못 읽는다.
폴더가 아니라 **이름으로** 막는다 (`sandbox.DENY_NAMES`).

## 설정으로 넓힐 수 있다

`[agent] read_roots` · `write_roots` 로 바꾼다. 경로는 프로젝트 루트 기준
상대경로이거나 절대경로다. **없는 폴더는 조용히 빠진다** — 오타가 경계를 넓히는
방향으로 실패하면 안 된다 (`sandbox.Sandbox.build`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lifetrainer.agent import sandbox as sandbox_mod

# 프로젝트 루트 기준 상대경로. 실제 존재하는 것만 감옥에 들어간다.
DEFAULT_READ_ROOTS: tuple[str, ...] = ("docs", "HISTORY", "config", "data/agent")
DEFAULT_WRITE_ROOTS: tuple[str, ...] = ("data/agent",)


@dataclass(frozen=True)
class AgentSettings:
    """`[agent]` 섹션. `Config` 에 얹히지 않고 따로 읽는다 — 이유는 아래.

    ★ `config.Config` 에 필드를 더하지 않는 이유: 그 dataclass 는 테스트 수십 개가
    직접 생성하고 있고(`Config(root=..., timezone=..., ...)`), 필드를 늘리면
    기본값이 있어도 **다른 담당이 같은 파일을 고치다 충돌한다** (`config.py` 의
    WebConfig 주석이 같은 사정을 적어 두었다). 에이전트는 부가 계층이므로
    자기 설정을 자기가 읽는다.
    """

    enabled: bool = True
    read_roots: tuple[str, ...] = DEFAULT_READ_ROOTS
    write_roots: tuple[str, ...] = DEFAULT_WRITE_ROOTS
    # ★ Slack 자연어 DM 을 에이전트에게 넘길지. **기본은 끈다.**
    #
    #   켜면 자연어 한 턴이 2~9초 → 약 35초가 된다 (슬래시 명령은 안 느려진다 —
    #   Slack 에서 다른 이벤트다). 설정 파일이 사라지거나 새 기기에 올릴 때
    #   **느린 쪽으로 조용히 넘어가면 안 되므로** 기본값이 False 다.
    #   되돌리기는 이 값 하나를 false 로 바꾸고 서비스 재기동.
    slack: bool = False
    slack_timeout_sec: float = 240.0
    # `openclaw` 실행 파일. 비우면 `delegate.resolve_bin` 이 찾는다.
    # nvm 을 갈아엎었는데 자동 탐색이 옛 버전을 물면 여기서 못 박는다.
    openclaw_bin: str = ""
    # OpenClaw 쪽 이름들. 문서와 스크립트가 같은 값을 봐야 해서 여기 둔다.
    agent_id: str = "lifetrainer"
    workspace: str = "data/agent/workspace"


def load_settings(cfg: Any) -> AgentSettings:
    """`config/lifetrainer.toml` 의 `[agent]` 를 읽는다. 없으면 기본값."""
    raw = _raw_section(cfg)
    return AgentSettings(
        enabled=bool(raw.get("enabled", True)),
        read_roots=tuple(raw.get("read_roots") or DEFAULT_READ_ROOTS),
        write_roots=tuple(raw.get("write_roots") or DEFAULT_WRITE_ROOTS),
        agent_id=str(raw.get("agent_id") or "lifetrainer"),
        workspace=str(raw.get("workspace") or "data/agent/workspace"),
        slack=bool(raw.get("slack", False)),
        slack_timeout_sec=float(raw.get("slack_timeout_sec") or 240.0),
        openclaw_bin=str(raw.get("openclaw_bin") or ""),
    )


def _raw_section(cfg: Any) -> dict:
    """toml 을 다시 읽어 `[agent]` 만 꺼낸다.

    `load_config` 가 이미 파싱한 결과를 안 들고 있어서(알려진 섹션만 dataclass 로
    옮긴다) 파일을 한 번 더 연다. 기동 시 한 번뿐이라 값이 안 나오는 비용이다.
    """
    import tomli

    path = Path(cfg.root) / "config" / "lifetrainer.toml"
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            return dict(tomli.load(fh).get("agent") or {})
    except Exception:  # noqa: BLE001 - 설정이 깨졌다고 에이전트가 못 뜨면 안 된다
        return {}


def build_sandbox(cfg: Any, settings: AgentSettings | None = None) -> sandbox_mod.Sandbox:
    """설정에서 감옥을 만든다. 쓰기 폴더는 없으면 만든다.

    쓰기 폴더를 만드는 이유: `Sandbox.build` 가 **없는 폴더를 버리므로**, 첫
    실행에서 `data/agent/` 가 없으면 쓰기가 통째로 막힌 채 조용히 시작된다.
    """
    settings = settings or load_settings(cfg)
    root = Path(cfg.root)

    writes = [_abs(root, p) for p in settings.write_roots]
    for path in writes:
        path.mkdir(parents=True, exist_ok=True)

    reads = [_abs(root, p) for p in settings.read_roots]
    return sandbox_mod.Sandbox.build(reads, writes, base=root)


def _abs(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def build_context(config_path: str | None = None) -> Any:
    """MCP 서버가 쓸 실행 환경 한 벌. 설정 로드 → 감옥 → 컨텍스트."""
    from lifetrainer.agent.catalog import AgentContext
    from lifetrainer.config import load_config

    cfg = load_config(config_path)
    settings = load_settings(cfg)
    return AgentContext(cfg=cfg, sandbox=build_sandbox(cfg, settings), actor="agent")
