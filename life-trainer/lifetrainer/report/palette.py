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


def split_theme(theme: str) -> tuple[str, str]:
    """`"pastel-dark"` → `("pastel", "dark")`. 변주가 없으면 `("base", ...)`.

    테마 이름이 **변주 × 명암** 두 축이다. 문자열 하나로 다니는 이유는 이 값이
    쿼리 파라미터·localStorage·PNG 렌더 인자로 그대로 오가기 때문이다 —
    두 값으로 쪼개면 넘기는 자리마다 짝을 맞춰야 하고 한쪽만 바뀌는 사고가 난다.
    """
    if "-" in theme:
        variant, _, mode = theme.partition("-")
        return variant, mode
    return "base", theme


def variant_names(path: str | Path) -> list[str]:
    """고를 수 있는 변주 목록. `base` 가 항상 먼저다."""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return ["base", *sorted(raw.get("themes") or {})]


def load_palette(path: str | Path, theme: str = "light") -> Palette:
    """`config/palette.yaml` 을 읽어 지정된 테마 하나로 확정된 `Palette` 를 만든다.

    `theme` 는 `"light"`/`"dark"` 또는 `"<변주>-<명암>"`(예: `"pastel-dark"`).
    변주는 **카테고리 색만** 바꾼다 — 표면·글자·구조 상태·계획 오버레이는 기본을
    그대로 쓴다. 그것들은 취향이 아니라 읽힘의 뼈대라서 테마마다 다르면 안 된다.
    """
    variant, mode = split_theme(theme)
    if mode not in _VALID_THEMES:
        raise ValueError(f"알 수 없는 명암입니다: {mode!r} ('light'|'dark' 만 허용)")
    theme = mode

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
    if variant != "base":
        overrides = ((raw.get("themes") or {}).get(variant) or {}).get(theme)
        if overrides is None:
            raise ValueError(f"알 수 없는 팔레트 변주입니다: {variant!r}")
        # 변주에 없는 카테고리는 기본색을 그대로 쓴다 — 카테고리를 하나 더할 때
        # 변주 세 곳을 같이 안 고쳐도 화면이 깨지지 않는다(색이 하나 튈 뿐이다).
        categories = {cat_id: overrides.get(cat_id, categories[cat_id]) for cat_id in ordered_ids}
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
        theme=f"{variant}-{theme}" if variant != "base" else theme,
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
