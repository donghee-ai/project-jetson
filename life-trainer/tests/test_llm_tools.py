"""lifetrainer.llm.tools / lifetrainer.llm.context 테스트.

네트워크·LLM 없음. 전부 tmp_path 의 임시 SQLite 로 돈다.

이 파일이 지키려는 계약 두 개:
- **의도와 측정을 섞지 않는다** (계약서 §3). 계획은 ①층(시스템), 실측 활동은
  ③층(user 메시지)에 들어가야 한다. 계획만 주입했더니 모델이 "다 했다"고
  지어낸 사고가 실제로 있었다.
- **툴은 예외를 던지지 않는다.** 실패도 문장으로 돌려줘야 모델이 되물을 수 있다.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime

import pytest

from lifetrainer import db, timeutil
from lifetrainer.config import load_config
from lifetrainer.llm import tools as T
from lifetrainer.llm.context import build_day_context, build_turn_context
from lifetrainer.plan import models as plan_models

DAY = "2026-08-17"


@pytest.fixture()
def cfg(tmp_path):
    base = load_config()
    return dataclasses.replace(base, db_path=tmp_path / "lt.db", data_dir=tmp_path)


@pytest.fixture()
def conn(cfg):
    c = db.connect(cfg.db_path)
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture()
def ctx(conn, cfg):
    return T.ToolContext(conn=conn, cfg=cfg, today=DAY, channel="D_TEST")


def _slot(cfg, minute: int) -> int:
    return timeutil.wallclock_min_to_slot(
        minute, cfg.rollup.slot_minutes, boundary_hour=cfg.rollup.day_boundary_hour
    )


def _breakdown(conn, cfg, day: str, minute: int, category: str, seconds: float, app: str = "") -> None:
    """활동 하나를 심는다.

    `compute_daily` 의 `active_sec` 은 `slot` 에서, 카테고리 분해는 `slot_breakdown`
    에서 나온다 — 둘 다 넣어야 실제와 같은 모양이 된다.
    """
    slot = _slot(cfg, minute)
    now = timeutil.now_ts()
    conn.execute(
        "INSERT INTO slot(day, slot, start_ts, category, active_sec, afk_sec, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 0, ?)",
        (day, slot, now, category, seconds, now),
    )
    conn.execute(
        "INSERT INTO slot_breakdown(day, slot, category, app, seconds) VALUES (?, ?, ?, ?, ?)",
        (day, slot, category, app, seconds),
    )
    conn.commit()


# ── 날짜 인자 정규화 ─────────────────────────────────────────────────────


def test_day_생략이면_오늘(ctx):
    assert T._resolve_day(ctx, None) == DAY
    assert T._resolve_day(ctx, "") == DAY


def test_한국어_상대날짜를_받아준다(ctx):
    assert T._resolve_day(ctx, "어제") == "2026-08-16"
    assert T._resolve_day(ctx, "내일") == "2026-08-18"


def test_이상한_날짜는_문장으로_거절한다(ctx):
    assert "날짜 형식" in T.run_tool(ctx, "get_activity_summary", {"day": "언젠가"})


# ── 조회 툴 ──────────────────────────────────────────────────────────────


def test_활동_요약은_집계값을_돌려준다(ctx, conn, cfg):
    _breakdown(conn, cfg, DAY, 600, "coding", 1800, "Code.exe")
    out = T.run_tool(ctx, "get_activity_summary", {"day": DAY})
    assert "coding" in out and "30분" in out


def test_활동이_없으면_없다고_말한다(ctx):
    assert "활동이 없습니다" in T.run_tool(ctx, "get_activity_summary", {"day": DAY})


def test_같은_날_비교는_거절한다(ctx):
    out = T.run_tool(ctx, "compare_days", {"day_a": DAY, "day_b": DAY})
    assert "같습니다" in out


def test_두_날_비교는_증감을_돌려준다(ctx, conn, cfg):
    _breakdown(conn, cfg, "2026-08-16", 600, "coding", 600)
    _breakdown(conn, cfg, DAY, 600, "coding", 1800)
    out = T.run_tool(ctx, "compare_days", {"day_a": "2026-08-16", "day_b": DAY})
    assert "coding" in out and "+" in out


def test_문서_검색어에_특수문자가_있어도_FTS_가_안_깨진다(ctx, conn):
    conn.execute(
        "INSERT INTO doc(kind, url, title, abstract, fetched_at, content_hash, score, state)"
        " VALUES ('paper', 'http://x/1', 'Jetson Orin 추론 최적화', '온디바이스', ?, 'h1', 5.0, 'scored')",
        (timeutil.now_ts(),),
    )
    conn.commit()
    out = T.run_tool(ctx, "search_docs", {"query": 'jetson AND (orin OR "추론"* )'})
    assert "오류" not in out
    assert "Jetson Orin" in out


def test_검색어에_쓸_단어가_없으면_문장으로_거절한다(ctx):
    assert "오류" in T.run_tool(ctx, "search_docs", {"query": "!!! ???"})


def test_없는_툴은_예외가_아니라_문장이다(ctx):
    assert "툴은 없습니다" in T.run_tool(ctx, "존재하지않는툴", {})


# ── 계획 쓰기 ────────────────────────────────────────────────────────────


def test_계획_추가는_인스턴스를_만든다(ctx, conn):
    out = T.run_tool(
        ctx, "add_plan", {"title": "딥워크", "start_min": 540, "end_min": 720, "day": DAY}
    )
    assert "추가했습니다" in out
    rows = plan_models.list_instances(conn, day=DAY)
    assert [r.title for r in rows] == ["딥워크"]
    assert rows[0].source == "chat"


def test_계획_추가에_시각이_없으면_설명을_돌려준다(ctx):
    out = T.run_tool(ctx, "add_plan", {"title": "딥워크"})
    assert "start_min" in out


# ── 예약 알림 ────────────────────────────────────────────────────────────


def test_상대시간_예약(ctx, conn):
    out = T.run_tool(ctx, "schedule_reminder", {"text": "물 마시기", "delay_minutes": 30})
    assert "예약했습니다" in out
    row = conn.execute(
        "SELECT kind, state, not_before FROM job WHERE kind = ?", (T.REMINDER_KIND,)
    ).fetchone()
    assert row["state"] == "queued"
    assert row["not_before"] > timeutil.now_ts()


def test_지난_시각은_다음날로_넘어간다(ctx):
    """밤 11시에 '8시에 알려줘'는 17시간 전이 아니라 내일 아침이다."""
    when = T._resolve_when(ctx, {"at_time": "00:01"})
    assert when > timeutil.now_ts()


def test_예약_한도를_넘으면_거절한다(ctx):
    out = T.run_tool(
        ctx, "schedule_reminder", {"text": "먼 미래", "delay_minutes": (T.MAX_REMINDER_DAYS + 1) * 1440}
    )
    assert "오류" in out


def test_시각도_지연도_없으면_거절한다(ctx):
    assert "하나는 있어야" in T.run_tool(ctx, "schedule_reminder", {"text": "뭔가"})


def test_예약_조회와_취소(ctx, conn):
    T.run_tool(ctx, "schedule_reminder", {"text": "회의", "delay_minutes": 60})
    listed = T.run_tool(ctx, "list_reminders", {})
    assert "회의" in listed

    job_id = conn.execute("SELECT id FROM job WHERE kind = ?", (T.REMINDER_KIND,)).fetchone()["id"]
    assert "취소했습니다" in T.run_tool(ctx, "cancel_reminder", {"reminder_id": job_id})
    assert conn.execute("SELECT state FROM job WHERE id = ?", (job_id,)).fetchone()["state"] == "cancelled"
    assert "없습니다" in T.run_tool(ctx, "list_reminders", {})


def test_이미_취소된_것은_다시_취소되지_않는다(ctx, conn):
    T.run_tool(ctx, "schedule_reminder", {"text": "회의", "delay_minutes": 60})
    job_id = conn.execute("SELECT id FROM job WHERE kind = ?", (T.REMINDER_KIND,)).fetchone()["id"]
    T.run_tool(ctx, "cancel_reminder", {"reminder_id": job_id})
    assert "찾지 못했습니다" in T.run_tool(ctx, "cancel_reminder", {"reminder_id": job_id})


def test_예약은_대화가_일어난_채널로_간다(ctx, conn):
    """알림 대상은 모델 인자가 아니라 대화 자리에서 정해져야 한다."""
    import json

    T.run_tool(ctx, "schedule_reminder", {"text": "회의", "delay_minutes": 60})
    payload = json.loads(
        conn.execute("SELECT payload_json FROM job WHERE kind = ?", (T.REMINDER_KIND,)).fetchone()[0]
    )
    assert payload["channel"] == "D_TEST"


# ── 컨텍스트 3층 배치 ────────────────────────────────────────────────────


def test_계획은_시스템층에_의도로_표시된다(conn, cfg):
    plan_models.add_instance(conn, cfg, DAY, title="딥워크", start_min=540, end_min=720)
    prompt = build_day_context(conn, cfg, DAY).to_prompt()
    assert "사람이 세운 의도" in prompt
    assert "딥워크" in prompt


def test_계획이_없으면_없다고_명시한다(conn, cfg):
    assert "등록된 계획 없음" in build_day_context(conn, cfg, DAY).to_prompt()


def test_시스템층에는_실측이_들어가지_않는다(conn, cfg):
    """실측은 10분마다 변한다 — ①층에 넣으면 하루 종일 프롬프트 캐시가 깨진다."""
    _breakdown(conn, cfg, DAY, 600, "coding", 1800)
    assert "기계 측정" not in build_day_context(conn, cfg, DAY).to_prompt()


def test_턴_컨텍스트에_실측이_들어간다(conn, cfg):
    _breakdown(conn, cfg, DAY, 600, "coding", 1800)
    turn = build_turn_context(conn, cfg, DAY)
    assert "기계 측정" in turn
    assert "coding" in turn


def test_지금_계획에_남은_시간이_숫자로_들어간다(conn, cfg):
    """★ 남은 시간은 **코드가 계산해서 적는다.**

    계획 시간대(20:00~22:00)와 현재 시각만 주고 빼기를 시켰더니 8B 가 세 번 연속
    틀렸다 — 20:19 에 "20분", 20:20 에 "40분", 21:04 에 "46분" 남았다고 했다.
    시간이 갈수록 남은 시간이 늘어났다. 계약서의 "모델에게 산술을 시키지 않는다".
    """
    from lifetrainer import timeutil

    plan_models.add_instance(conn, cfg, DAY, title="문서 정리", start_min=20 * 60, end_min=22 * 60)
    at = timeutil.to_ts(datetime(2026, 8, 17, 21, 4, tzinfo=cfg.tz))
    turn = build_turn_context(conn, cfg, DAY, now=at)

    assert "20:00~22:00" in turn
    assert "56분 남음" in turn


def test_계획을_묻지_않은_말에는_지금_계획을_안_싣는다(conn, cfg):
    """★ 잡담에 계획이 실려 있으면 8B 가 그걸 답으로 삼는다.

    실측: "심심해" 에 "지금은 문서 정리 시간입니다" 를 읊었다(2026-08-22).
    프롬프트로 "먼저 꺼내지 마라"를 넣어도 줄어들 뿐 안 없어져서 구조로 막았다.
    """
    from lifetrainer import timeutil

    plan_models.add_instance(conn, cfg, DAY, title="문서 정리", start_min=20 * 60, end_min=22 * 60)
    at = timeutil.to_ts(datetime(2026, 8, 17, 21, 4, tzinfo=cfg.tz))

    assert "문서 정리" not in build_turn_context(conn, cfg, DAY, now=at, user_text="심심해")
    assert "문서 정리" not in build_turn_context(
        conn, cfg, DAY, now=at, user_text="파이썬 리스트 정렬 어떻게 해?"
    )


@pytest.mark.parametrize(
    "text", ["지금 뭐 해야 해?", "오늘 계획 뭐였지?", "얼마나 남았어?", "지금 상태 어때?"]
)
def test_계획을_묻는_말에는_그대로_싣는다(conn, cfg, text):
    from lifetrainer import timeutil

    plan_models.add_instance(conn, cfg, DAY, title="문서 정리", start_min=20 * 60, end_min=22 * 60)
    at = timeutil.to_ts(datetime(2026, 8, 17, 21, 4, tzinfo=cfg.tz))
    assert "문서 정리" in build_turn_context(conn, cfg, DAY, now=at, user_text=text)


def test_사용자_말이_비면_예전처럼_싣는다(conn, cfg):
    """`user_text` 를 안 넘기는 호출부(리포트 등)의 동작을 바꾸지 않는다."""
    from lifetrainer import timeutil

    plan_models.add_instance(conn, cfg, DAY, title="문서 정리", start_min=20 * 60, end_min=22 * 60)
    at = timeutil.to_ts(datetime(2026, 8, 17, 21, 4, tzinfo=cfg.tz))
    assert "문서 정리" in build_turn_context(conn, cfg, DAY, now=at)


def test_계획_시간대_밖이면_지금_계획을_안_싣는다(conn, cfg):
    from lifetrainer import timeutil

    plan_models.add_instance(conn, cfg, DAY, title="문서 정리", start_min=20 * 60, end_min=22 * 60)
    at = timeutil.to_ts(datetime(2026, 8, 17, 22, 30, tzinfo=cfg.tz))
    assert "지금 시간대 계획" not in build_turn_context(conn, cfg, DAY, now=at)


def test_계획_수는_상한을_넘지_않는다(conn, cfg):
    from lifetrainer.llm.context import MAX_PLANS

    for i in range(MAX_PLANS + 3):
        plan_models.add_instance(
            conn, cfg, DAY, title=f"계획{i}", start_min=i * 20, end_min=i * 20 + 10
        )
    day_ctx = build_day_context(conn, cfg, DAY)
    assert len(day_ctx.plans) == MAX_PLANS
    assert day_ctx.truncated_plans == 3
    assert "생략" in day_ctx.to_prompt()


def test_빈손이면_대체하지_않고_다시_찾을_재료를_준다(ctx, conn):
    """예전에는 관심사 상위 문서로 **대체**했다.

    그랬더니 "시립도서관 이번 주 휴관일" 같은 무관한 질의에도 젯슨 문서를 들이밀어,
    모델이 그걸 성실히 설명하는 엉뚱한 답이 나왔다(환각은 아니고 폴백 설계 문제였다).
    이제는 없으면 없다고 하고, 관심사 낱말만 힌트로 준다.
    """
    conn.execute(
        "INSERT INTO doc(kind, url, title, abstract, fetched_at, content_hash, score, state)"
        " VALUES ('paper', 'http://x/2', 'Jetson 추론 최적화', 'edge', ?, 'h2', 9.0, 'scored')",
        (timeutil.now_ts(),),
    )
    conn.execute(
        "INSERT INTO interest(term, weight, source, updated_at) VALUES ('jetson', 3.0, 'manual', ?)",
        (timeutil.now_ts(),),
    )
    conn.commit()

    out = T.run_tool(ctx, "search_docs", {"query": "시립도서관 이번 주 휴관일"})
    assert "검색된 문서가 없습니다" in out
    assert "Jetson" not in out          # 무관한 문서를 들이밀지 않는다
    assert "jetson" in out              # 다시 찾을 낱말은 알려준다


def test_관심사가_없으면_힌트도_없다(ctx):
    out = T.run_tool(ctx, "search_docs", {"query": "아무거나"})
    assert "검색된 문서가 없습니다" in out
    assert "다시 검색" not in out


# ── 웹 출처 라벨 ─────────────────────────────────────────────────────────
#
# 모델이 구글 "검색어" 트렌드를 읽고 "유튜브 급상승 1위" 라고 답한 사고가 있었다.
# 데이터도 링크도 진짜인데 **그게 무엇인지에 대한 주장**이 틀렸다. 프롬프트로는
# 안 잡혔고(온도를 낮추면 오히려 악화) 결과 문자열에 라벨을 박아서 해결했다.


def test_아는_출처는_한국어_라벨이_붙는다():
    assert "유튜브 급상승이 아님" in T._source_label("https://trends.google.co.kr/trending/rss?geo=KR")
    assert T._source_label("https://github.com/trending") == "GitHub 트렌딩 저장소"
    assert T._source_label("https://news.ycombinator.com/") == "Hacker News 프론트페이지"


def test_모르는_출처는_기본_라벨():
    assert T._source_label("https://example.com/a") == "웹 페이지"


def test_fetch_url_결과에_라벨과_출처가_함께_담긴다(ctx, monkeypatch):
    from lifetrainer.llm import webfetch

    monkeypatch.setattr(
        webfetch,
        "fetch_page",
        lambda cfg, conn, url, **kw: webfetch.FetchedPage(
            url="https://github.com/trending", title="Trending", text="본문", truncated=False
        ),
    )
    out = T.run_tool(ctx, "fetch_url", {"url": "https://github.com/trending"})
    assert "GitHub 트렌딩 저장소" in out
    assert "https://github.com/trending" in out


def test_내부_주소는_툴_단계에서도_거절된다(ctx):
    out = T.run_tool(ctx, "fetch_url", {"url": "http://127.0.0.1:8080/v1/models"})
    assert "오류" in out and "공개 인터넷" in out


def test_스킴이_없으면_https_로_붙인다(ctx, monkeypatch):
    from lifetrainer.llm import webfetch

    seen = {}

    def _fake(cfg, conn, url, **kw):
        seen["url"] = url
        return webfetch.FetchedPage(url=url, title="", text="x", truncated=False)

    monkeypatch.setattr(webfetch, "fetch_page", _fake)
    T.run_tool(ctx, "fetch_url", {"url": "example.com/a"})
    assert seen["url"] == "https://example.com/a"


# ── 다른 날을 물었을 때 (2026-08-23 실측 사고) ─────────────────────────────
# ★ "내일 계획 뭐 있어?" 에 8B 가 get_plans 를 안 부르고 시스템 프롬프트의
#   `[오늘 … 계획 — 사람이 세운 의도]` 블록을 그대로 베껴 답했다. 내일 계획이
#   DB 에 있는데 오늘 것을 읊었고 프롬프트 라벨까지 같이 나갔다.
#   고치는 방향은 툴 강제가 아니라 **물어본 날 계획을 눈앞에 놓는 것**이다.


def test_다른_날을_가리키면_그_날짜를_돌려준다():
    from lifetrainer.llm.context import referenced_day as R

    assert R("2026-08-23", "내일 계획 뭐 있어?") == "2026-08-24"
    assert R("2026-08-23", "어제 계획") == "2026-08-22"
    assert R("2026-08-23", "모레 일정") == "2026-08-25"
    assert R("2026-08-23", "8월 24일에 뭐 하기로 했지?") == "2026-08-24"
    assert R("2026-08-23", "2026-08-25 일정") == "2026-08-25"


def test_오늘과_무관한_말에는_안_걸린다():
    """오늘은 이미 시스템 프롬프트에 실려 있다. 또 실으면 토큰만 는다."""
    from lifetrainer.llm.context import referenced_day as R

    assert R("2026-08-23", "오늘 뭐 했지?") is None
    assert R("2026-08-23", "젯슨 논문 있어?") is None
    assert R("2026-08-23", "2026-08-23 일정") is None  # 오늘이면 None


def test_물어본_날의_계획이_턴_배경에_실린다(conn, cfg):
    """이게 없으면 8B 가 오늘 블록을 베낀다."""
    from lifetrainer.llm.context import build_turn_context

    plan_models.add_instance(
        conn, cfg, "2026-08-24", title="딥워크 코딩", start_min=540, end_min=720
    )
    out = build_turn_context(conn, cfg, "2026-08-23", user_text="내일 계획 뭐 있어?")
    assert "2026-08-24" in out and "딥워크 코딩" in out


def test_그_날_계획이_없으면_없다고_싣는다(conn, cfg):
    """빈손을 말 안 하면 모델이 오늘 것으로 메운다."""
    from lifetrainer.llm.context import build_turn_context

    out = build_turn_context(conn, cfg, "2026-08-23", user_text="모레 계획 뭐 있어?")
    assert "2026-08-25" in out and "등록된 계획 없음" in out


def test_get_plans_는_상태를_사람_말로_싣는다(ctx, conn, cfg):
    """★ 두 경로가 같은 낱말을 써야 한다.

    이 툴이 `todo` 를 영어로 실었고, `llm/context` 는 같은 상태를 "예정" 으로
    싣고 있었다. 같은 대화 안에서 한 층은 영어, 다른 층은 한국어였던 셈이라
    8B 가 `todo` 를 "진행 중" 으로 옮겼다.
    """
    from lifetrainer.llm.context import STATUS_KO

    plan_models.add_instance(conn, cfg, DAY, title="안한것", start_min=540, end_min=600)
    out = T.run_tool(ctx, "get_plans", {})
    assert STATUS_KO["todo"] in out
    assert "todo" not in out


def test_오늘만_물으면_다른_날_블록이_안_붙는다(conn, cfg):
    """★ `now` 를 반드시 고정한다. 안 그러면 **00:00~06:00 에만 깨진다.**

    `build_turn_context` 의 첫 줄이 "(현재 <벽시계>, 논리적 하루 <day>)" 인데,
    하루 경계가 06:00 이라 그 시간대에는 벽시계 날짜가 논리적 하루보다 하루 앞선다.
    그래서 "2026-08-24 가 없어야 한다"는 이 단언이 새벽에만 실패했다 —
    테스트가 실제 시각에 의존하고 있었다.
    """
    from lifetrainer.llm.context import build_turn_context

    noon = timeutil.to_ts(datetime(2026, 8, 23, 12, 0, tzinfo=cfg.tz))
    out = build_turn_context(conn, cfg, "2026-08-23", now=noon, user_text="오늘 뭐 했지?")
    assert "2026-08-24" not in out


# ── 프롬프트 뼈대 유출 안전망 ──────────────────────────────────────────────


def test_뼈대가_섞인_답을_잡아낸다():
    from lifetrainer.llm.converse import _has_scaffolding

    assert _has_scaffolding("[오늘 2026-08-23 계획 — 사람이 세운 의도] 12:00 식사")
    assert _has_scaffolding("[오늘 실제 활동 — 기계 측정] 총 3시간")
    assert _has_scaffolding("[배경 — 참고용. 묻지 않았으면 먼저 꺼내지 마라]\n뭐라뭐라")


def test_정상_답을_오탐하지_않는다():
    """출처 표기 `[1](url)` 은 흔하다. 이걸 잡으면 멀쩡한 답이 죽는다."""
    from lifetrainer.llm.converse import _has_scaffolding

    assert not _has_scaffolding("내일은 딥워크 코딩이 있습니다. [1](https://x)")
    assert not _has_scaffolding("[참고] 이건 그냥 대괄호입니다")


def test_뼈대는_줄_통째로_걷어낸다():
    """라벨만 떼면 남은 값이 답인 척한다 — 오늘 계획이 내일 답으로 둔갑한다."""
    from lifetrainer.llm.converse import _strip_scaffolding

    got = _strip_scaffolding(
        "[오늘 2026-08-23 계획 — 사람이 세운 의도] 12:00~13:00 식사\n내일은 딥워크 코딩입니다."
    )
    assert got == "내일은 딥워크 코딩입니다."
    assert "12:00" not in got
