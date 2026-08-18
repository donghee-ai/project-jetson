"""색상 로더 — `config/palette.yaml` 이 색의 유일한 원본이다 (계약서 §2).

웹 플래너(CSS 변수)와 플래너 PNG(matplotlib)가 전부 이 모듈 하나를 거쳐 색을
받는다. 두 곳에 색을 따로 적어두면 반드시 갈라지므로, 이 파일 밖에서는
색을 하드코딩하지 않는다.

이 팔레트는 dataviz 스킬 검증기에서 `adjacent` 만 통과했고 `all-pairs` 는
실패했다 (research↔ops, research↔entertainment 가 색각 이상 사용자에게 겹쳐
보인다). 그래서 이 모듈을 쓰는 쪽(`report/planner.py`, 웹)은 색만으로 카테고리
정체를 전달하면 안 된다 — 범례를 항상 띄우고, 3칸(30분) 이상 연속 블록에는
이름을 직접 적어야 한다. 이 규칙 자체는 palette.py 의 책임이 아니라 이 색을
쓰는 렌더러의 책임이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_VALID_THEMES = frozenset({"light", "dark"})


@dataclass(frozen=True)
class Palette:
    """`load_palette` 가 반환하는, 특정 테마(light|dark) 하나로 확정된 색 묶음."""

    theme: str
    surface: str
    ink: dict[str, str]  # primary/secondary/muted/gridline/baseline
    categories: dict[str, str]  # category_id -> hex. slot 오름차순 = 고정 순서
    labels: dict[str, str]  # category_id -> 한글 라벨
    structural: dict[str, str]  # away/off/unknown -> hex (활동이 아니라 색상 부호화 대상 아님)
    plan: dict[str, object]  # plan_overlay 값들 (stroke/stroke_alpha/wash_alpha/achieved/missed)
    order: list[str]  # 고정 슬롯 순서의 카테고리 id 목록


def _pick(node: dict[str, Any], theme: str) -> str:
    """`{light: ..., dark: ...}` 형태의 노드에서 테마 값을 꺼낸다."""
    return str(node[theme])


def load_palette(path: str | Path, theme: str = "light") -> Palette:
    """`config/palette.yaml` 을 읽어 지정된 테마 하나로 확정된 `Palette` 를 만든다."""
    if theme not in _VALID_THEMES:
        raise ValueError(f"알 수 없는 테마입니다: {theme!r} ('light'|'dark' 만 허용)")

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    surface = _pick(raw["surface"], theme)
    ink = {key: _pick(val, theme) for key, val in raw["ink"].items()}

    categories_raw = raw["categories"]
    # slot 필드 오름차순이 곧 "고정 순서" 다. yaml 삽입 순서에 의존하지 않는다
    # (팔레트 주석: "절대 순환 배정하지 않는다" — 순서 자체가 all-pairs 실패를
    # adjacent 로 완화하는 설계의 일부라서, 이 순서를 임의로 흩트리면 안 된다).
    ordered_ids = sorted(categories_raw, key=lambda cat_id: int(categories_raw[cat_id]["slot"]))
    categories = {cat_id: _pick(categories_raw[cat_id], theme) for cat_id in ordered_ids}
    labels = {cat_id: str(categories_raw[cat_id]["label"]) for cat_id in ordered_ids}

    structural = {key: _pick(val, theme) for key, val in raw["structural"].items()}

    plan: dict[str, object] = {}
    for key, val in raw["plan_overlay"].items():
        if isinstance(val, dict) and theme in val:
            plan[key] = _pick(val, theme)
        else:
            # stroke_alpha / wash_alpha 처럼 테마와 무관한 스칼라 값
            plan[key] = val

    return Palette(
        theme=theme,
        surface=surface,
        ink=ink,
        categories=categories,
        labels=labels,
        structural=structural,
        plan=plan,
        order=ordered_ids,
    )


def css_variables(pal: Palette) -> str:
    """웹이 그대로 `<style>` 에 박아 넣을 `:root { --k: v; }` 문자열을 만든다."""
    lines = [":root {"]
    lines.append(f"  --surface: {pal.surface};")
    for key, val in pal.ink.items():
        lines.append(f"  --ink-{key}: {val};")
    for cat_id, color in pal.categories.items():
        lines.append(f"  --cat-{cat_id}: {color};")
    for key, val in pal.structural.items():
        lines.append(f"  --structural-{key}: {val};")
    for key, val in pal.plan.items():
        lines.append(f"  --plan-{key}: {val};")
    lines.append("}")
    return "\n".join(lines)
