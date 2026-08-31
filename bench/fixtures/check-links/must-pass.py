"""검사기가 **반드시 통과시켜야 하는** 예제 (파이썬).

오탐이 나오면 그 예제를 여기 **추가**한다. 헐거워지면 못 잡고, 빡빡해지면
사람이 검사기를 꺼 버린다 — 둘 다 검사가 없는 것과 같다 (CLAUDE.md §1).

살아 있는 참조 세 꼴:
  · 폴더가 붙은 것          `runtime/agent-gateway.md §4-5`
  · 맨 이름인데 저장소에 있는 것   `environment.md`
  · 닫는 백틱과 절 번호 사이가 벌어진 것  (`docs/contracts.md` §0)
"""
import os
from pathlib import Path

# ── 여기부터는 **문서 참조가 아니다.** 하나라도 잡히면 오탐이다 ──────────

# ① 남의 트리의 파일. OpenClaw 워크스페이스가 갖는 것이라 이 저장소에 없는 게 정상이다.
#    `AGENTS.md` · `SOUL.md` · `IDENTITY.md` · `BOOTSTRAP.md`
WORKSPACE_FILE = "AGENTS.md"
STUBS = ("SOUL.md", "IDENTITY.md", "USER.md", "TOOLS.md", "HEARTBEAT.md")

# ② 저장소 폴더 이름이 아닌 예시 경로 — 모델이 넘기는 값이지 이 저장소의 문서가 아니다.
#    `notes/x.md` 처럼 쓰면 문서 참조로 읽히지 않는다.
EXAMPLE_RELATIVE = "notes/x.md"


def _test_fixture_paths(tmp_path: Path) -> list[Path]:
    """코드 안의 문자열 리터럴. 테스트가 **만드는** 파일이지 가리키는 문서가 아니다."""
    return [
        tmp_path / "docs" / "big.md",
        tmp_path / "docs" / "nope.md",
        Path(os.path.join(str(tmp_path), "anything.md")),
    ]
