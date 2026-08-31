"""경로 감옥 — 에이전트는 지정된 폴더 밖으로 나가지 못한다.

## 왜 이 파일이 있는가

OpenClaw 에는 이미 `tools.fs.workspaceOnly` 가 있다. 그것을 믿지 않는 이유는
두 가지다.

1. **경계가 우리 것이 아니다.** 프레임워크를 올리거나 설정 하나가 바뀌면 경계가
   같이 움직인다. 이 저장소에는 "설정이 조용히 되돌아가서 깨진" 기록이 이미 있다
   (`nvpower.sh` 가 부팅마다 심링크를 되돌린 건 · `daemon install` 이 유닛을
   다시 생성한 건).
2. **감옥은 두 겹이어야 한다.** MCP 툴은 게이트웨이의 fs 정책을 **거치지 않는다** —
   우리 프로세스가 직접 파일을 연다. 여기서 안 막으면 아무도 안 막는다.

## 무엇을 막는가

    ..  탈출        realpath 로 접은 뒤 root 아래인지 본다 (문자열 비교 금지)
    심링크 탈출      realpath 가 root 밖을 가리키면 거부. 부모 디렉터리도 같이 본다
    비밀 파일        읽기 허용 폴더 안에 있어도 이름으로 거부 (아래 참고)
    거대 파일        읽은 것이 다음 턴의 프롬프트가 된다 — 깊이가 곧 비용이다

★ **읽기 허용과 쓰기 허용을 나눈다.** `config/` 는 읽혀야 규칙을 설명할 수 있지만
쓰이면 분류 규칙이 조용히 바뀐다. 기본값은 "읽기는 넓게, 쓰기는 `data/agent/` 한 곳".

★ **`config/lifetrainer.toml` 은 읽기 허용 폴더 안에 있지만 거부한다.** Slack 봇
토큰과 Serper 키가 그 안에 있다. 폴더 단위 허용만으로는 이걸 못 막는다 —
경계를 폴더로만 그리면 "허용한 폴더 안의 비밀"이 통째로 샌다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 읽어서 프롬프트에 싣는 양의 상한. 32K 깊이에서 생성 속도가 70% 떨어지므로
# (`research/performance.md`) 파일 하나가 컨텍스트를 다 먹게 두면 안 된다.
# 4000자 ≈ 한국어 2,000토큰 정도로, `llm.max_input_tokens`(4000) 의 절반이다.
MAX_READ_BYTES = 4000

# 목록에 한 번에 실을 항목 수. 같은 이유로 자른다.
MAX_LIST_ENTRIES = 100

# 이름으로 거부하는 것들. **허용 폴더 안에 있어도 거부한다.**
# 폴더 단위 허용의 구멍을 메우는 목록이라, 폴더를 넓힐 때마다 여기를 같이 본다.
DENY_NAMES = frozenset(
    {
        "lifetrainer.toml",  # Slack 봇 토큰 · Serper 키
        "openclaw.json",  # 게이트웨이 토큰 · Slack 앱 토큰
        "websecret",  # 웹 세션 서명키
        "ingestsecret",  # 폰 수신 HMAC 키
        ".env",
        "id_rsa",
        "id_ed25519",
    }
)

# 확장자로 거부하는 것들. 백업본이 원본과 같은 비밀을 갖고 있다 —
# 이 저장소에는 실제로 `lifetrainer.toml.bak-before-ingest` 같은 파일이 있다.
DENY_SUFFIXES = ("secret", ".pem", ".key", ".sqlite", ".db", ".db-wal", ".db-shm")

# 이름이 이걸로 시작하면 거부 (백업본 규약).
DENY_STEMS = ("lifetrainer.toml", "openclaw.json")


class SandboxError(Exception):
    """경계 밖을 요구했다. 메시지가 그대로 모델에게 툴 결과로 간다."""


@dataclass(frozen=True)
class Sandbox:
    """에이전트가 움직일 수 있는 범위.

    `read_roots` 는 열어볼 수 있는 폴더, `write_roots` 는 쓸 수 있는 폴더다.
    **쓰기 폴더는 읽기 폴더의 부분집합이어야 한다** — 자기가 쓴 것을 못 읽으면
    모델이 같은 파일을 반복해서 다시 쓴다. `build` 가 자동으로 합쳐 준다.
    """

    read_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]
    # 상대 경로를 풀 때만 쓰는 기준점(보통 프로젝트 루트). **허가와는 무관하다** —
    # 여기서 푼 경로도 아래 containment 검사를 똑같이 통과해야 한다. 모델이
    # "docs/handbook.md" 라고 말했을 때 root 이름을 앞에 또 붙이지 않게 하려는 것뿐이다.
    base: Path | None = None

    # ── 생성 ──────────────────────────────────────────────────────────
    @classmethod
    def build(
        cls,
        read_roots: list[Path] | list[str],
        write_roots: list[Path] | list[str],
        *,
        base: Path | str | None = None,
    ) -> "Sandbox":
        """설정에서 온 경로 목록을 정규화한다. 없는 폴더는 조용히 버린다.

        없는 폴더를 버리는 이유: 설정에 오타가 있어도 에이전트는 **좁게** 돌아야지
        넓게 돌면 안 된다. 반대로 오타 때문에 전부 비면 `read_roots` 가 비고,
        그때는 모든 경로가 거부된다 — 안전한 방향으로 실패한다.
        """
        reads = _normalize(read_roots)
        writes = _normalize(write_roots)
        # 쓰기 폴더는 읽기에도 넣는다 (위 docstring 참고).
        merged = tuple(dict.fromkeys(reads + writes))
        folded_base = Path(os.path.realpath(os.path.expanduser(str(base)))) if base else None
        return cls(read_roots=merged, write_roots=writes, base=folded_base)

    # ── 판정 ──────────────────────────────────────────────────────────
    def resolve_read(self, raw: str) -> Path:
        """읽기용으로 경로를 해석한다. 허용 밖이면 `SandboxError`."""
        return self._resolve(raw, self.read_roots, "읽을")

    def resolve_write(self, raw: str) -> Path:
        """쓰기용으로 경로를 해석한다. 허용 밖이면 `SandboxError`.

        ★ **조용히 다른 곳에 쓰지 않는다.** 상대 경로는 쓰기 폴더 기준으로 풀리므로
        읽기 전용인 `docs/` 를 가리켜도 그냥 두면 `data/agent/docs/` 밑에
        **성공적으로** 써진다.
        모델은 `docs/` 에 썼다고 믿고, 사람은 한참 뒤에야 그림자 사본을 발견한다.
        읽기 전용 폴더를 가리킨 것이 분명하면(그 폴더가 실재하면) 거부한다 —
        이 저장소가 반복해서 배운 것: 조용히 성공하는 것이 실패보다 나쁘다.
        """
        candidate = Path(os.path.expanduser((raw or "").strip()))
        # 폴더를 **명시했을 때만** 본다. 맨 파일 이름(`memo.md`)에는 위치 의도가
        # 없으므로 쓰기 폴더로 보내는 것이 맞다 — 여기까지 거부하면 모델이
        # 메모 하나를 못 남긴다.
        named_a_folder = candidate.parent != Path(".")
        if named_a_folder and not candidate.is_absolute() and self.base is not None:
            meant = Path(os.path.realpath(self.base / candidate))
            if meant.parent.is_dir() and not self._inside(meant, self.write_roots):
                return _reject(
                    f"쓸 수 없는 경로입니다: {self._short(meant)}\n"
                    f"허용된 폴더: {', '.join(self._short(p) for p in self.write_roots) or '(없음)'}"
                )
        return self._resolve(raw, self.write_roots, "쓸")

    def _inside(self, path: Path, roots: tuple[Path, ...]) -> bool:
        return any(path == root or root in path.parents for root in roots)

    def hint(self) -> str:
        """못 찾았을 때 붙이는 한 줄.

        ★ 이 한 줄이 필요한 이유가 실측에 있다. "config 폴더 보여줘" 에 8B 가
        `/…/data/agent/config` 를 만들어 불렀다 — 프롬프트에서 본 허용 폴더
        (`data/agent`)에 `config` 를 **이어 붙인** 것이다. 같은 부류가
        `runtime/agent-gateway.md §4-4` 에 이미 기록돼 있다 (`workspace/workspace/`).
        절대 경로를 조립하지 말고 짧은 이름을 쓰라고 결과에서 가르친다 —
        거부만 하면 모델은 다음 턴에 또 조립한다.
        """
        names = ", ".join(self._short(p) for p in self.read_roots) or "(없음)"
        return f"경로는 짧은 이름으로 쓰세요 (예: {names}). 절대 경로를 조립하지 마세요."

    def describe(self) -> str:
        """모델에게 보여줄 범위 설명. 프롬프트에 실리므로 짧게 쓴다."""
        reads = ", ".join(self._short(p) for p in self.read_roots) or "(없음)"
        writes = ", ".join(self._short(p) for p in self.write_roots) or "(없음)"
        return f"읽기 가능: {reads}\n쓰기 가능: {writes}"

    # ── 내부 ──────────────────────────────────────────────────────────
    def _resolve(self, raw: str, roots: tuple[Path, ...], verb: str) -> Path:
        """감옥의 심장 — 경로 하나를 절대 경로로 접고 `roots` 안인지 판정한다."""
        if not raw or not raw.strip():
            return _reject("경로가 비었습니다.")
        if "\x00" in raw:
            return _reject("경로에 널 문자가 있습니다.")

        cleaned = os.path.expanduser(raw.strip())
        candidates = self._candidates(Path(cleaned), roots)

        # ★ 존재하는 것을 먼저 고른다. 상대 경로 하나가 여러 root 아래에서 성립할 수
        #   있는데, 8B 는 어느 root 인지 말해 주지 않는다 (`runtime/agent-gateway.md §4-4`
        #   의 `workspace/workspace/` 가 같은 부류의 실패다). 존재 여부로 고르면
        #   모델이 root 를 몰라도 맞는 파일에 닿는다. 아무것도 없으면 첫 후보로
        #   간다 — 새 파일 쓰기가 그 경로다.
        resolved: Path | None = None
        for candidate in candidates:
            folded = Path(os.path.realpath(candidate))
            if resolved is None:
                resolved = folded
            if folded.exists():
                resolved = folded
                break
        if resolved is None:
            return _reject("경로를 해석할 수 없습니다.")

        if _is_denied_name(resolved):
            return _reject(f"'{resolved.name}' 은(는) 비밀이 들어 있어 열 수 없습니다.")

        if self._inside(resolved, roots):
            return resolved

        # 허용 폴더를 base 기준 상대경로로 보여 준다. 절대경로를 그대로 실으면
        # 거부 한 번이 100토큰이 넘는데, 그 글자는 전부 다음 턴의 입력이 된다.
        allowed = ", ".join(self._short(p) for p in roots) or "(없음)"
        return _reject(f"{verb} 수 없는 경로입니다: {self._short(resolved)}\n허용된 폴더: {allowed}")

    def _short(self, path: Path) -> str:
        if self.base is not None:
            try:
                return str(path.relative_to(self.base))
            except ValueError:
                pass
        return str(path)

    def _candidates(self, candidate: Path, roots: tuple[Path, ...]) -> list[Path]:
        """해석해 볼 절대 경로 후보들. 절대 경로면 그것 하나뿐이다."""
        if candidate.is_absolute():
            return [candidate]
        bases = list(roots) or list(self.read_roots) or [Path("/")]
        if self.base is not None:
            bases.append(self.base)  # 맨 뒤 — 허용 폴더 기준 해석을 먼저 시도한다
        return [b / candidate for b in bases]


def _reject(message: str) -> Path:  # 반환형을 맞춰 호출부에서 return 으로 쓰게 한다
    raise SandboxError(message)


def _normalize(values: list[Path] | list[str]) -> tuple[Path, ...]:
    out: list[Path] = []
    for value in values:
        path = Path(os.path.realpath(os.path.expanduser(str(value))))
        if path.is_dir() and path not in out:
            out.append(path)
    return tuple(out)


def _is_denied_name(path: Path) -> bool:
    name = path.name
    lowered = name.lower()
    if lowered in DENY_NAMES:
        return True
    if any(lowered.endswith(suffix) for suffix in DENY_SUFFIXES):
        return True
    # `lifetrainer.toml.bak-before-ingest` 처럼 원본 이름으로 시작하는 백업본.
    return any(lowered.startswith(stem) and lowered != stem for stem in DENY_STEMS)


# ── 파일 조작 (감옥을 통과한 뒤에만 부른다) ─────────────────────────────


def read_text(sandbox: Sandbox, raw: str, *, max_bytes: int = MAX_READ_BYTES) -> str:
    """허용된 파일을 읽어 문자열로. 상한을 넘으면 자르고 **잘랐다고 말한다.**

    잘린 것을 말하지 않으면 모델이 뒷부분을 기억으로 채운다 — 이 저장소가
    이미 두 번 겪은 실패 유형이다 (`HISTORY/2026-08-18-search-fallback-noise.md`).
    """
    path = sandbox.resolve_read(raw)
    if not path.exists():
        raise SandboxError(f"파일이 없습니다: {sandbox._short(path)}\n{sandbox.hint()}")
    if path.is_dir():
        raise SandboxError(f"{sandbox._short(path)} 은(는) 폴더입니다. list_dir 을 쓰세요.")

    # ★ **통째로 읽지 않는다.** `read_bytes()` 는 자르기 전에 파일 전체를 메모리에
    #   올린다 — 상주 300MB 예산에서 감옥 안의 큰 파일 하나가 그걸 넘길 수 있다
    #   (`data/agent/` 는 에이전트가 쓰는 곳이라 커질 수 있다). 필요한 만큼 + 1바이트만
    #   읽어서 "더 있는지"까지 같이 안다.
    with path.open("rb") as fh:
        data = fh.read(max_bytes + 1)
    truncated = len(data) > max_bytes
    text = data[:max_bytes].decode("utf-8", errors="replace")
    if truncated:
        total = path.stat().st_size
        text += f"\n\n... (앞 {max_bytes}바이트만 실었습니다. 전체 {total}바이트)"
    return text


def list_dir(sandbox: Sandbox, raw: str, *, max_entries: int = MAX_LIST_ENTRIES) -> str:
    """허용된 폴더의 목록. 폴더는 `/` 를 붙이고 파일은 크기를 붙인다."""
    path = sandbox.resolve_read(raw)
    if not path.exists():
        raise SandboxError(f"폴더가 없습니다: {sandbox._short(path)}\n{sandbox.hint()}")
    if not path.is_dir():
        raise SandboxError(f"{sandbox._short(path)} 은(는) 파일입니다. read_file 을 쓰세요.")

    entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
    shown, hidden = entries[:max_entries], len(entries) - max_entries
    lines = []
    for entry in shown:
        if _is_denied_name(entry):
            continue  # 목록에서도 감춘다 — 이름만으로도 힌트가 된다
        try:
            if entry.is_dir():
                lines.append(f"{entry.name}/")
            else:
                lines.append(f"{entry.name}  ({entry.stat().st_size}B)")
        except OSError:
            # 끊긴 심링크·권한 없는 항목. 목록 하나 때문에 폴더 전체를 못 보면 안 된다.
            lines.append(f"{entry.name}  (읽을 수 없음)")
    if hidden > 0:
        lines.append(f"... 외 {hidden}개")
    return f"{path}:\n" + ("\n".join(lines) if lines else "(비어 있음)")


def write_text(sandbox: Sandbox, raw: str, content: str) -> str:
    """허용된 폴더에 파일을 쓴다. 부모 폴더는 만들되 **허용 밖이면 안 만든다.**"""
    path = sandbox.resolve_write(raw)
    if path.is_dir():
        raise SandboxError(f"{path} 은(는) 폴더입니다.")
    # 부모도 감옥 안인지 확인한 뒤에 만든다. resolve_write 가 이미 봤지만,
    # 부모가 심링크였다가 지금 막 바뀌는 경우를 위해 한 번 더 본다.
    sandbox.resolve_write(str(path.parent))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"{path} 에 {len(content.encode('utf-8'))}바이트를 썼습니다."
