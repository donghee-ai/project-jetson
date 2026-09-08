"""lifetrainer.rollup.classify 테스트.

실제 config/rules.yaml 을 로드해 검증한다 (계약서 §4 대로 이 파일은 담당 B 소유).
네트워크 호출 없음.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from lifetrainer.rollup.classify import Classifier, Rule, normalize_fingerprint

RULES_PATH = Path(__file__).resolve().parent.parent / "config" / "rules.yaml"


@pytest.fixture()
def classifier() -> Classifier:
    return Classifier.from_yaml(RULES_PATH)


# ── rules.yaml 기본 구조 ─────────────────────────────────────────────────


def test_from_yaml_loads_reserved_categories(classifier: Classifier):
    # away/off/unknown 은 예약 카테고리라 categories 목록엔 있어야 하지만
    # 규칙에서 직접 지정되면 안 된다 (from_yaml 이 이미 검증했으므로 여기선 존재만 확인).
    for cat_id in ("away", "off", "unknown"):
        assert cat_id in classifier.categories

    # "off" 는 PyYAML(1.1) 이 bareword 를 bool 로 해석하는 흔한 함정이다.
    # 문자열로 정확히 남아있는지 확인한다.
    assert classifier.categories["off"].id == "off"
    assert isinstance(classifier.categories["off"].id, str)


def test_at_least_40_apps_covered():
    """rules.yaml 의 앱 정규식이 Windows/Linux 흔한 앱을 40개 이상 커버하는지."""
    doc = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    app_tokens: set[str] = set()
    for rule in doc["rules"]:
        app_pattern = rule.get("match", {}).get("app")
        if not app_pattern:
            continue
        # '(?i)^(a|b|c)$' 같은 알터네이션에서 토큰을 뽑아낸다.
        inner = app_pattern.split("(", 2)[-1].rsplit(")", 1)[0]
        for token in inner.split("|"):
            token = token.strip()
            if token:
                app_tokens.add(token)
    assert len(app_tokens) >= 40, f"앱 커버리지 부족: {len(app_tokens)}개"


def test_default_category_is_unknown(classifier: Classifier):
    assert classifier.default_category == "unknown"


def test_order_returns_stable_list(classifier: Classifier):
    order = classifier.order()
    assert order[0] == "coding"
    assert set(order) == set(classifier.categories.keys())


# ── classify() 우선순위 ──────────────────────────────────────────────────


def test_url_rule_wins_over_generic_browser_app_rule(classifier: Classifier):
    """chrome.exe + github.com url -> coding (browsing 이 아니라). 구체 규칙 우선순위 확인."""
    result = classifier.classify(app="chrome.exe", title="my/repo", url="https://github.com/foo/bar")
    assert result.category == "coding"
    assert result.source == "rule"


def test_generic_browser_app_falls_back_to_browsing(classifier: Classifier):
    result = classifier.classify(app="chrome.exe", title="아무 페이지", url="https://example.com")
    assert result.category == "browsing"


def test_title_rule_used_when_url_missing(classifier: Classifier):
    result = classifier.classify(app="chrome.exe", title="Rick Astley - Never Gonna Give You Up - YouTube")
    assert result.category == "entertainment"


def test_research_url_arxiv(classifier: Classifier):
    result = classifier.classify(app="firefox.exe", url="https://arxiv.org/abs/2401.00001")
    assert result.category == "research"


def test_case_insensitive_app_match(classifier: Classifier):
    lower = classifier.classify(app="code.exe", title="main.py")
    upper = classifier.classify(app="CODE.EXE", title="main.py")
    mixed = classifier.classify(app="Code.exe", title="main.py")
    assert lower.category == upper.category == mixed.category == "coding"


def test_linux_and_windows_equivalents_map_same_category(classifier: Classifier):
    assert classifier.classify(app="Slack.exe").category == "sns"
    assert classifier.classify(app="slack").category == "sns"
    assert classifier.classify(app="nvim").category == "coding"
    assert classifier.classify(app="Code.exe").category == "coding"


def test_unmatched_app_falls_back_to_default(classifier: Classifier):
    result = classifier.classify(app="SomeRandomTool.exe", title="???")
    assert result.category == "unknown"
    assert result.source == "default"
    assert result.rule_index is None


def test_no_fields_falls_back_to_default(classifier: Classifier):
    result = classifier.classify()
    assert result.category == "unknown"
    assert result.source == "default"


# ── color / label ────────────────────────────────────────────────────────


def test_color_and_label(classifier: Classifier):
    assert classifier.color("coding").startswith("#")
    assert classifier.label("coding") == "코딩"


# ── is_browser ───────────────────────────────────────────────────────────


def test_is_browser(classifier: Classifier):
    assert classifier.is_browser("chrome.exe")
    assert classifier.is_browser("Chrome.exe")
    assert classifier.is_browser("firefox")
    assert not classifier.is_browser("Code.exe")
    assert not classifier.is_browser(None)


# ── Rule.matches (AND 조건, 값 없으면 불일치) ───────────────────────────


def test_rule_matches_requires_all_specified_fields():
    import re

    rule = Rule(
        index=0,
        category="coding",
        subcategory=None,
        app=re.compile(r"(?i)^code\.exe$"),
        title=None,
        url=re.compile(r"(?i)github"),
    )
    assert rule.matches("code.exe", None, "https://github.com/x") is True
    assert rule.matches("code.exe", None, None) is False  # url 필요한데 없음
    assert rule.matches(None, None, "https://github.com/x") is False  # app 없음


# ── normalize_fingerprint ────────────────────────────────────────────────


def test_normalize_fingerprint_lowercases_and_collapses_whitespace():
    fp = normalize_fingerprint("Code.EXE", "  Main   Window  ")
    app_part, title_part = fp.split("\x1f")
    assert app_part == "code.exe"
    assert title_part == "main window"


def test_normalize_fingerprint_groups_paging_titles():
    fp1 = normalize_fingerprint("app.exe", "Document — 3 of 12")
    fp2 = normalize_fingerprint("app.exe", "Document — 7 of 12")
    assert fp1 == fp2


def test_normalize_fingerprint_groups_notification_counts():
    fp1 = normalize_fingerprint("Slack.exe", "general (3) - MyTeam - Slack")
    fp2 = normalize_fingerprint("Slack.exe", "general (17) - MyTeam - Slack")
    assert fp1 == fp2


def test_normalize_fingerprint_groups_paths():
    fp1 = normalize_fingerprint("Code.exe", "main.py - /home/user/proj1")
    fp2 = normalize_fingerprint("Code.exe", "main.py - /home/user/proj2")
    assert fp1 == fp2


def test_normalize_fingerprint_groups_guids():
    fp1 = normalize_fingerprint("app.exe", "Window 123e4567-e89b-12d3-a456-426614174000")
    fp2 = normalize_fingerprint("app.exe", "Window 999e4567-e89b-12d3-a456-426614174999")
    assert fp1 == fp2


def test_normalize_fingerprint_none_values():
    fp = normalize_fingerprint(None, None)
    assert fp == "\x1f"


# ── 게임 카테고리 (2026-08-19 신설) ───────────────────────────────────────
#
# 실제 미분류 목록에서 게임 실행 파일이 반복됐다. 공개본에는 개인별 순위·사용량을
# 싣지 않고, 대표적인 공개 실행 파일 이름만 회귀 픽스처로 둔다.


def test_unreal_shipping_suffix_is_gaming(classifier: Classifier):
    """언리얼 패키징 규약. 타이틀을 몰라도 잡혀야 한다 — 열거로는 못 따라간다."""
    result = classifier.classify(app="VALORANT-Win64-Shipping.exe", title="VALORANT")
    assert result.category == "gaming"
    assert result.subcategory == "client"
    other = classifier.classify(app="TslGame-Win64-Shipping.exe", title="PUBG")
    assert other.category == "gaming"


def test_known_game_titles_are_gaming(classifier: Classifier):
    for app in ("MapleStory.exe", "League of Legends.exe", "cs2.exe", "LostArk.exe"):
        assert classifier.classify(app=app, title=app).category == "gaming", app


def test_game_launchers_are_gaming_not_entertainment(classifier: Classifier):
    """런처는 게임 시간의 일부다 — 대기열·상점·패치 대기가 전부 여기서 일어난다."""
    for app in ("steam.exe", "Riot Client.exe", "NexonPlug.exe", "LeagueClientUx.exe"):
        result = classifier.classify(app=app, title=app)
        assert result.category == "gaming", app
        assert result.subcategory == "launcher"


def test_media_apps_stay_entertainment(classifier: Classifier):
    """게임을 떼어냈다고 여가가 비면 안 된다 — 영상·음악은 그대로 여가다."""
    for app in ("vlc.exe", "spotify.exe", "potplayer.exe"):
        assert classifier.classify(app=app, title=app).category == "entertainment", app


def test_unity_stays_coding(classifier: Classifier):
    """게임을 **만드는** 것은 코딩이다. 게임 규칙이 이걸 덮으면 안 된다."""
    result = classifier.classify(app="unity.exe", title="SampleScene - MyGame")
    assert result.category == "coding"
    assert result.subcategory == "gamedev"


def test_javaw_needs_title_to_be_gaming(classifier: Classifier):
    """javaw.exe 는 마인크래프트일 수도 개발용 JVM 일 수도 있다 — 제목으로 가른다."""
    game = classifier.classify(app="javaw.exe", title="Minecraft 1.20.4")
    assert game.category == "gaming"
    dev = classifier.classify(app="javaw.exe", title="MyProject - IntelliJ")
    assert dev.category != "gaming"


def test_game_stats_url_beats_video_rule(classifier: Classifier):
    """op.gg 를 보는 것은 영상 시청이 아니라 게임 활동의 연장이다."""
    result = classifier.classify(
        app="chrome.exe", title="OP.GG", url="https://op.gg/summoners/kr/example"
    )
    assert result.category == "gaming"
    assert result.subcategory == "stats"


def test_youtube_still_entertainment(classifier: Classifier):
    result = classifier.classify(
        app="chrome.exe", title="유튜브", url="https://youtube.com/watch?v=x"
    )
    assert result.category == "entertainment"


def test_gaming_is_a_declared_category(classifier: Classifier):
    assert "gaming" in classifier.categories
    assert classifier.label("gaming") == "게임"


# ── 색 단일 원본 (2026-08-25) ──────────────────────────────────────────────
#
# 색이 rules.yaml 과 palette.yaml 두 곳에 있었고 **서로 달랐다**.
# 웹은 palette 를, 일일/주간 타임라인 PNG 는 rules 를 읽어서 같은 카테고리가
# 화면마다 다른 색으로 나왔다. rules 쪽 값은 palette.yaml 머리말이
# "검증기 3항목 FAIL" 이라고 적어 둔 바로 그 색이었다.


def test_colors_come_from_the_palette_file(classifier: Classifier):
    """`Classifier.color` 는 palette.yaml 값을 그대로 돌려준다."""
    import yaml
    from pathlib import Path

    pal = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "config" / "palette.yaml").read_text("utf-8")
    )
    for cat_id, spec in pal["categories"].items():
        assert classifier.color(cat_id) == spec["light"]
        assert classifier.color(cat_id, "dark") == spec["dark"]


def test_rules_yaml_declares_no_colors():
    """★ 색을 rules.yaml 에 되살리면 두 원본이 다시 갈라진다."""
    import yaml
    from pathlib import Path

    doc = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "config" / "rules.yaml").read_text("utf-8")
    )
    offenders = [c["id"] for c in doc["categories"] if "color" in c]
    assert not offenders, f"rules.yaml 에 색이 다시 생겼습니다: {offenders} — palette.yaml 이 원본입니다"


def test_structural_states_have_a_color_too(classifier: Classifier):
    """away/off/unknown 은 categories 가 아니라 structural 에 있다 — 여기서도 나와야 한다."""
    for cat_id in ("away", "off", "unknown"):
        assert classifier.color(cat_id).startswith("#")
