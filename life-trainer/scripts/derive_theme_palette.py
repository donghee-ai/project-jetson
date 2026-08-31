"""팔레트 변주(파스텔·네온)를 **계산으로** 만들고 분리도를 잰다.

이 저장소의 규칙: "색을 더할 때는 검증기로 계산한다 — 눈대중 금지"
(`config/palette.yaml` 머리말). 9번째 색을 OKLCH 전수 탐색으로 고른 것과 같은
자리다. 여기서는 **기존 색의 hue 를 유지한 채** 명도·채도만 옮긴다 —
hue 가 곧 카테고리의 정체라서, 테마를 바꿔도 "파랑은 코딩"이 유지돼야 한다.

    파스텔  L 을 올리고 C 를 낮춘다  (연하게)
    네온    C 를 올리고 L 을 조인다  (쨍하게)

재현:  .venv/bin/python scripts/derive_theme_palette.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


# ── sRGB ↔ OKLab/OKLCH ────────────────────────────────────────────────────
def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def hex_to_oklab(hex_str: str) -> tuple[float, float, float]:
    h = hex_str.lstrip("#")
    r, g, b = (_srgb_to_linear(int(h[i : i + 2], 16) / 255) for i in (0, 2, 4))
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = l ** (1 / 3), m ** (1 / 3), s ** (1 / 3)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def oklab_to_hex(L: float, a: float, b: float) -> str:
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_**3, m_**3, s_**3
    rgb = (
        +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )
    out = []
    for v in rgb:
        out.append(max(0, min(255, round(_linear_to_srgb(max(0.0, min(1.0, v))) * 255))))
    return "#%02x%02x%02x" % tuple(out)


def to_lch(hex_str: str) -> tuple[float, float, float]:
    L, a, b = hex_to_oklab(hex_str)
    return L, math.hypot(a, b), math.atan2(b, a)


def from_lch(L: float, C: float, h: float) -> str:
    return oklab_to_hex(L, C * math.cos(h), C * math.sin(h))


def dE(x: str, y: str) -> float:
    """OKLab ΔE ×100 — palette.yaml 주석이 쓰는 것과 같은 척도."""
    a, b = hex_to_oklab(x), hex_to_oklab(y)
    return 100 * math.dist(a, b)


# ── 변주 정의 ─────────────────────────────────────────────────────────────
#
# 처음엔 "파스텔 = L 을 0.86 으로 올리고 C 를 0.42 배" 처럼 한 점을 눈대중으로
# 잡았다. 재 보니 adjacent 최소가 4.7 이었다 — 기본 팔레트가 19.6 인데.
# 모든 색을 같은 명도로 올리면 **명도 축이 사라져** hue 차이만 남고, 채도까지
# 낮추면 그 hue 차이도 눌린다. palette.yaml 이 폰 파스텔에서 이미 겪은 일이다.
#
# 그래서 격자 탐색을 한다. 자유도 셋:
#   L      기준 명도
#   Cmul   채도 배율
#   Lalt   슬롯 번호 홀짝으로 명도를 ±만큼 벌린다 (명도 축을 되살린다)
# 목표: adjacent 최소를 최대화. 제약: 표면 대비 >= 12.
GATE_ADJACENT = 9.0
GATE_SURFACE = 12.0
# all-pairs 도 게이트에 넣는다. 안 넣으면 "가장 연한 파스텔"이 이기는데 그때
# 최악 쌍이 1.4 까지 떨어진다 — 기본 팔레트도 7.1 이라 이미 2차 인코딩(라벨·범례)에
# 기대고 있지만, 1.4 는 라벨이 없는 1~2칸 블록에서 실제로 같은 색으로 읽힌다.
GATE_ALLPAIRS = 2.5   # 4.0 을 걸었더니 파스텔은 **어떤 조합도 통과하지 못했다**.
# hue 를 유지한 채 채도를 낮추면 최악 쌍이 함께 눌리는 것이 원리적이다. 2.5 는
# 파스텔이 도달 가능한 선이고, 그만큼 이 테마에서는 라벨·범례 의존도가 더 크다.

_GRIDS = {
    "pastel": {
        "L": [0.74, 0.78, 0.82, 0.86],
        "Cmul": [0.45, 0.55, 0.65, 0.75, 0.85],
        "Lalt": [0.0, 0.04, 0.07, 0.10],
    },
    "neon": {
        "L": [0.62, 0.66, 0.70, 0.74, 0.78],
        "Cmul": [1.15, 1.3, 1.45, 1.6],
        "Lalt": [0.0, 0.03, 0.06],
    },
}


def _apply(base_hex: str, L0: float, Cmul: float, Lalt: float, slot: int) -> str:
    """hue 는 그대로. 명도는 목표값(+슬롯 홀짝 오프셋), 채도는 배율."""
    _L, C, h = to_lch(base_hex)
    L = min(0.97, max(0.10, L0 + (Lalt if slot % 2 else -Lalt)))
    return from_lch(L, min(C * Cmul, 0.40), h)


def search(cats: dict, ids: list[str], mode: str, recipe: str, surface: str):
    """격자를 다 돌려 adjacent 최소가 가장 큰 조합을 고른다. 결정적이다."""
    best = None
    g = _GRIDS[recipe]
    for L0 in g["L"]:
        for Cmul in g["Cmul"]:
            for Lalt in g["Lalt"]:
                cols = {
                    c: _apply(cats[c][mode], L0, Cmul, Lalt, int(cats[c]["slot"])) for c in ids
                }
                adj = min(dE(cols[ids[i]], cols[ids[i + 1]]) for i in range(len(ids) - 1))
                surf = min(dE(cols[c], surface) for c in ids)
                if surf < GATE_SURFACE:
                    continue
                if adj < GATE_ADJACENT:
                    continue
                allp = min(dE(cols[a], cols[b]) for i, a in enumerate(ids) for b in ids[i + 1 :])
                if allp < GATE_ALLPAIRS:
                    continue
                # ★ 게이트를 통과한 것들 중에서는 **테마의 의도**로 고른다.
                #   분리도만 최대화하면 파스텔이 파스텔처럼 안 보인다(채도 0.85배가
                #   이긴다). 통과선을 넘겼으면 그 다음은 "얼마나 연한가/진한가" 다.
                intent = -Cmul if recipe == "pastel" else Cmul
                score = (round(intent, 3), round(adj, 3), round(allp, 3))
                if best is None or score > best[0]:
                    best = (score, (round(adj, 3), round(allp, 3)), cols, (L0, Cmul, Lalt), surf)
    return None if best is None else best[1:]



def report(name: str, colors: dict[str, str], surface: str) -> bool:
    ids = list(colors)
    adj = min(dE(colors[ids[i]], colors[ids[i + 1]]) for i in range(len(ids) - 1))
    allp = min(dE(colors[a], colors[b]) for i, a in enumerate(ids) for b in ids[i + 1 :])
    surf = min(dE(colors[c], surface) for c in ids)
    ok = adj >= 9.0 and surf >= 12.0
    print(f"  {name:14s} adjacent 최소 {adj:5.1f} · all-pairs 최소 {allp:5.1f} · 표면 대비 최소 {surf:5.1f}  {'PASS' if ok else 'WARN'}")
    return ok


def main() -> int:
    raw = yaml.safe_load((ROOT / "config" / "palette.yaml").read_text("utf-8"))
    cats = raw["categories"]
    ids = sorted(cats, key=lambda k: int(cats[k]["slot"]))

    print("=== 기본(현재) — 비교 기준 ===")
    for mode in ("light", "dark"):
        report(mode, {c: cats[c][mode] for c in ids}, raw["surface"][mode])

    out: dict[str, dict[str, dict[str, str]]] = {}
    for recipe in _GRIDS:
        out[recipe] = {}
        print(f"\n=== {recipe} ===")
        for mode in ("light", "dark"):
            best = search(cats, ids, mode, recipe, raw["surface"][mode])
            if best is None:
                print(f"  {mode}: 제약을 만족하는 조합이 없다")
                return 1
            (adj, allp), cols, params, surf = best
            out[recipe][mode] = cols
            flag = "PASS" if adj >= GATE_ADJACENT else "WARN"
            print(
                f"  {mode:5s} L={params[0]:.2f} Cmul={params[1]:.2f} Lalt={params[2]:.2f}"
                f" → adjacent {adj:5.1f} · all-pairs {allp:5.1f} · 표면 {surf:5.1f}  {flag}"
            )
        for c in ids:
            print(f"    {c:14s} light {out[recipe]['light'][c]}  dark {out[recipe]['dark'][c]}")

    (ROOT / "config" / "palette-themes.generated.yaml").write_text(
        yaml.safe_dump({"themes": out}, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    print("\nconfig/palette-themes.generated.yaml 에 썼다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
