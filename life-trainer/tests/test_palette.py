

# ── 팔레트 변주 (2026-08-25) ──────────────────────────────────────────────
#
# 기본 hue 를 유지한 채 명도·채도만 옮긴 파스텔·네온. hue 가 카테고리의 정체라서
# 테마를 바꿔도 "파랑은 코딩"이 유지돼야 한다.
# 색은 `scripts/derive_theme_palette.py` 가 OKLCH 격자 탐색으로 고른 것이다.


def _pal_path():
    """★ **CWD 가 아니라 `__file__` 기준이다** (issues/0017).

    저장소 루트에서 `pytest life-trainer/tests/` 를 돌리면 `"config/palette.yaml"` 이
    없다. 그런데 이 헬퍼가 이미 있는데도 절반은 문자열을 그대로 쓰고 있었다 —
    **고쳐 놓고 쓰지 않은 것**이라 새로 쓰는 사람은 옆줄을 보고 문자열을 따라 쓴다.
    아래 `test_어느_폴더에서_돌려도_같다` 가 그 재발을 막는다.
    """
    from pathlib import Path

    return Path(__file__).resolve().parent.parent / "config" / "palette.yaml"


def test_split_theme_separates_variant_from_mode():
    from lifetrainer.report.palette import split_theme

    assert split_theme("light") == ("base", "light")
    assert split_theme("dark") == ("base", "dark")
    assert split_theme("pastel-dark") == ("pastel", "dark")
    assert split_theme("neon-light") == ("neon", "light")


def test_variants_change_categories_but_not_the_frame():
    """표면·글자·구조 상태·계획 오버레이는 취향이 아니라 읽힘의 뼈대다."""
    from lifetrainer.report.palette import load_palette

    base = load_palette(_pal_path(), "light")
    pastel = load_palette(_pal_path(), "pastel-light")

    assert pastel.categories != base.categories
    assert pastel.surface == base.surface
    assert pastel.ink == base.ink
    assert pastel.structural == base.structural
    assert pastel.plan == base.plan
    # 순서와 라벨은 그대로여야 범례가 흔들리지 않는다.
    assert pastel.order == base.order
    assert pastel.labels == base.labels


def test_every_variant_covers_every_category():
    """변주에 빠진 카테고리가 있으면 색 하나만 튄다 — 조용히 넘어가면 안 된다."""
    from lifetrainer.report.palette import load_palette, variant_names

    base = load_palette(_pal_path(), "light")
    for variant in variant_names(_pal_path()):
        for mode in ("light", "dark"):
            theme = mode if variant == "base" else f"{variant}-{mode}"
            pal = load_palette(_pal_path(), theme)
            assert set(pal.categories) == set(base.categories), theme


def test_unknown_variant_is_refused():
    import pytest

    from lifetrainer.report.palette import load_palette

    with pytest.raises(ValueError):
        load_palette(_pal_path(), "sepia-light")


# ── UI 강조색은 팔레트 변주를 안 따른다 (2026-08-28) ─────────────────────
#
# `planner.css` 가 `--ui-accent: var(--cat-coding)` 이었다. 그래서 격자를 파스텔로
# 바꾸면 "실제 활동" 숫자와 "오늘의 계획" 강조까지 같이 옅어졌다(파스텔 coding 은
# #a7e2ff). 격자의 취향과 UI 의 읽힘은 다른 축이다.


def test_ui_강조색은_변주와_무관하게_한_값이다():
    import re

    from lifetrainer.web.app import _palette_css_blocks

    css = _palette_css_blocks(_pal_path())
    found = re.findall(r"--ui-accent-base:\s*([^;]+);", css)
    assert found, "서버가 --ui-accent-base 를 주입해야 한다"
    # 라이트 1개 + 다크 2개(미디어쿼리·강제) = 3개. 변주별로 늘어나면 안 된다.
    assert len(found) == 3, f"변주마다 생기면 안 된다: {found}"
    assert len(set(found)) == 2, f"라이트/다크 두 값이어야 한다: {found}"


def test_ui_강조색은_기본과_네온에서_온다():
    """라이트=기본, 다크=네온. 둘 다 palette.yaml 이 원본이고 하드코딩이 없다."""
    from lifetrainer.report.palette import load_palette
    from lifetrainer.web.app import _palette_css_blocks

    css = _palette_css_blocks(_pal_path())
    base_light = load_palette(_pal_path(), "light").categories["coding"]
    neon_dark = load_palette(_pal_path(), "neon-dark").categories["coding"]

    assert f"--ui-accent-base: {base_light};" in css
    assert f"--ui-accent-base: {neon_dark};" in css
    # 파스텔은 UI 강조색으로 절대 안 쓰인다 — 이게 사고의 원인이었다
    pastel_light = load_palette(_pal_path(), "pastel-light").categories["coding"]
    assert f"--ui-accent-base: {pastel_light};" not in css


def test_격자_카테고리색은_명암을_따른다_라이트는_기본_다크는_네온():
    """★ 2026-09-01 에 계약이 바뀌었다.

    전에는 이 테스트가 *"파스텔·네온 격자는 살아 있어야 한다"* 를 지켰다 —
    변주 고르개(기본·파스텔·네온)를 실수로 죽이지 않게 하려던 것이다.
    그 고르개를 **일부러 없앴다**: 취향을 고르게 두는 대신 명암 하나에 묶었다.
    그래서 지킬 것이 뒤집혔다 — 이제 `data-palette` 가 **없어야** 한다.

    기능을 지우면 그 기능을 지키던 테스트도 같이 뒤집는다. 지우기만 하면
    다음 사람이 고르개를 되살려도 아무도 안 막는다.
    """
    from lifetrainer.report.palette import load_palette
    from lifetrainer.web.app import _palette_css_blocks

    css = _palette_css_blocks(_pal_path())

    # 고르개가 없으니 변주 선택자도 없어야 한다.
    assert "data-palette" not in css

    # 라이트는 기본, 다크는 네온.
    base_light = load_palette(_pal_path(), "light").categories["coding"]
    neon_dark = load_palette(_pal_path(), "neon-dark").categories["coding"]
    assert f"--cat-coding: {base_light};" in css
    assert f"--cat-coding: {neon_dark};" in css

    # 기본 다크(`dark`)와 파스텔은 격자에 안 실린다 — 실리면 고르개가 돌아온 것이다.
    plain_dark = load_palette(_pal_path(), "dark").categories["coding"]
    pastel_light = load_palette(_pal_path(), "pastel-light").categories["coding"]
    assert f"--cat-coding: {plain_dark};" not in css
    assert f"--cat-coding: {pastel_light};" not in css


def test_어느_폴더에서_돌려도_같다():
    """★ `issues/0017` 의 재발 방지 (2026-09-07).

    이 파일의 테스트들이 설정을 **CWD 상대경로**로 읽고 있었다. 저장소 루트에서
    `pytest life-trainer/tests/` 를 돌리면 3건이 실패했는데, 실패 메시지가
    "파일이 없다" 가 아니라 색 비교 실패로 나와서 **테스트가 잘못된 것처럼 읽혔다.**

    운영 코드는 원래 안전했다 — `classify.py` 도 `planner.py` 도
    `cfg.root / "config" / ...` 로 절대경로를 만든다. **테스트만** 그랬다.

    ★ 왜 "루트에서 한 번 돌려 본다" 가 아니라 이 검사인가: 그건 사람이 기억해야 하고,
      검사는 안 그렇다 (저장소 규칙 §1). 여기서는 **소스에 상대경로 문자열이
      남아 있는지**를 본다 — 어느 폴더에서 돌리든 결과가 같다.
    """
    import re
    from pathlib import Path

    tests_dir = Path(__file__).resolve().parent
    offenders = []
    for path in sorted(tests_dir.glob("test_*.py")):
        for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            # 열거나 읽는 자리에 'config/…' 문자열이 그대로 있으면 CWD 에 기댄 것이다.
            if re.search(r"""(load_palette|open|read_text|Path)\(\s*["']config/""", line):
                offenders.append(f"{path.name}:{num}")
    assert not offenders, (
        "설정을 CWD 상대경로로 읽는 테스트: " + ", ".join(offenders)
        + " — `__file__` 기준으로 잡으세요 (이 파일의 _pal_path)"
    )
