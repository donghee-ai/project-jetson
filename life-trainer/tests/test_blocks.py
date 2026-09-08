"""lifetrainer.slackio.blocks 테스트. 네트워크 없음, 순수 함수만 검증한다.

`report.stats`/`rollup.classify` 를 실제로 import 하지 않는다 (blocks.py 가
TYPE_CHECKING 으로만 참조하므로, 여기서도 SimpleNamespace 로 흉내만 낸다).
"""

from __future__ import annotations

from types import SimpleNamespace


from lifetrainer.slackio import blocks


# ── 페이크 stats/classifier ───────────────────────────────────────────


class FakeClassifier:
    _labels = {
        "coding": "코딩",
        "research": "리서치",
        "browsing": "웹",
        "sns": "SNS",
        "away": "자리비움",
        "off": "꺼짐",
        "unknown": "미분류",
    }

    def label(self, category_id: str) -> str:
        return self._labels.get(category_id, category_id)

    def color(self, category_id: str) -> str:
        return "#123456"

    def order(self) -> list[str]:
        return list(self._labels)


def _cat_sec(category: str, seconds: float, share: float):
    return SimpleNamespace(category=category, seconds=seconds, share=share)


def make_daily_stats(**overrides):
    base = dict(
        day="2026-08-16",
        total_span_sec=36000.0,
        active_sec=26400.0,
        afk_sec=3600.0,
        off_sec=3600.0,
        private_sec=0.0,          # 프라이빗을 안 쓴 날이 기본값이다
        repeated_defers=[],            # 미룬 것이 없는 날이 기본값이다
        coverage=0.86,
        by_category=[
            _cat_sec("coding", 14400.0, 0.55),
            _cat_sec("research", 7200.0, 0.27),
            _cat_sec("browsing", 4800.0, 0.18),
        ],
        top_apps=[("Code.exe", 12000.0), ("chrome.exe", 6000.0)],
        first_activity_ts=1755302400.0,
        last_activity_ts=1755331200.0,
        longest_focus=("coding", 30, 12),
        slot_categories=["off"] * 144,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def make_weekly_stats(**overrides):
    base = dict(
        end_day="2026-08-16",
        days=["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14", "2026-08-15", "2026-08-16"],
        this_week=[
            _cat_sec("coding", 72000.0, 0.6),
            _cat_sec("research", 24000.0, 0.2),
        ],
        last_week=[
            _cat_sec("coding", 60000.0, 0.55),
            _cat_sec("research", 30000.0, 0.27),
        ],
        delta={"coding": 12000.0, "research": -6000.0},
        daily_active=[("2026-08-10", 10000.0), ("2026-08-11", 12000.0)],
        best_day=("2026-08-11", 12000.0),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── 기본 블록 빌더 ─────────────────────────────────────────────────────


def test_header_structure():
    b = blocks.header("일일 리포트")
    assert b["type"] == "header"
    assert b["text"]["type"] == "plain_text"
    assert b["text"]["text"] == "일일 리포트"


def test_section_structure():
    b = blocks.section("*굵게* 일반")
    assert b["type"] == "section"
    assert b["text"] == {"type": "mrkdwn", "text": "*굵게* 일반"}


def test_context_structure():
    b = blocks.context(["a", "b"])
    assert b["type"] == "context"
    assert [e["text"] for e in b["elements"]] == ["a", "b"]
    assert all(e["type"] == "mrkdwn" for e in b["elements"])


def test_context_limits_to_10_elements():
    b = blocks.context([str(i) for i in range(20)])
    assert len(b["elements"]) <= 10


def test_divider_structure():
    assert blocks.divider() == {"type": "divider"}


def test_fields_section_basic_pairs():
    b = blocks.fields_section([("활동 시간", "7시간"), ("커버리지", "86%")])
    assert b["type"] == "section"
    assert len(b["fields"]) == 2
    assert b["fields"][0]["text"] == "*활동 시간*\n7시간"


def test_fields_section_caps_at_10_and_marks_truncation():
    pairs = [(f"라벨{i}", f"값{i}") for i in range(15)]
    b = blocks.fields_section(pairs)
    assert len(b["fields"]) <= 10
    # 마지막 필드는 잘렸다는 표시를 담아야 한다
    assert "생략" in b["fields"][-1]["text"]


def test_fields_section_within_limit_has_no_truncation_marker():
    pairs = [(f"라벨{i}", f"값{i}") for i in range(5)]
    b = blocks.fields_section(pairs)
    assert len(b["fields"]) == 5
    assert all("생략" not in f["text"] for f in b["fields"])


# ── 텍스트 길이 제한 ───────────────────────────────────────────────────


def test_section_text_truncated_to_3000_chars():
    long_text = "가" * 5000
    b = blocks.section(long_text)
    assert len(b["text"]["text"]) <= blocks.MAX_TEXT_LEN
    assert b["text"]["text"] != long_text
    assert "잘림" in b["text"]["text"]


def test_short_text_not_modified():
    text = "짧은 텍스트"
    b = blocks.section(text)
    assert b["text"]["text"] == text


# ── image_block: slack_file, image_url 아님 ────────────────────────────


def test_image_block_uses_slack_file():
    b = blocks.image_block("F0123456", "타임라인", "144슬롯 타임라인")
    assert b["type"] == "image"
    assert b["slack_file"] == {"id": "F0123456"}
    assert "image_url" not in b
    assert b["alt_text"] == "144슬롯 타임라인"
    assert b["title"]["type"] == "plain_text"


def test_image_block_never_sets_both_slack_file_and_image_url():
    b = blocks.image_block("F999", "t", "a")
    assert ("slack_file" in b) != ("image_url" in b) or "image_url" not in b
    assert "image_url" not in b


# ── 블록 개수 제한 (50개) ────────────────────────────────────────────


def test_enforce_block_limit_under_cap_unchanged():
    small = [blocks.divider() for _ in range(10)]
    assert blocks._enforce_block_limit(small) == small


def test_enforce_block_limit_over_cap_truncates_and_marks():
    big = [blocks.divider() for _ in range(60)]
    trimmed = blocks._enforce_block_limit(big)
    assert len(trimmed) <= blocks.MAX_BLOCKS
    assert trimmed[-1]["type"] == "context"
    assert "생략" in trimmed[-1]["elements"][0]["text"]


def test_planner_card_omits_web_button_without_url():
    result = blocks.planner_card_blocks("2026-08-19", 0.5, 1, 2)
    assert all(block.get("block_id") != "planner_open_actions" for block in result)


def test_planner_card_links_to_authenticated_web_planner():
    url = "https://planner.example/auth/enter?t=signed"
    result = blocks.planner_card_blocks("2026-08-19", 0.5, 1, 2, url)
    actions = next(block for block in result if block.get("block_id") == "planner_open_actions")
    button = actions["elements"][0]
    assert button["action_id"] == "open_planner"
    assert button["url"] == url
    assert "웹에서" in button["text"]["text"]


# ── daily_report_blocks ─────────────────────────────────────────────


def test_daily_report_blocks_starts_with_header_and_day():
    stats = make_daily_stats()
    result = blocks.daily_report_blocks(stats, FakeClassifier())
    assert result[0]["type"] == "header"
    assert stats.day in result[0]["text"]["text"]


def test_daily_report_blocks_ends_with_context():
    stats = make_daily_stats()
    result = blocks.daily_report_blocks(stats, FakeClassifier())
    assert result[-1]["type"] == "context"


def test_daily_report_blocks_within_block_limit():
    stats = make_daily_stats()
    result = blocks.daily_report_blocks(stats, FakeClassifier())
    assert len(result) <= blocks.MAX_BLOCKS


def test_daily_report_blocks_has_category_section_with_labels():
    stats = make_daily_stats()
    result = blocks.daily_report_blocks(stats, FakeClassifier())
    joined = "\n".join(b.get("text", {}).get("text", "") for b in result if b["type"] == "section")
    assert "코딩" in joined
    assert "리서치" in joined


def test_daily_report_blocks_at_most_one_emoji_per_category_line():
    stats = make_daily_stats()
    result = blocks.daily_report_blocks(stats, FakeClassifier())
    for b in result:
        if b["type"] != "section" or "text" not in b:
            continue
        for line in b["text"]["text"].split("\n"):
            emoji_count = sum(line.count(e) for e in blocks._CATEGORY_EMOJI.values())
            assert emoji_count <= 1


def test_daily_report_blocks_all_valid_block_types():
    stats = make_daily_stats()
    result = blocks.daily_report_blocks(stats, FakeClassifier())
    valid_types = {"header", "section", "context", "divider", "image"}
    assert all(b["type"] in valid_types for b in result)


# ── weekly_report_blocks ────────────────────────────────────────────


def test_weekly_report_blocks_starts_with_header():
    stats = make_weekly_stats()
    result = blocks.weekly_report_blocks(stats, FakeClassifier())
    assert result[0]["type"] == "header"
    assert stats.end_day in result[0]["text"]["text"]


def test_weekly_report_blocks_ends_with_context():
    stats = make_weekly_stats()
    result = blocks.weekly_report_blocks(stats, FakeClassifier())
    assert result[-1]["type"] == "context"


def test_weekly_report_blocks_includes_delta_comparison():
    stats = make_weekly_stats()
    result = blocks.weekly_report_blocks(stats, FakeClassifier())
    joined = "\n".join(b.get("text", {}).get("text", "") for b in result if b["type"] == "section")
    assert "지난주 대비" in joined


def test_weekly_report_blocks_within_block_limit():
    stats = make_weekly_stats()
    result = blocks.weekly_report_blocks(stats, FakeClassifier())
    assert len(result) <= blocks.MAX_BLOCKS


def test_weekly_report_blocks_handles_empty_delta():
    stats = make_weekly_stats(delta={})
    result = blocks.weekly_report_blocks(stats, FakeClassifier())
    assert result[0]["type"] == "header"  # 예외 없이 정상 생성됨


def test_weekly_report_blocks_handles_no_best_day():
    stats = make_weekly_stats(best_day=None)
    result = blocks.weekly_report_blocks(stats, FakeClassifier())
    assert result[0]["type"] == "header"


# ── 프라이빗 ─────────────────────────────────────────────────────────────


def test_프라이빗이_0이면_요약에_안_나온다():
    """★ 안 쓴 날 "프라이빗 0분"이 매일 붙으면 칸만 차지하고 아무것도 안 말한다.

    리포트는 **달라진 것**을 말해야 한다 ([CLAUDE.md](../../CLAUDE.md) §1 — 이미 아는
    것을 매번 다시 알리지 않는다).
    """
    text = str(blocks.daily_report_blocks(make_daily_stats(private_sec=0.0), FakeClassifier()))
    assert "프라이빗" not in text


def test_프라이빗이_있으면_요약에_나온다():
    text = str(blocks.daily_report_blocks(make_daily_stats(private_sec=5400.0), FakeClassifier()))
    assert "프라이빗" in text
    assert "1시간 30분" in text


def test_미룬_것이_없으면_리포트에_안_나온다():
    text = str(blocks.daily_report_blocks(make_daily_stats(repeated_defers=[]), FakeClassifier()))
    assert "미룬 것" not in text


def test_두_번_이상_미룬_것은_이름과_횟수로_나온다():
    stats = make_daily_stats(repeated_defers=[("보고서 쓰기", 3), ("운동", 2)])
    text = str(blocks.daily_report_blocks(stats, FakeClassifier()))
    assert "보고서 쓰기 (3번)" in text and "운동 (2번)" in text
