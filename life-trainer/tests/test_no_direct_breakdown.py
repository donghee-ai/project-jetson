"""`slot_breakdown` 을 **아무나 직접 읽지 못하게** 막는다 (2026-09-05).

## 왜 검사인가 — 규칙으로는 세 번 샜다

`slot_breakdown` 은 실측 원본이라 **사람의 보정이 안 들어간다**
(`plan/override.py`: "실측 원본은 절대 건드리지 않는다"). 그 결정은 옳다.
문제는 **각 화면이 원본을 직접 SELECT 했다**는 것이다. 그러면 사람이 칠한 것이
그 화면에만 안 나타난다. 2026-09-05 실측:

    3시간을 '학습' 으로 보정하니
      격자 칸      learning ✅        오늘의 수면·커버리지  ✅ (slot.category 를 본다)
      차트·원그래프  학습 없음 ❌        상위 앱·기기별        ❌ (slot_breakdown 을 본다)

**수면만의 문제가 아니라 모든 카테고리에서 그랬다.** 화면마다 따로 고치면 다음에
붙는 화면이 또 틀린다 — 실제로 이번에 두 곳을 각각 고치다 이 검사를 만들기로 했다.

그래서 카테고리별 합계는 `report/stats.category_seconds` **하나**가 낸다.
새 코드가 원본을 직접 읽으면 여기서 걸린다.

★ 이 저장소는 같은 방법을 이미 쓴다 — `make check-docs`(문서가 지표를 옮겨 적는 것),
  `test_no_unused_exports.py`. *"규칙은 지키는 사람이 기억해야 하고, 검사는 안 그렇다."*

## 헐거워지지도, 빡빡해지지도 않게

허용 목록에 **왜 허용인지**를 같이 적는다. 이유를 못 쓰면 허용하지 않는다 —
목록이 늘기만 하면 검사가 없는 것과 같다 (저장소 규칙 §1).
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent / "lifetrainer"

# 파일 → 왜 직접 읽어도 되는가.
#
# ★ 새로 더하기 전에: 정말 **카테고리별 초**가 필요한 것이면 `category_seconds` 를
#   부르면 된다. 여기 더해야 할 만큼 다른 것을 재는가?
ALLOWED: dict[str, str] = {
    "report/stats.py": (
        "합계를 내는 당사자(`category_seconds`)와, 앱·기기 차원 집계. "
        "앱/기기는 보정이 그 정보를 안 갖고 있어 보정할 수가 없다 — "
        "보정은 '이 10분은 학습' 이지 '어느 앱이었다' 가 아니다."
    ),
    "rollup/rollup.py": "원본을 **쓰는** 쪽. 지우고 다시 쓴다.",
    "db.py": (
        "마이그레이션. 하루 경계가 바뀌면 파생 테이블을 비우고 다시 계산하게 만든다 — "
        "읽어서 보여주는 것이 아니라 **지우는** 쪽이다."
    ),
    "web/app.py": (
        "기기별 띠(`slot_breakdown × device`). 카테고리 합계가 아니라 "
        "**기기 차원**이라 보정과 무관하다."
    ),
    "plan/achieve.py": (
        "계획 달성률. **보정을 얹어서** 센다(`_actual_sec`) — 칸 범위가 계획 구간이라 "
        "`category_seconds`(하루 전체)를 그대로 못 쓴다. 규칙은 같다: 보정된 칸은 "
        "칸 길이 전부를 그 카테고리에 싣는다."
    ),
}


def _readers() -> set[str]:
    found = set()
    for path in _ROOT.rglob("*.py"):
        if re.search(r"FROM\s+slot_breakdown", path.read_text(encoding="utf-8")):
            found.add(str(path.relative_to(_ROOT)))
    return found


def test_slot_breakdown_을_직접_읽는_곳이_늘지_않았다():
    """★ 새 파일이 원본을 직접 읽으면 여기서 걸린다.

    걸렸다면 두 길 중 하나다:
      · 카테고리별 초가 필요한 것이면 `stats.category_seconds` 를 부른다 (거의 이 쪽)
      · 정말 다른 차원(앱·기기)을 재는 것이면 위 `ALLOWED` 에 **이유와 함께** 더한다
    """
    unexpected = _readers() - set(ALLOWED)
    assert not unexpected, (
        "slot_breakdown 을 직접 읽는 새 파일: "
        + ", ".join(sorted(unexpected))
        + " — 카테고리별 초라면 stats.category_seconds 를 쓰세요 (이 파일 머리말)"
    )


def test_허용_목록에_죽은_항목이_없다():
    """★ 안 울려야 하는 쪽의 반대 — **헐거워지는 것**도 막는다.

    파일이 사라졌거나 더 이상 원본을 안 읽는데 목록에 남아 있으면, 그 자리는 다음에
    아무 검사 없이 다시 열린다.
    """
    stale = set(ALLOWED) - _readers()
    assert not stale, f"이제 원본을 안 읽는데 목록에 남아 있다: {sorted(stale)}"


def test_허용_이유가_비어_있지_않다():
    """이유를 못 쓰면 허용하지 않는다. 목록이 늘기만 하면 검사가 없는 것과 같다."""
    for path, why in ALLOWED.items():
        assert len(why.strip()) > 20, f"{path} 의 허용 이유가 너무 짧다"
