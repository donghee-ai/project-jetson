"""대화에서 모델이 부를 수 있는 툴 — 정의와 몸통.

## 원칙

1. **집계는 SQL, 문장화는 LLM** (설계 3원칙). 툴 몸통은 이미 있는 집계 함수
   (`report/stats.compute_daily`, `plan/achieve.plans_for_day`)를 부르기만 한다.
   여기서 초를 다시 더하지 않는다.
2. **스키마에 `pattern` 금지.** llama.cpp 의 GBNF 변환기가 정규식을 못 다뤄
   요청 전체가 400 이 된다(`docs/openclaw-agent.md §4-5`). `client._check_no_pattern`
   이 발신 직전에 한 번 더 막지만, 애초에 쓰지 않는다. 형식 제약은 설명 문장으로 건다.
3. **결과는 짧게.** 툴 결과는 다음 턴의 입력이 된다. 깊이가 곧 비용이라
   (32K 에서 생성 속도 -70%) 상위 N 건만 돌려주고 나머지는 건수만 알린다.
4. **쓰기 툴은 게이트 뒤에 둔다.** `gated=True` 인 툴은 트리거가 켜져야 주입된다
   (`trigger.py`). 토큰 절약이 1차 목적이고, 실수로 쓰기가 일어나지 않는 것이 2차다.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, TYPE_CHECKING

from lifetrainer import timeutil
from lifetrainer.plan.achieve import day_achievement, plans_for_day
from lifetrainer.report.stats import compute_daily, format_hm

if TYPE_CHECKING:
    from lifetrainer.config import Config

logger = logging.getLogger(__name__)

MAX_DOC_RESULTS = 5
MAX_CATEGORIES = 6
MAX_TOP_APPS = 5
MAX_REMINDER_DAYS = 30
# 웹 페이지 본문을 이만큼만 싣는다. 깊이가 곧 비용이다 (32K 에서 생성 속도 -70%).
MAX_FETCH_CHARS = 3500

# 예약 알림이 타는 job kind. worker.HANDLERS 에 같은 이름의 핸들러가 있어야 실제로 발송된다.
REMINDER_KIND = "reminder"

# FTS5 는 연산자 문자를 만나면 구문 오류를 낸다. 검색어에서 단어만 뽑아 인용부호로
# 감싸는 방식으로 정규화한다 (사용자 질문이 그대로 들어오므로 반드시 필요하다).
_FTS_WORD_RE = re.compile(r"[0-9A-Za-z가-힣._+#-]+")

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ToolError(Exception):
    """툴 실행 실패. 메시지가 그대로 모델에게 툴 결과로 전달된다."""


@dataclass
class ToolContext:
    """툴 몸통이 쓰는 실행 환경.

    `channel` 은 이 대화가 오가는 Slack 대상이다 — 예약 알림이 "어디로" 갈지는
    대화가 일어난 자리에서 정해져야 하므로 모델 인자로 받지 않는다.
    """

    conn: sqlite3.Connection
    cfg: "Config"
    today: str
    actor: str = "user"
    channel: str | None = None


@dataclass
class Tool:
    name: str
    schema: dict
    handler: Callable[[ToolContext, dict], str]
    gated: bool = False


def _resolve_day(ctx: ToolContext, value: Any) -> str:
    """`day` 인자를 논리적 날짜로 정규화한다. 비면 오늘.

    소형 모델이 "오늘"/"어제" 같은 한국어를 그대로 넣는 일이 잦아 함께 받아준다 —
    되묻는 것보다 알아듣는 편이 턴을 아낀다.
    """
    if value in (None, "", "오늘", "today"):
        return ctx.today
    text = str(value).strip()
    if text in ("어제", "yesterday"):
        return _shift_day(ctx.today, -1)
    if text in ("내일", "tomorrow"):
        return _shift_day(ctx.today, 1)
    if not _DAY_RE.match(text):
        raise ToolError(f"날짜 형식이 잘못됐습니다: {text!r}. YYYY-MM-DD 로 주세요.")
    return text


def _shift_day(day: str, delta: int) -> str:
    """`day` 에서 `delta` 일 이동한 날짜 문자열."""
    from datetime import date, timedelta

    y, m, d = (int(x) for x in day.split("-"))
    return (date(y, m, d) + timedelta(days=delta)).isoformat()


# ── 조회 툴 (항상 주입) ────────────────────────────────────────────────


def _summarize_day(ctx: ToolContext, day: str) -> str:
    """하루치 활동을 한 덩어리 텍스트로. 두 툴이 공유한다."""
    stats = compute_daily(ctx.conn, ctx.cfg, day)
    if stats.active_sec <= 0:
        return f"{day}: 계측된 활동이 없습니다."

    cats = ", ".join(
        f"{c.category} {format_hm(c.seconds)}({c.share * 100:.0f}%)"
        for c in stats.by_category[:MAX_CATEGORIES]
    )
    parts = [f"{day} 활동 {format_hm(stats.active_sec)}", f"카테고리: {cats}"]
    if stats.top_apps:
        apps = ", ".join(f"{a} {format_hm(s)}" for a, s in stats.top_apps[:MAX_TOP_APPS])
        parts.append(f"상위 앱: {apps}")
    if stats.longest_focus:
        cat, _, length = stats.longest_focus
        mins = length * ctx.cfg.rollup.slot_minutes
        parts.append(f"최장 집중: {cat} {mins}분")
    return " / ".join(parts)


def _tool_get_activity_summary(ctx: ToolContext, args: dict) -> str:
    day = _resolve_day(ctx, args.get("day"))
    return _summarize_day(ctx, day)


def _tool_compare_days(ctx: ToolContext, args: dict) -> str:
    day_a = _resolve_day(ctx, args.get("day_a"))
    day_b = _resolve_day(ctx, args.get("day_b"))
    if day_a == day_b:
        raise ToolError("두 날짜가 같습니다. 서로 다른 날을 주세요.")

    a = compute_daily(ctx.conn, ctx.cfg, day_a)
    b = compute_daily(ctx.conn, ctx.cfg, day_b)
    lines = [
        f"{day_a} 활동 {format_hm(a.active_sec)} vs {day_b} 활동 {format_hm(b.active_sec)}"
    ]

    a_map = {c.category: c.seconds for c in a.by_category}
    b_map = {c.category: c.seconds for c in b.by_category}
    deltas = sorted(
        ((cat, b_map.get(cat, 0.0) - a_map.get(cat, 0.0)) for cat in set(a_map) | set(b_map)),
        key=lambda p: abs(p[1]),
        reverse=True,
    )[:MAX_CATEGORIES]
    if deltas:
        moved = ", ".join(
            f"{cat} {'+' if d >= 0 else '-'}{format_hm(abs(d))}" for cat, d in deltas
        )
        lines.append(f"카테고리 변화({day_b} 기준): {moved}")
    return " / ".join(lines)


def _tool_get_plans(ctx: ToolContext, args: dict) -> str:
    day = _resolve_day(ctx, args.get("day"))
    instances = plans_for_day(ctx.conn, ctx.cfg, day)
    if not instances:
        return f"{day}: 등록된 계획이 없습니다."

    overall, achieved, total = day_achievement(ctx.conn, ctx.cfg, day)
    items = "; ".join(
        f"[{pi.instance_id}] {pi.plan.start_min // 60:02d}:{pi.plan.start_min % 60:02d}"
        f"~{pi.plan.end_min // 60:02d}:{pi.plan.end_min % 60:02d} {pi.plan.title}"
        f" ({pi.status}, 달성 {pi.achievement * 100:.0f}%)"
        for pi in instances
    )
    return f"{day} 계획 {total}건, 전체 달성률 {overall * 100:.0f}% ({achieved}건 완료): {items}"


def _tool_search_docs(ctx: ToolContext, args: dict) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("검색어가 비었습니다.")
    limit = args.get("limit")
    try:
        limit = max(1, min(MAX_DOC_RESULTS, int(limit))) if limit is not None else 3
    except (TypeError, ValueError):
        limit = 3

    words = _FTS_WORD_RE.findall(query)
    if not words:
        raise ToolError(f"검색어에서 쓸 수 있는 단어를 찾지 못했습니다: {query!r}")
    match = " OR ".join(f'"{w}"' for w in words[:8])

    try:
        rows = ctx.conn.execute(
            "SELECT d.title, d.score, d.url FROM doc_fts f JOIN doc d ON d.id = f.rowid "
            "WHERE doc_fts MATCH ? AND d.dup_of IS NULL "
            "ORDER BY d.score DESC LIMIT ?",
            (match, limit),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("문서 검색 실패 (query=%r): %s", query, exc)
        raise ToolError(f"문서 검색에 실패했습니다: {exc}") from exc

    if not rows:
        # ★ 예전에는 여기서 "관심사 상위 문서"로 **대체**했다. 모델이 활동 카테고리
        # 이름("논문 리서치")을 검색어로 넣어 0건이 나오는 경우를 구하려던 것이었다.
        # 그런데 "시립도서관 이번 주 휴관일" 처럼 전혀 무관한 질의에도 젯슨 문서를
        # 들이밀어, 모델이 그걸 성실히 설명하는 엉뚱한 답이 나왔다.
        #
        # 대체하지 않는다. **없으면 없다고 하고, 다시 검색할 재료만 준다.**
        # 추천이 목적이었다면 모델이 관심사 낱말로 한 번 더 부르면 된다.
        terms = _interest_terms(ctx, 6)
        hint = f" 관심사 기반 추천이 필요하면 이 낱말로 다시 검색하세요: {', '.join(terms)}." if terms else ""
        return f"'{query}' 로 검색된 문서가 없습니다.{hint}"

    items = "; ".join(f"{r[0]} (점수 {float(r[1] or 0):.1f}, {r[2]})" for r in rows)
    return f"'{query}' 검색 결과 {len(rows)}건: {items}"


def _interest_terms(ctx: ToolContext, limit: int) -> list[str]:
    """관심사 가중치 상위 낱말. 검색이 빈손일 때 "이걸로 다시 찾아보라"는 힌트로 쓴다."""
    try:
        return [
            str(r[0])
            for r in ctx.conn.execute(
                "SELECT term FROM interest ORDER BY weight DESC, term ASC LIMIT ?", (limit,)
            ).fetchall()
        ]
    except sqlite3.Error as exc:
        logger.warning("관심사 낱말 조회 실패: %s", exc)
        return []


# ── 쓰기 툴 (트리거로만 주입) ──────────────────────────────────────────


def _tool_add_plan(ctx: ToolContext, args: dict) -> str:
    from lifetrainer.plan.models import add_instance

    title = str(args.get("title") or "").strip()
    if not title:
        raise ToolError("계획 제목이 비었습니다.")
    day = _resolve_day(ctx, args.get("day"))

    try:
        start_min = int(args["start_min"])
        end_min = int(args["end_min"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ToolError(
            "start_min/end_min 은 자정 기준 분 단위 정수입니다 (예: 09:00 → 540)."
        ) from exc

    category = args.get("category")
    category = str(category).strip() if category else None

    try:
        instance_id = add_instance(
            ctx.conn,
            ctx.cfg,
            day,
            title=title,
            start_min=start_min,
            end_min=end_min,
            category=category,
            source="chat",
        )
    except (ValueError, sqlite3.Error) as exc:
        raise ToolError(f"계획 추가 실패: {exc}") from exc

    return (
        f"계획을 추가했습니다: [{instance_id}] {day} "
        f"{start_min // 60:02d}:{start_min % 60:02d}~{end_min // 60:02d}:{end_min % 60:02d} {title}"
    )


def _resolve_when(ctx: ToolContext, args: dict) -> float:
    """예약 시각을 유닉스 타임스탬프로 푼다. `delay_minutes` 가 `at_time` 보다 우선.

    `at_time` 이 이미 지난 시각이면 다음 날로 넘긴다 — 밤 11시에 "8시에 알려줘"는
    언제나 내일 아침을 뜻하지, 17시간 전을 뜻하지 않는다.
    """
    now = timeutil.now_ts()

    delay = args.get("delay_minutes")
    if delay not in (None, ""):
        try:
            minutes = float(delay)
        except (TypeError, ValueError) as exc:
            raise ToolError(f"delay_minutes 가 숫자가 아닙니다: {delay!r}") from exc
        if minutes <= 0:
            raise ToolError("delay_minutes 는 0보다 커야 합니다.")
        if minutes > MAX_REMINDER_DAYS * 24 * 60:
            raise ToolError(f"{MAX_REMINDER_DAYS}일 뒤까지만 예약할 수 있습니다.")
        return now + minutes * 60

    at_time = str(args.get("at_time") or "").strip()
    if not at_time:
        raise ToolError("delay_minutes 또는 at_time 중 하나는 있어야 합니다.")
    parts = at_time.split(":")
    try:
        hour, minute = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (IndexError, ValueError) as exc:
        raise ToolError(f"at_time 형식이 잘못됐습니다: {at_time!r}. HH:MM 로 주세요.") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ToolError(f"at_time 이 유효한 시각이 아닙니다: {at_time!r}")

    local = timeutil.from_ts(now, ctx.cfg.tz)
    at_day = str(args.get("at_day") or "").strip()
    if at_day:
        if not _DAY_RE.match(at_day):
            raise ToolError(f"at_day 형식이 잘못됐습니다: {at_day!r}. YYYY-MM-DD 로 주세요.")
        y, m, d = (int(x) for x in at_day.split("-"))
        target = local.replace(year=y, month=m, day=d, hour=hour, minute=minute, second=0, microsecond=0)
    else:
        target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)

    ts = timeutil.to_ts(target)
    if not at_day and ts <= now:
        ts += 86400.0
    if ts <= now:
        raise ToolError("이미 지난 시각입니다. 앞으로의 시각을 주세요.")
    return ts


def _tool_schedule_reminder(ctx: ToolContext, args: dict) -> str:
    from lifetrainer.llm.queue import enqueue

    text = str(args.get("text") or "").strip()
    if not text:
        raise ToolError("알릴 내용이 비었습니다.")
    when = _resolve_when(ctx, args)

    job_id = enqueue(
        ctx.conn,
        REMINDER_KIND,
        {"text": text, "channel": ctx.channel, "actor": ctx.actor},
        priority=10,  # 시각이 정해진 일이라 배치 작업보다 먼저 집혀야 한다
        not_before=when,
    )
    if job_id is None:
        raise ToolError("같은 예약이 이미 있습니다.")

    local = timeutil.from_ts(when, ctx.cfg.tz)
    return f"[{job_id}] {local.strftime('%Y-%m-%d %H:%M')} 에 '{text}' 알림을 예약했습니다."


def _tool_list_reminders(ctx: ToolContext, args: dict) -> str:
    import json as _json

    rows = ctx.conn.execute(
        "SELECT id, payload_json, not_before FROM job"
        " WHERE kind = ? AND state = 'queued' ORDER BY not_before ASC LIMIT 20",
        (REMINDER_KIND,),
    ).fetchall()
    if not rows:
        return "예약된 알림이 없습니다."

    items = []
    for row in rows:
        try:
            text = _json.loads(row[1]).get("text", "")
        except (ValueError, TypeError):
            text = ""
        local = timeutil.from_ts(float(row[2]), ctx.cfg.tz)
        items.append(f"[{row[0]}] {local.strftime('%m-%d %H:%M')} {text}")
    return f"예약된 알림 {len(rows)}건: " + "; ".join(items)


def _tool_cancel_reminder(ctx: ToolContext, args: dict) -> str:
    from lifetrainer.db import transaction

    raw_id = args.get("reminder_id")
    try:
        reminder_id = int(raw_id)
    except (TypeError, ValueError) as exc:
        raise ToolError(
            f"reminder_id 가 숫자가 아닙니다: {raw_id!r}. 목록을 먼저 조회해 번호를 확인하세요."
        ) from exc

    with transaction(ctx.conn) as tx:
        cur = tx.execute(
            "UPDATE job SET state = 'cancelled', finished_at = ?"
            " WHERE id = ? AND kind = ? AND state = 'queued'",
            (timeutil.now_ts(), reminder_id, REMINDER_KIND),
        )
        changed = cur.rowcount
    if not changed:
        raise ToolError(f"취소할 수 있는 예약 {reminder_id} 번을 찾지 못했습니다.")
    return f"예약 {reminder_id} 번을 취소했습니다."


def _tool_fetch_url(ctx: ToolContext, args: dict) -> str:
    """공개 웹 페이지를 읽어 온다.

    이 프로젝트는 원래 "기기 밖으로 안 나가는 내 데이터에 답한다" 를 원칙으로 삼았고
    (핸드오프 §7-B), 실시간 웹 조회는 범위 밖이었다. 사용자가 명시적으로 요청해
    범위를 넓힌 기능이다. 그래서 **읽기 전용**이고, 대상은 `webfetch.assert_public_url`
    이 통과시킨 공개 인터넷 주소로만 제한한다 — 이 기기의 웹 플래너·llama-server·
    노트북 ActivityWatch 는 툴로 접근할 수 없다.
    """
    from lifetrainer.llm.webfetch import WebFetchError, fetch_page

    url = str(args.get("url") or "").strip()
    if not url:
        raise ToolError("url 이 비었습니다.")
    if "://" not in url:
        url = "https://" + url

    try:
        page = fetch_page(ctx.cfg, ctx.conn, url, max_chars=MAX_FETCH_CHARS)
    except WebFetchError as exc:
        raise ToolError(str(exc)) from exc

    label = _source_label(page.url)
    head = f"{page.title} — {page.url}" if page.title else page.url
    tail = "\n(이하 생략)" if page.truncated else ""
    return f"[웹 · {label}] {head}\n{page.text}{tail}"


# 아는 출처는 무엇인지 한국어로 못박아 둔다. 모델이 "구글 검색어 트렌드" 를 읽고
# "유튜브 급상승 1위" 라고 답하는 일이 실제로 있었다 — 데이터도 링크도 진짜인데
# **그게 무엇인지에 대한 주장**이 틀렸다. 프롬프트로는 안 잡혀서(온도를 낮추면
# 오히려 악화) 결과 문자열에 라벨을 박는다.
_SOURCE_LABELS = (
    ("trends.google", "구글 검색어 급상승 (유튜브 급상승이 아님)"),
    ("github.com/trending", "GitHub 트렌딩 저장소"),
    ("news.ycombinator.com", "Hacker News 프론트페이지"),
    ("youtube.com", "유튜브"),
)


def _source_label(url: str) -> str:
    low = url.lower()
    for needle, label in _SOURCE_LABELS:
        if needle in low:
            return label
    return "웹 페이지"


REGISTRY: dict[str, Tool] = {}


def _register(tool: Tool) -> Tool:
    REGISTRY[tool.name] = tool
    return tool


_register(
    Tool(
        name="get_activity_summary",
        schema={
            "type": "function",
            "function": {
                "name": "get_activity_summary",
                "description": (
                    "특정 날짜에 사용자가 실제로 무엇을 했는지 조회한다 "
                    "(카테고리별 시간, 상위 앱, 최장 집중). 오늘 것은 이미 알고 있으니 "
                    "다른 날이 궁금할 때 쓴다."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "day": {
                            "type": "string",
                            "description": "YYYY-MM-DD 형식. 생략하면 오늘.",
                        }
                    },
                    "required": [],
                },
            },
        },
        handler=_tool_get_activity_summary,
    )
)

_register(
    Tool(
        name="compare_days",
        schema={
            "type": "function",
            "function": {
                "name": "compare_days",
                "description": "두 날짜의 활동을 비교한다. 카테고리별 증감을 돌려준다.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "day_a": {"type": "string", "description": "기준 날짜, YYYY-MM-DD"},
                        "day_b": {"type": "string", "description": "비교 날짜, YYYY-MM-DD"},
                    },
                    "required": ["day_a", "day_b"],
                },
            },
        },
        handler=_tool_compare_days,
    )
)

_register(
    Tool(
        name="get_plans",
        schema={
            "type": "function",
            "function": {
                "name": "get_plans",
                "description": (
                    "특정 날짜의 계획과 달성률을 조회한다. 오늘 계획은 이미 알고 있으니 "
                    "다른 날이 궁금할 때 쓴다."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "day": {"type": "string", "description": "YYYY-MM-DD 형식. 생략하면 오늘."}
                    },
                    "required": [],
                },
            },
        },
        handler=_tool_get_plans,
    )
)

_register(
    Tool(
        name="search_docs",
        schema={
            "type": "function",
            "function": {
                "name": "search_docs",
                "description": (
                    "수집해 둔 문서(RSS·arXiv)를 검색한다. 사용자의 관심사나 일정에 맞는 "
                    "읽을거리를 찾을 때 쓴다."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "검색어. 주제 낱말을 쓴다 (예: jetson, 추론 최적화). "
                                "'논문 리서치' 같은 활동 카테고리 이름은 검색어가 아니다."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": f"결과 개수, 1~{MAX_DOC_RESULTS}. 기본 3.",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        handler=_tool_search_docs,
    )
)

_register(
    Tool(
        name="add_plan",
        gated=True,
        schema={
            "type": "function",
            "function": {
                "name": "add_plan",
                "description": "사용자의 일정에 계획을 하나 추가한다.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "계획 제목"},
                        "start_min": {
                            "type": "integer",
                            "description": "시작 시각, 자정 기준 분. 09:00 이면 540.",
                        },
                        "end_min": {
                            "type": "integer",
                            "description": "종료 시각, 자정 기준 분. 12:00 이면 720.",
                        },
                        "day": {"type": "string", "description": "YYYY-MM-DD. 생략하면 오늘."},
                        "category": {
                            "type": "string",
                            "description": "카테고리 (coding/research/writing 등). 모르면 생략.",
                        },
                    },
                    "required": ["title", "start_min", "end_min"],
                },
            },
        },
        handler=_tool_add_plan,
    )
)


_register(
    Tool(
        name="schedule_reminder",
        gated=True,
        schema={
            "type": "function",
            "function": {
                "name": "schedule_reminder",
                "description": (
                    "지정한 시각에 사용자에게 Slack 으로 알림을 보내도록 예약한다. "
                    "'몇 분 뒤' 는 delay_minutes, '몇 시에' 는 at_time 을 쓴다."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "알림에 보낼 내용"},
                        "delay_minutes": {
                            "type": "integer",
                            "description": "지금부터 몇 분 뒤에 알릴지. '10분 뒤' 면 10.",
                        },
                        "at_time": {
                            "type": "string",
                            "description": "알릴 시각 HH:MM (24시간제). 이미 지났으면 다음 날로 잡힌다.",
                        },
                        "at_day": {
                            "type": "string",
                            "description": "알릴 날짜 YYYY-MM-DD. 생략하면 오늘 또는 내일.",
                        },
                    },
                    "required": ["text"],
                },
            },
        },
        handler=_tool_schedule_reminder,
    )
)

_register(
    Tool(
        name="list_reminders",
        gated=True,
        schema={
            "type": "function",
            "function": {
                "name": "list_reminders",
                "description": "예약해 둔 알림 목록을 조회한다.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        handler=_tool_list_reminders,
    )
)

_register(
    Tool(
        name="cancel_reminder",
        gated=True,
        schema={
            "type": "function",
            "function": {
                "name": "cancel_reminder",
                "description": (
                    "예약된 알림을 취소한다. 번호를 모르면 list_reminders 로 먼저 조회한다."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reminder_id": {
                            "type": "integer",
                            "description": "list_reminders 가 보여준 대괄호 안 번호",
                        }
                    },
                    "required": ["reminder_id"],
                },
            },
        },
        handler=_tool_cancel_reminder,
    )
)


_register(
    Tool(
        name="fetch_url",
        gated=True,
        schema={
            "type": "function",
            "function": {
                "name": "fetch_url",
                "description": (
                    "공개 웹 페이지를 열어 본문 텍스트를 읽는다. 지금 이 순간의 정보"
                    "(트렌딩 순위, 뉴스, 릴리스 노트 등)는 반드시 이 툴로 확인한다. "
                    "쓸 만한 주소: GitHub 트렌딩 https://github.com/trending / "
                    "Hacker News https://news.ycombinator.com/ / "
                    "한국 실시간 급상승 검색어 https://trends.google.co.kr/trending/rss?geo=KR . "
                    "유튜브 급상승(youtube.com/feed/trending)은 자바스크립트로 그려져 "
                    "본문을 읽을 수 없다 — 시도해서 실패하면 확인 못 했다고 답한다. "
                    "수집해 둔 문서를 찾는 것이 목적이면 search_docs 를 쓴다."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "열어볼 http/https 주소. 공개 사이트만 가능하다.",
                        }
                    },
                    "required": ["url"],
                },
            },
        },
        handler=_tool_fetch_url,
    )
)


def schemas_for(names: list[str]) -> list[dict]:
    """툴 이름 목록을 llama-server 에 보낼 스키마 목록으로."""
    return [REGISTRY[n].schema for n in names if n in REGISTRY]


def base_tool_names() -> list[str]:
    """트리거 없이 항상 주입하는 조회 툴들."""
    return [name for name, tool in REGISTRY.items() if not tool.gated]


def run_tool(ctx: ToolContext, name: str, args: dict) -> str:
    """툴 하나를 실행하고 결과 문자열을 돌려준다.

    실패는 예외로 올리지 않고 문장으로 돌려준다 — 모델이 그걸 읽고 되묻거나
    다른 툴을 부를 수 있어야 대화가 이어진다.
    """
    tool = REGISTRY.get(name)
    if tool is None:
        return f"오류: '{name}' 이라는 툴은 없습니다. 사용 가능한 툴만 부르세요."
    try:
        return tool.handler(ctx, args)
    except ToolError as exc:
        return f"오류: {exc}"
    except Exception as exc:  # 툴 하나가 대화 전체를 죽이면 안 된다
        logger.exception("툴 %r 실행 중 예외 (args=%r)", name, args)
        return f"오류: 툴 실행에 실패했습니다 ({type(exc).__name__}: {exc})"
