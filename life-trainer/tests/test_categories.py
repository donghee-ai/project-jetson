"""카테고리 목록이 갈리지 않게 맞춰 본다 (2026-09-05) — 외래키 대신.

## 왜 외래키가 아닌가

카테고리의 **정본은 `config/rules.yaml`** 이고 색은 `config/palette.yaml` 이다.
DB 에 `category` 표를 만들어 FK 를 걸면 정본이 둘이 된다 — 이 저장소의 반복 실패 2번
("같은 값을 여러 곳에서 각자 계산했다") 을 스키마에 새기는 셈이다.

FK 가 막아 줄 것(오타·없는 카테고리)은 **파일끼리 맞춰 보는 것**으로 같은 값을 얻는다.

## 무엇을 막나 — 실제로 밟은 함정이다

`private` 은 2026-09-01 에 생겼는데 `rules.yaml` 의 예약 카테고리에 **안 들어갔다.**
`classifier.label()` 은 `self.categories[id]` 라 `KeyError` 로 죽고, PNG 리포트 범례가
그 날 나온 카테고리를 전부 라벨로 바꾼다 — **프라이빗이 있는 날의 일일 PNG 를 그렸으면
죽었을 것이다.** 09-05 에 `sleep` 을 넣다가 같은 자리에서 드러나 둘 다 넣었다.

증상이 "그 카테고리가 나온 날에만" 나타나므로 조용히 오래 살아남는 부류다.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from lifetrainer.config import load_config
from lifetrainer.report.palette import load_palette
from lifetrainer.rollup.classify import Classifier
from lifetrainer.rollup.rollup import CODE_ASSIGNED_CATEGORIES

_CONFIG = Path(__file__).resolve().parent.parent / "config"


def _declared() -> set[str]:
    raw = yaml.safe_load((_CONFIG / "rules.yaml").read_text(encoding="utf-8"))
    return {str(c["id"]) for c in raw["categories"]}


def test_코드가_붙이는_카테고리가_rules_에_선언돼_있다():
    """★ 이게 이 파일의 핵심. 빠지면 그 카테고리가 나온 날 PNG 리포트가 죽는다."""
    missing = set(CODE_ASSIGNED_CATEGORIES) - _declared()
    assert not missing, (
        f"코드가 붙이는데 rules.yaml 에 없는 카테고리: {sorted(missing)} — "
        "`classifier.label()` 이 KeyError 로 죽는다"
    )


def test_설정이_가리키는_예약_카테고리도_선언돼_있다():
    """`afk_category`·`no_data_category`·`default_category` 도 라벨이 필요하다."""
    cfg = load_config()
    needed = {cfg.rollup.afk_category, cfg.rollup.no_data_category}
    missing = needed - _declared()
    assert not missing, f"설정이 가리키는데 rules.yaml 에 없다: {sorted(missing)}"


def test_모든_카테고리에_라벨과_색이_있다():
    """라벨은 rules.yaml, 색은 palette.yaml — **두 파일이 같은 집합을 덮어야** 한다.

    한쪽에만 있으면 화면에서 그 칸이 색 없이(또는 id 그대로) 나온다.
    """
    classifier = Classifier.from_yaml(_CONFIG / "rules.yaml")
    pal = load_palette(_CONFIG / "palette.yaml", "light")
    known_colors = set(pal.categories) | set(pal.structural)

    for cat in _declared():
        assert classifier.label(cat), f"{cat} 에 라벨이 없다"
        assert cat in known_colors, f"{cat} 에 palette.yaml 색이 없다"


def test_색만_있고_선언이_없는_카테고리는_없다():
    """★ 반대 방향 — 헐거워지는 쪽.

    palette 에만 있고 rules 에 없으면 **쓸 수 없는 색**이고, 다음 사람이 그걸 보고
    "있는 카테고리" 로 오해한다.
    """
    pal = load_palette(_CONFIG / "palette.yaml", "light")
    orphan = (set(pal.categories) | set(pal.structural)) - _declared()
    assert not orphan, f"palette 에만 있는 카테고리: {sorted(orphan)}"
