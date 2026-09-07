"""Slack Block Kit 빌더.

`stats`/`classifier` 파라미터는 순환 임포트를 피하려고 `TYPE_CHECKING` 아래에서만
타입을 임포트한다 (해당 모듈이 아직 없을 수도 있다). 런타임에는 속성만 보고
쓰는 덕 타이핑으로 접근한다 — `report.stats.DailyStats`/`WeeklyStats`,
`rollup.classify.Classifier` 를 실제로 import 하지 않는다.

Slack 의 실제 제한(문서 §6 참고):
  - 메시지당 블록 50개
  - section/context 텍스트 3000자
  - section 의 fields 최대 10개
넘는 입력이 들어오면 예외를 던지지 않고 잘라내되, 잘렸다는 표시를 남긴다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lifetrainer import timeutil

if TYPE_CHECKING:  # pragma: no cover - 타입 힌트 전용, 런타임 임포트 없음
    from lifetrainer.report.stats import DailyStats, WeeklyStats
    from lifetrainer.rollup.classify import Classifier

# ── Slack 제한 상수 ────────────────────────────────────────────────────

MAX_BLOCKS = 50
MAX_TEXT_LEN = 3000
MAX_FIELDS = 10
MAX_CONTEXT_ELEMENTS = 10
MAX_CHECKBOX_OPTIONS = 10  # Block Kit checkboxes 요소의 옵션 개수 제한

_TRUNC_MARK = " …(잘림)"

# plan_instance.status -> 표시 라벨 (SPEC §7-2 의 체크 기호 3종 대응)
_STATUS_LABEL: dict[str, str] = {
    "todo": "todo",
    "doing": "진행중",
    "done": "완료",
    "partial": "부분",
    "deferred": "이월",
    "canceled": "취소",
}

# 카테고리별 이모지. 카테고리당 최대 1개까지만 쓴다 (config/rules.yaml 의 카테고리 id 기준).
_CATEGORY_EMOJI: dict[str, str] = {
    "coding": "💻",
    "research": "🔬",
    "writing": "📝",
    "sns": "💬",
    "ops": "🛠️",
    "learning": "📚",
    "browsing": "🌐",
    "entertainment": "🍿",
    "gaming": "🎮",
    "away": "🚶",
    "off": "💤",
    "private": "🔒",
    "unknown": "❓",
}


def _truncate(text: str, limit: int = MAX_TEXT_LEN) -> str:
    """텍스트가 limit 을 넘으면 잘라내고 잘렸다는 표시를 붙인다."""
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(_TRUNC_MARK))
    return text[:keep] + _TRUNC_MARK


def _emoji_for(category_id: str) -> str:
    return _CATEGORY_EMOJI.get(category_id, "")


def _format_duration(seconds: float) -> str:
    """초 -> '1시간 10분' 형태. report.stats.format_hm 과 별개의 로컬 헬퍼

    (순환 임포트를 피하려고 stats 모듈을 런타임에 import 하지 않는다)."""
    total_min = int(round(max(0.0, seconds) / 60))
    h, m = divmod(total_min, 60)
    if h and m:
        return f"{h}시간 {m}분"
    if h:
        return f"{h}시간"
    return f"{m}분"


def _enforce_block_limit(blocks: list[dict]) -> list[dict]:
    """블록이 50개를 넘으면 잘라내고 마지막에 잘렸다는 context 블록을 붙인다."""
    if len(blocks) <= MAX_BLOCKS:
        return blocks
    omitted = len(blocks) - (MAX_BLOCKS - 1)
    trimmed = blocks[: MAX_BLOCKS - 1]
    trimmed.append(context([f"…{omitted}개 블록 생략 (Slack 메시지당 50개 제한)"]))
    return trimmed


# ── 기본 블록 빌더 ─────────────────────────────────────────────────────


def header(text: str) -> dict:
    """header 블록. plain_text 는 실무상 150자 안팎으로 제한하는 것이 안전하다."""
    return {"type": "header", "text": {"type": "plain_text", "text": _truncate(text, 150), "emoji": True}}


def section(text: str) -> dict:
    """mrkdwn section 블록."""
    return {"type": "section", "text": {"type": "mrkdwn", "text": _truncate(text)}}


def fields_section(pairs: list[tuple[str, str]]) -> dict:
    """label/value 쌍을 2열 fields 로. 최대 10개, 넘으면 잘라내고 표시를 남긴다."""
    truncated = False
    items = pairs
    if len(items) > MAX_FIELDS:
        items = items[: MAX_FIELDS - 1]
        truncated = True

    fields = [{"type": "mrkdwn", "text": _truncate(f"*{label}*\n{value}", 2000)} for label, value in items]
    if truncated:
        omitted = len(pairs) - len(items)
        fields.append({"type": "mrkdwn", "text": f"_…외 {omitted}개 항목 생략_"})

    return {"type": "section", "fields": fields}


def context(texts: list[str]) -> dict:
    """context 블록. 요소는 최대 10개까지만 쓴다."""
    elements = [{"type": "mrkdwn", "text": _truncate(t)} for t in texts[:MAX_CONTEXT_ELEMENTS]]
    return {"type": "context", "elements": elements}


def divider() -> dict:
    return {"type": "divider"}


def image_block(file_id: str, title: str, alt: str) -> dict:
    """업로드된 파일(files_upload_v2 결과의 file id)을 참조하는 image 블록.

    `slack_file: {"id": file_id}` 형태로만 채운다. `image_url` 과 동시에
    지정하면 Slack 이 거부하므로 이 헬퍼는 절대로 `image_url` 을 쓰지 않는다
    (조사 문서 §6).
    """
    return {
        "type": "image",
        "title": {"type": "plain_text", "text": _truncate(title, 2000), "emoji": True},
        "slack_file": {"id": file_id},
        "alt_text": _truncate(alt, 2000),
    }


# ── `/plan` `/view` 카드 ─────────────────────────────────────────────


def planner_card_blocks(
    day: str, overall: float, achieved: int, total: int, planner_url: str | None = None
) -> list[dict]:
    """`/view` 카드: 헤더 + 달성률 fields + 선택적 웹 플래너 링크.

    호출부(`slackio.app`)가 `image_block` 으로 덧붙인다 — 파일 id 는 여기서
    알 수 없기 때문이다. `planner_url` 은 사용자별 단기 서명 링크이며, 웹 주소가
    설정되지 않은 설치에서는 블록 자체를 만들지 않아 깨진 버튼을 남기지 않는다.
    """
    result = [
        header(f"플래너 — {day}"),
        fields_section(
            [
                ("달성률", f"{overall * 100:.0f}%"),
                ("완료", f"{achieved}/{total}"),
            ]
        ),
    ]
    if planner_url:
        result.append(
            {
                "type": "actions",
                "block_id": "planner_open_actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "웹에서 계획 조절", "emoji": True},
                        "style": "primary",
                        "action_id": "open_planner",
                        "url": planner_url,
                    }
                ],
            }
        )
    return result


# ── `/del` 인터랙션 (SPEC §7-2) ────────────────────────────────────────


def del_picker_blocks(day: str, rows: list) -> list[dict]:
    """`/del` 체크박스 목록. `rows` 는 `plan.models.PlanInstanceRow` 호환 객체 목록이다.

    완료(`done`) 항목을 걸러내는 것은 호출부(`list_instances(include_done=False)`)의
    책임이다 — 여기서는 넘어온 것을 그대로 그린다. Block Kit checkboxes 는
    옵션이 최대 10개라 넘치면 앞 10개만 체크박스로 보여주고 나머지는
    `/del <번호>` 로 지정하라는 안내를 context 블록으로 남긴다.
    """
    items = rows[:MAX_CHECKBOX_OPTIONS]
    options = []
    for r in items:
        dur = f"{r.planned_min}분" if r.planned_min else "—"
        label = f"{r.ordinal}. {r.title}  [{dur}]  {_STATUS_LABEL.get(r.status, r.status)}"
        options.append(
            {"text": {"type": "plain_text", "text": _truncate(label, 75), "emoji": True}, "value": str(r.id)}
        )

    result: list[dict] = [header(f"🗑 삭제할 계획을 선택하세요 ({day})")]
    if options:
        result.append(
            {
                "type": "actions",
                "block_id": "del_picker",
                "elements": [{"type": "checkboxes", "action_id": "del_select", "options": options}],
            }
        )
    if len(rows) > MAX_CHECKBOX_OPTIONS:
        omitted = len(rows) - MAX_CHECKBOX_OPTIONS
        result.append(context([f"…{omitted}개 항목은 목록에서 생략됨 — 번호로 지정하세요 (예: /del 11,12)"]))
    result.append(
        {
            "type": "actions",
            "block_id": "del_buttons",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "삭제", "emoji": True},
                    "style": "danger",
                    "action_id": "del_confirm",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "취소", "emoji": True},
                    "action_id": "del_cancel",
                },
            ],
        }
    )
    return _enforce_block_limit(result)


def del_result_blocks(text: str, instance_ids: list[int]) -> list[dict]:
    """삭제 확인 카드: 결과 문구 + 실행취소 버튼. 버튼 값은 아카이브된 id 들을

    콤마로 이어붙인 문자열이다 — `action['value']` 는 문자열만 옮길 수 있어서다.
    실행취소 자체의 15초 만료 타이머는 `slackio.app` 이 스케줄링한다(이 모듈은
    Slack 을 모르므로 여기서는 다루지 않는다 — 이 모듈은 순수 블록 빌더다).
    """
    value = ",".join(str(i) for i in instance_ids)
    return [
        section(text),
        {
            "type": "actions",
            "block_id": "del_undo_actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "실행 취소", "emoji": True},
                    "action_id": "del_undo",
                    "value": value,
                }
            ],
        },
    ]


def _context_generated_at() -> dict:
    """생성 시각 context 블록. Slack 의 `<!date^…>` 토큰을 써서 각 사용자의
    로컬 타임존으로 자동 렌더링되게 한다 (cfg/tz 를 받지 않아도 되는 이유)."""
    ts = timeutil.now_ts()
    fallback = timeutil.iso_utc(ts)
    token = f"<!date^{int(ts)}^{{date_short_pretty}} {{time}}|{fallback}>"
    return context([f"Life Trainer · {token}"])


# ── 리포트 카드 ────────────────────────────────────────────────────────


def daily_report_blocks(stats: "DailyStats", classifier: "Classifier") -> list[dict]:
    """일일 리포트 카드: header → 활동 요약 fields → 카테고리 상위 항목 → context."""
    blocks: list[dict] = [header(f"일일 리포트 — {stats.day}")]

    summary: list[tuple[str, str]] = [
        ("활동 시간", _format_duration(stats.active_sec)),
        ("커버리지", f"{stats.coverage * 100:.0f}%"),
        ("자리비움", _format_duration(stats.afk_sec)),
        ("꺼짐", _format_duration(stats.off_sec)),
    ]
    # ★ 0 일 때는 아예 안 보인다. 프라이빗을 안 쓴 날 "프라이빗 0분" 이 매일 붙으면
    #   칸만 차지하고 아무것도 안 말한다 — 리포트는 **달라진 것**을 말해야 한다.
    if stats.private_sec > 0:
        summary.append(("프라이빗", _format_duration(stats.private_sec)))
    if stats.longest_focus:
        cat, _start_slot, length_slots = stats.longest_focus
        summary.append(("최장 몰입", f"{_emoji_for(cat)} {classifier.label(cat)} ({length_slots}슬롯)".strip()))
    blocks.append(fields_section(summary))

    if stats.by_category:
        lines = []
        for cs in stats.by_category[:8]:
            emoji = _emoji_for(cs.category)
            prefix = f"{emoji} " if emoji else ""
            lines.append(f"{prefix}*{classifier.label(cs.category)}* — {_format_duration(cs.seconds)} ({cs.share * 100:.0f}%)")
        blocks.append(section("\n".join(lines)))

    if stats.top_apps:
        top = ", ".join(f"{app}({_format_duration(sec)})" for app, sec in stats.top_apps[:5])
        blocks.append(context([f"상위 앱: {top}"]))

    # ★ 비어 있으면 **아무것도 안 붙는다.** "미룬 것 없음" 을 매일 적으면 칸만 차지하고
    #   아무것도 안 말한다 — 리포트는 달라진 것을 말해야 한다 (CLAUDE.md §1).
    #
    #   2026-09-01 까지 이 숫자를 보여주는 곳이 없었다. 슬랙 `/defer` 가 이월을 만들고
    #   `v_carry_debt` 뷰도 동작하는데 **읽는 쪽만 없었다.**
    if stats.repeated_defers:
        items = " · ".join(f"{title} ({depth}번)" for title, depth in stats.repeated_defers)
        blocks.append(context([f"↩️ 두 번 이상 미룬 것: {items}"]))

    blocks.append(_context_generated_at())
    return _enforce_block_limit(blocks)


def weekly_report_blocks(stats: "WeeklyStats", classifier: "Classifier") -> list[dict]:
    """주간 리포트 카드: header → 요약 fields → 카테고리 상위 항목 → 지난주 대비 → context."""
    start_day = stats.days[0] if stats.days else stats.end_day
    blocks: list[dict] = [header(f"주간 리포트 — {start_day} ~ {stats.end_day}")]

    total_active = sum(cs.seconds for cs in stats.this_week)
    summary: list[tuple[str, str]] = [("이번 주 활동", _format_duration(total_active))]
    if stats.best_day:
        best_day, best_sec = stats.best_day
        summary.append(("최고 활동일", f"{best_day} ({_format_duration(best_sec)})"))
    blocks.append(fields_section(summary))

    if stats.this_week:
        lines = []
        for cs in stats.this_week[:8]:
            emoji = _emoji_for(cs.category)
            prefix = f"{emoji} " if emoji else ""
            lines.append(f"{prefix}*{classifier.label(cs.category)}* — {_format_duration(cs.seconds)} ({cs.share * 100:.0f}%)")
        blocks.append(section("\n".join(lines)))

    if stats.delta:
        top_changes = sorted(stats.delta.items(), key=lambda kv: abs(kv[1]), reverse=True)[:6]
        lines = []
        for cat, diff_sec in top_changes:
            sign = "+" if diff_sec >= 0 else "-"
            lines.append(f"{classifier.label(cat)}: {sign}{_format_duration(abs(diff_sec))}")
        blocks.append(divider())
        blocks.append(section("*지난주 대비*\n" + "\n".join(lines)))

    blocks.append(_context_generated_at())
    return _enforce_block_limit(blocks)
