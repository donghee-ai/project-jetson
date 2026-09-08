"""lifetrainer.slackio.commands / lifetrainer.slackio.app(신규 명령) 테스트.

`commands.py` 는 Slack 을 전혀 모르는 순수 파서라 직접 호출로 검증한다.
`app.py` 의 새 슬래시 명령들은 Bolt 라우팅(Socket Mode 연결)을 거치지 않고
`_dispatch_*` 함수를 가짜 `ack`/`respond`/`body`/`action` 으로 직접 호출해 검증한다
(계약서-v2 §5: "Slack 으로 아무것도 발송하지 마라" — 네트워크 호출 금지).

★ `load_config()` 는 이 실제 기기의 `~/.openclaw/openclaw.json` 에 있는 **진짜**
Slack 봇 토큰을 자동으로 채워 넣을 수 있다(`config/lifetrainer.toml` 의
`bot_token=""` + `openclaw_config` 폴백). 그 토큰이 테스트에 새어 들어와 실수로도
실제 Slack API 를 두드리지 않도록, `cfg` 픽스처에서 `slack` 을 항상 안전한 값
(빈 토큰 + 존재하지 않는 openclaw 경로)으로 덮어쓴다 — `tests/test_cli.py`/
`tests/test_cli_plan.py` 와 같은 방어다. `_build_notifier` 도 monkeypatch 로
가짜 client 를 주입해 이중으로 막는다.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from lifetrainer import db, timeutil
from lifetrainer.config import SlackConfig, load_config
from lifetrainer.plan import models as plan_models
from lifetrainer.plan.subjects import create_subject, palette_choices
from lifetrainer.slackio import app as app_mod
from lifetrainer.slackio.commands import ParsedTask, parse_index_list, parse_plan_text
from lifetrainer.slackio.notify import SlackNotifier


# ── 픽스처 ────────────────────────────────────────────────────────────


@pytest.fixture()
def cfg(tmp_path):
    base = load_config()
    safe_slack = SlackConfig(
        mode="bolt",
        bot_token="",
        app_token="",
        default_channel="C_TEST",
        openclaw_config=Path("/nonexistent/openclaw.json"),
    )
    return dataclasses.replace(
        base,
        db_path=tmp_path / "lt.db",
        report=dataclasses.replace(base.report, png_dir=tmp_path / "png"),
        slack=safe_slack,
    )


@pytest.fixture()
def conn(cfg):
    c = db.connect(cfg.db_path)
    db.init_db(c)
    yield c
    c.close()


class FakeWebClient:
    """test_notify.py 와 같은 모양의 가짜 클라이언트 — 실제 네트워크 호출 없음."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.files_upload_v2_result = {"ok": True, "file": {"id": "F123"}}
        self.chat_postMessage_result = {"ok": True, "ts": "1000.001"}

    def conversations_open(self, **kwargs):
        self.calls.append(("conversations_open", kwargs))
        return {"channel": {"id": "D999"}}

    def files_upload_v2(self, **kwargs):
        self.calls.append(("files_upload_v2", kwargs))
        return self.files_upload_v2_result

    def chat_postMessage(self, **kwargs):
        self.calls.append(("chat_postMessage", kwargs))
        return self.chat_postMessage_result

    @property
    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]


class RespondRecorder:
    """Bolt 의 `respond()` 를 흉내낸다 — 위치 인자(text) 와 키워드 인자를 모두 기록한다."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        payload = dict(kwargs)
        if args:
            payload.setdefault("text", args[0])
        self.calls.append(payload)

    @property
    def texts(self) -> list[str]:
        return [c.get("text", "") for c in self.calls]


def _noop_ack() -> None:
    return None


def _today(cfg) -> str:
    return timeutil.day_str(timeutil.now_ts(), cfg.tz, boundary_hour=cfg.rollup.day_boundary_hour)


def _wired_notifier_factory(fake_client: FakeWebClient):
    """`app._build_notifier` 를 대체할 팩토리 — 가짜 client 를 주입하고 `enabled=True` 로 만든다.

    `SlackNotifier.enabled` 는 `cfg.slack.bot_token` 이 비어 있지 않아야 True 다.
    테스트 `cfg` 는 실제 토큰 유출을 막기 위해 항상 `bot_token=""` 로 두므로,
    여기서만 리터럴 더미 토큰("xoxb-test")으로 바꿔치기해 발송 경로를 켠다 —
    실제 네트워크는 `.client` 가 `FakeWebClient` 이므로 절대 열리지 않는다.
    """

    def _factory(cfg_arg):
        fake_cfg = dataclasses.replace(cfg_arg, slack=dataclasses.replace(cfg_arg.slack, bot_token="xoxb-test"))
        notifier = SlackNotifier(fake_cfg)
        notifier.client = fake_client
        return notifier

    return _factory


# ═════════════════════════════════════════════════════════════════════
# commands.py — 순수 파서
# ═════════════════════════════════════════════════════════════════════


def test_시각범위를_at_으로_감싸도_제목에_기호가_안_남는다():
    """`@09:00-11:00` — 시각 범위가 먼저 떨어져 나가고 `@` 만 남아 제목에 붙었다.

    에이전트가 실제로 이렇게 썼다(`@` 는 기간 토큰인데 시각 범위에 얹었다).
    시각은 제대로 들어갔고 제목만 "딥워크 @" 가 됐다 — 사람이 `/plan 회의 #` 를
    쳐도 같은 자리다.
    """
    from lifetrainer.slackio.commands import parse_plan_text

    task = parse_plan_text("딥워크 @09:00-11:00")[0]
    assert task.title == "딥워크"
    assert (task.start_min, task.end_min) == (540, 660)

    assert parse_plan_text("회의 #")[0].title == "회의"
    assert parse_plan_text("정리 !")[0].title == "정리"


def test_parse_plan_text_plain_single_task():
    tasks = parse_plan_text("저녁 약속가기")
    assert tasks == [ParsedTask(title="저녁 약속가기", subject=None, minutes=None, priority="normal", start_min=None, end_min=None)]


def test_parse_plan_text_empty_returns_empty_list():
    assert parse_plan_text("") == []
    assert parse_plan_text("   ") == []


def test_parse_plan_text_numbered_list_multiline():
    tasks = parse_plan_text("1. 첫째\n2. 둘째")
    assert [t.title for t in tasks] == ["첫째", "둘째"]


def test_parse_plan_text_numbered_list_same_line():
    tasks = parse_plan_text("1. 첫째 2. 둘째")
    assert [t.title for t in tasks] == ["첫째", "둘째"]


def test_parse_plan_text_single_numbered_item_still_yields_one_task():
    tasks = parse_plan_text("1. 저녁 약속가기")
    assert len(tasks) == 1
    assert tasks[0].title == "저녁 약속가기"


def test_parse_plan_text_extended_syntax_extracts_and_strips_tokens():
    tasks = parse_plan_text("프로젝트 보고서 #프로젝트 @90m !high")
    assert len(tasks) == 1
    task = tasks[0]
    assert task.title == "프로젝트 보고서"
    assert task.subject == "프로젝트"
    assert task.minutes == 90
    assert task.priority == "high"


def test_parse_plan_text_duration_hours_and_minutes():
    tasks = parse_plan_text("논문 읽기 @1h30m")
    assert tasks[0].minutes == 90
    assert "@1h30m" not in tasks[0].title


def test_parse_plan_text_time_range_sets_start_end_min():
    tasks = parse_plan_text("자료 정리 09:00-12:00")
    assert tasks[0].start_min == 540
    assert tasks[0].end_min == 720
    assert "09:00-12:00" not in tasks[0].title


def test_parse_plan_text_numbered_list_each_item_gets_own_tokens():
    tasks = parse_plan_text("1. 프로젝트 보고서 #프로젝트 @30m\n2. 논문 초록 정리 #리서치 @20m !low")
    assert len(tasks) == 2
    assert tasks[0].subject == "프로젝트" and tasks[0].minutes == 30
    assert tasks[1].subject == "리서치" and tasks[1].minutes == 20 and tasks[1].priority == "low"


def test_parse_plan_text_does_not_mistake_hyphenated_number_for_list_marker():
    # "실습 3-1" 은 "N." 형식이 아니므로 번호 목록으로 오인하면 안 된다.
    tasks = parse_plan_text("실습 3-1 강의수강")
    assert len(tasks) == 1
    assert "3-1" in tasks[0].title


# ── parse_index_list ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("3,5", [3, 5]),
        ("3 5", [3, 5]),
        ("3", [3]),
        ("3,  5 , 7", [3, 5, 7]),
        ("", []),
        ("   ", []),
    ],
)
def test_parse_index_list_forms(text, expected):
    assert parse_index_list(text) == expected


def test_parse_index_list_invalid_token_raises():
    with pytest.raises(ValueError):
        parse_index_list("3,x")


# ═════════════════════════════════════════════════════════════════════
# /plan — 인스턴스가 실제로 DB 에 들어가는지
# ═════════════════════════════════════════════════════════════════════


def test_dispatch_plan_creates_instance(cfg, conn):
    respond = RespondRecorder()
    app_mod._dispatch_plan(cfg, "저녁 약속가기", respond)

    rows = plan_models.list_instances(conn, _today(cfg))
    assert len(rows) == 1
    assert rows[0].title == "저녁 약속가기"
    assert rows[0].status == "todo"
    assert respond.calls  # ephemeral 확인 응답이 갔다


def test_dispatch_plan_numbered_list_creates_multiple_instances(cfg, conn):
    respond = RespondRecorder()
    app_mod._dispatch_plan(cfg, "1. 첫째\n2. 둘째", respond)

    rows = plan_models.list_instances(conn, _today(cfg))
    assert sorted(r.title for r in rows) == ["둘째", "첫째"]


def test_dispatch_plan_extended_syntax_sets_priority_and_time_range(cfg, conn):
    respond = RespondRecorder()
    app_mod._dispatch_plan(cfg, "자료 정리 09:00-12:00 !high", respond)

    rows = plan_models.list_instances(conn, _today(cfg))
    assert len(rows) == 1
    assert rows[0].start_min == 540
    assert rows[0].end_min == 720
    assert rows[0].priority == "high"


def test_dispatch_plan_resolves_existing_subject(cfg, conn):
    hex_color = palette_choices(cfg)[0]["hex"]
    subject_id = create_subject(conn, cfg, name="프로젝트", color=hex_color)

    respond = RespondRecorder()
    # 시각 범위를 명시해 슬롯 스냅(10분 단위 반올림)에 의한 흔들림 없이 정확한
    # 길이를 검증한다 — @<기간> 만 쓰면 기본 시작이 "지금"이라 스냅 경계에서
    # 테스트 실행 시각에 따라 길이가 흔들릴 수 있다(이는 add_instance 의 정상
    # 동작이지 버그가 아니다 — 계약서 §3, plan/models.py `_snap_to_slot`).
    app_mod._dispatch_plan(cfg, "실습 과제 #프로젝트 09:00-09:40", respond)

    rows = plan_models.list_instances(conn, _today(cfg))
    assert len(rows) == 1
    assert rows[0].subject_id == subject_id
    assert (rows[0].start_min, rows[0].end_min) == (540, 580)


def test_dispatch_plan_empty_text_shows_usage_without_writing(cfg, conn):
    respond = RespondRecorder()
    app_mod._dispatch_plan(cfg, "", respond)

    assert plan_models.list_instances(conn, _today(cfg)) == []
    assert "사용법" in respond.texts[0]


# ═════════════════════════════════════════════════════════════════════
# /done · /doing · /defer
# ═════════════════════════════════════════════════════════════════════


def _seed_three_instances(conn, cfg) -> list[int]:
    day = _today(cfg)
    ids = [
        plan_models.add_instance(conn, cfg, day, title="첫째", start_min=540, end_min=600),
        plan_models.add_instance(conn, cfg, day, title="둘째", start_min=600, end_min=660),
        plan_models.add_instance(conn, cfg, day, title="셋째", start_min=660, end_min=720),
    ]
    return ids


def test_dispatch_done_changes_status_of_ordinal_3(cfg, conn):
    _seed_three_instances(conn, cfg)
    day = _today(cfg)

    respond = RespondRecorder()
    app_mod._dispatch_set_status(cfg, "done", "3", respond, "U1")

    rows = {r.ordinal: r for r in plan_models.list_instances(conn, day)}
    assert rows[3].status == "done"
    assert rows[1].status == "todo"
    assert "완료" in respond.texts[0]


def test_dispatch_doing_changes_status_of_ordinal(cfg, conn):
    _seed_three_instances(conn, cfg)
    day = _today(cfg)

    respond = RespondRecorder()
    app_mod._dispatch_set_status(cfg, "doing", "2", respond, "U1")

    rows = {r.ordinal: r for r in plan_models.list_instances(conn, day)}
    assert rows[2].status == "doing"


def test_dispatch_defer_creates_next_day_instance(cfg, conn):
    _seed_three_instances(conn, cfg)
    day = _today(cfg)
    from datetime import date, timedelta

    next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()

    respond = RespondRecorder()
    app_mod._dispatch_set_status(cfg, "defer", "3", respond, "U1")

    today_rows = {r.ordinal: r for r in plan_models.list_instances(conn, day)}
    assert today_rows[3].status == "deferred"

    tomorrow_rows = plan_models.list_instances(conn, next_day)
    assert len(tomorrow_rows) == 1
    assert tomorrow_rows[0].title == "셋째"
    assert tomorrow_rows[0].carried_from == today_rows[3].id
    assert tomorrow_rows[0].source == "carry"
    assert "연기" in respond.texts[0]


def test_dispatch_set_status_unknown_ordinal_reports_not_found(cfg, conn):
    _seed_three_instances(conn, cfg)
    respond = RespondRecorder()
    app_mod._dispatch_set_status(cfg, "done", "99", respond, "U1")
    assert "찾을 수 없습니다" in respond.texts[0]


# ═════════════════════════════════════════════════════════════════════
# /del — 목록에서 완료 항목 숨김, 번호 삭제 + 실행취소
# ═════════════════════════════════════════════════════════════════════


def test_dispatch_del_list_hides_done_items(cfg, conn):
    ids = _seed_three_instances(conn, cfg)
    plan_models.set_status(conn, cfg, ids[0], "done", actor="user")

    respond = RespondRecorder()
    app_mod._dispatch_del(cfg, {"text": ""}, respond)

    assert len(respond.calls) == 1
    blocks = respond.calls[0]["blocks"]
    checkbox_blocks = [b for b in blocks if b.get("block_id") == "del_picker"]
    assert len(checkbox_blocks) == 1
    options = checkbox_blocks[0]["elements"][0]["options"]
    # 완료된 "첫째" 는 숨겨지고, 나머지 둘만 보여야 한다.
    option_texts = [o["text"]["text"] for o in options]
    assert not any("첫째" in t for t in option_texts)
    assert any("둘째" in t for t in option_texts)
    assert any("셋째" in t for t in option_texts)


def test_dispatch_del_empty_day_reports_nothing_to_delete(cfg, conn):
    respond = RespondRecorder()
    app_mod._dispatch_del(cfg, {"text": ""}, respond)
    assert "없습니다" in respond.texts[0]


def test_dispatch_del_by_number_archives_and_undo_revives(cfg, conn):
    ids = _seed_three_instances(conn, cfg)
    day = _today(cfg)

    respond = RespondRecorder()
    app_mod._dispatch_del(cfg, {"text": "3"}, respond)

    rows = plan_models.list_instances(conn, day, include_archived=True)
    archived = {r.id: r for r in rows}[ids[2]]
    assert archived.archived is True
    # 삭제 후에는 기본 목록(비삭제)에서 사라져야 한다.
    assert ids[2] not in {r.id for r in plan_models.list_instances(conn, day)}

    undo_calls = [c for c in respond.calls if "blocks" in c]
    assert undo_calls
    undo_button = undo_calls[-1]["blocks"][-1]["elements"][0]
    assert undo_button["action_id"] == "del_undo"
    assert undo_button["value"] == str(ids[2])

    # 실행취소 버튼 클릭을 흉내낸다.
    undo_respond = RespondRecorder()
    app_mod._dispatch_del_undo(cfg, {"value": undo_button["value"]}, undo_respond)

    revived = {r.id: r for r in plan_models.list_instances(conn, day)}[ids[2]]
    assert revived.archived is False


def test_dispatch_del_by_multiple_numbers(cfg, conn):
    ids = _seed_three_instances(conn, cfg)
    day = _today(cfg)

    respond = RespondRecorder()
    app_mod._dispatch_del(cfg, {"text": "1,2"}, respond)

    remaining = {r.id for r in plan_models.list_instances(conn, day)}
    assert ids[0] not in remaining
    assert ids[1] not in remaining
    assert ids[2] in remaining


def test_dispatch_del_unknown_number_reports_missing(cfg, conn):
    _seed_three_instances(conn, cfg)
    respond = RespondRecorder()
    app_mod._dispatch_del(cfg, {"text": "99"}, respond)
    assert "찾지 못했습니다" in respond.texts[0]


def test_dispatch_del_confirm_action_archives_selected_checkboxes(cfg, conn):
    ids = _seed_three_instances(conn, cfg)
    day = _today(cfg)

    body = {
        "state": {
            "values": {
                "del_picker": {
                    "del_select": {
                        "selected_options": [
                            {"value": str(ids[1])},
                        ]
                    }
                }
            }
        }
    }
    respond = RespondRecorder()
    app_mod._dispatch_del_confirm(cfg, body, respond)

    remaining = {r.id for r in plan_models.list_instances(conn, day)}
    assert ids[1] not in remaining
    assert ids[0] in remaining and ids[2] in remaining
    assert "삭제했습니다" in respond.texts[0]


def test_dispatch_del_confirm_no_selection_reports_error(cfg, conn):
    body = {"state": {"values": {}}}
    respond = RespondRecorder()
    app_mod._dispatch_del_confirm(cfg, body, respond)
    assert "선택된 항목이 없습니다" in respond.texts[0]


# ═════════════════════════════════════════════════════════════════════
# /memo — day.memo upsert
# ═════════════════════════════════════════════════════════════════════


def test_dispatch_memo_upserts_day_row(cfg, conn):
    respond = RespondRecorder()
    app_mod._dispatch_memo(cfg, "신청 작업 장바구니 담기", respond)

    day = _today(cfg)
    row = conn.execute("SELECT memo FROM day WHERE day = ?", (day,)).fetchone()
    assert row["memo"] == "신청 작업 장바구니 담기"

    # 같은 날 다시 부르면 upsert 되어야 한다(행이 늘지 않는다).
    app_mod._dispatch_memo(cfg, "메모 수정", respond)
    rows = conn.execute("SELECT memo FROM day WHERE day = ?", (day,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["memo"] == "메모 수정"


def test_dispatch_memo_empty_text_shows_usage(cfg, conn):
    respond = RespondRecorder()
    app_mod._dispatch_memo(cfg, "", respond)
    day = _today(cfg)
    row = conn.execute("SELECT memo FROM day WHERE day = ?", (day,)).fetchone()
    assert row is None
    assert "사용법" in respond.texts[0]


# ═════════════════════════════════════════════════════════════════════
# /view · /week — Slack 발송 경로는 가짜 client 로 (네트워크 0건)
# ═════════════════════════════════════════════════════════════════════


def test_dispatch_view_uploads_png_and_posts_card(cfg, conn, monkeypatch):
    fake_client = FakeWebClient()
    monkeypatch.setattr(app_mod, "_build_notifier", _wired_notifier_factory(fake_client))

    plan_models.add_instance(conn, cfg, _today(cfg), title="아침 운동", start_min=420, end_min=450)

    respond = RespondRecorder()
    app_mod._dispatch_view(cfg, {"text": "", "channel_id": "C1"}, respond)

    assert fake_client.call_names == ["files_upload_v2", "chat_postMessage"]
    posted_blocks = fake_client.calls[-1][1]["blocks"]
    image_blocks = [b for b in posted_blocks if b["type"] == "image"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["slack_file"] == {"id": "F123"}


def test_dispatch_view_invalid_date_reports_error_without_calling_slack(cfg, conn, monkeypatch):
    fake_client = FakeWebClient()
    monkeypatch.setattr(app_mod, "_build_notifier", _wired_notifier_factory(fake_client))

    respond = RespondRecorder()
    app_mod._dispatch_view(cfg, {"text": "안녕", "channel_id": "C1"}, respond)

    assert fake_client.calls == []
    assert "해석할 수 없습니다" in respond.texts[0]


def test_dispatch_view_yesterday_alias_resolves(cfg):
    day = app_mod._resolve_view_day(cfg, "어제")
    today = _today(cfg)
    from datetime import date, timedelta

    assert day == (date.fromisoformat(today) - timedelta(days=1)).isoformat()


def test_dispatch_week_posts_report_via_notifier(cfg, conn, monkeypatch):
    fake_client = FakeWebClient()
    monkeypatch.setattr(app_mod, "_build_notifier", _wired_notifier_factory(fake_client))

    respond = RespondRecorder()
    app_mod._dispatch_week(cfg, {"channel_id": "C1"}, respond)

    # PNG 유무와 무관하게 최소 chat_postMessage 는 호출되어야 한다.
    assert "chat_postMessage" in fake_client.call_names
