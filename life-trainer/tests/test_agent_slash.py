"""`lifetrainer.agent.slash` — Slack 없이 "/" 명령을 실행하는 통로.

이 파일이 지키려는 계약:

- **발송 경로가 없다.** 에이전트가 `/view`·`/week` 을 불러도 Slack 으로 아무것도
  안 나간다. 프롬프트로 부탁하는 것이 아니라 봇 토큰을 비운 사본으로 부르기
  때문이다 — 그 구조가 실제로 성립하는지를 본다.
- **명령 파싱을 두 번 쓰지 않는다.** `/plan` 확장 문법은 `slackio.commands` 의
  것이어야 한다. 여기서 다시 파싱하면 두 경로가 조용히 갈린다.
- **보여 주는 번호와 조작하는 번호가 같다.** `/view` 가 3번이라고 한 것을
  `/done 3` 이 바꿔야 한다.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime

import pytest

from lifetrainer import db, timeutil
from lifetrainer.agent import slash as SL
from lifetrainer.config import load_config
from lifetrainer.plan import models as plan_models

DAY = "2026-08-17"


@pytest.fixture()
def cfg(tmp_path):
    base = load_config()
    cfg = dataclasses.replace(
        base,
        db_path=tmp_path / "lt.db",
        data_dir=tmp_path,
        slack=dataclasses.replace(base.slack, bot_token="xoxb-살아있는-토큰", default_channel="D_TEST"),
    )
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    conn.close()
    return cfg


@pytest.fixture()
def today(cfg, monkeypatch):
    """시계를 통째로 2026-08-17 12:00 KST 에 세운다.

    ★ **논리적 오늘만 고정하면 모자란다.** `/plan` 에 시각 범위를 안 주면
    `_dispatch_plan` 이 `timeutil.now_ts()` 로 시작 시각을 정한다 — 그래서 낮에
    통과하던 테스트가 새벽 2시에 깨졌다. 이 저장소가 바로 어제 같은 부류를
    기록했는데(`HISTORY/2026-08-24-a-test-that-only-broke-before-dawn.md`)
    그걸 쓰면서 같은 실수를 다시 했다. 시각을 읽는 코드를 부르면 시각을 고정한다.

    ★ **논리적 오늘도 두 군데다.** `/view` 는 `agent.slash._today` 를,
    델리게이트되는 `/plan`·`/done` 은 `slackio.app._today` 를 쓴다. 한쪽만 고정하면
    쓰기는 진짜 오늘에, 읽기는 고정된 날에 가서 **아무 일도 안 일어난 것처럼 보인다.**
    """
    from lifetrainer.slackio import app as slack_app

    frozen = timeutil.to_ts(datetime(2026, 8, 17, 12, 0, tzinfo=cfg.tz))
    monkeypatch.setattr(timeutil, "now_ts", lambda: frozen)
    monkeypatch.setattr(SL, "_today", lambda _cfg: DAY)
    monkeypatch.setattr(slack_app, "_today", lambda _cfg: DAY)
    return DAY


# ── 파싱 ──────────────────────────────────────────────────────────────


def test_parse_splits_command_and_rest():
    assert SL.parse("/plan 과제 #프로젝트 @90m") == ("/plan", "과제 #프로젝트 @90m")


def test_parse_accepts_a_missing_slash():
    """모델이 자주 빠뜨린다. 거부해서 한 턴을 버릴 이유가 없다."""
    assert SL.parse("lt today") == ("/lt", "today")


@pytest.mark.parametrize("bad", ["", "   ", "/nope", "nope"])
def test_parse_refuses_unknown_and_empty(bad):
    with pytest.raises(SL.SlashError):
        SL.parse(bad)


def test_unknown_command_error_lists_the_commands():
    """거부만 하면 모델이 같은 실수를 반복한다. 무엇을 쓸 수 있는지 같이 준다."""
    with pytest.raises(SL.SlashError) as exc:
        SL.parse("/nope")
    for name in ("/plan", "/view", "/done"):
        assert name in str(exc.value)


# ── 발송 차단 ──────────────────────────────────────────────────────────


def test_mute_blanks_the_bot_token(cfg):
    assert cfg.slack.bot_token  # 원본은 살아 있고
    assert SL._mute_slack(cfg).slack.bot_token == ""  # 사본만 죽는다


def test_view_never_builds_a_notifier(cfg, today, monkeypatch):
    """`/view` 는 Slack 업로드 경로를 아예 안 탄다 (PNG 도 안 그린다)."""
    import lifetrainer.slackio.notify as notify_mod

    def explode(*_args, **_kwargs):  # pragma: no cover - 불리면 실패다
        raise AssertionError("에이전트 경로에서 Slack 발송기를 만들면 안 된다")

    monkeypatch.setattr(notify_mod, "SlackNotifier", explode)
    out = SL.run(cfg, "/view")
    assert DAY in out


def test_delegated_command_gets_a_muted_config(cfg, today, monkeypatch):
    """델리게이트되는 명령도 토큰이 비워진 사본을 받아야 한다."""
    seen = {}

    def fake(cfg_arg, _text, respond, _day=None):
        seen["token"] = cfg_arg.slack.bot_token
        respond("ok")

    import lifetrainer.slackio.app as slack_app

    monkeypatch.setattr(slack_app, "_dispatch_memo", fake)
    SL.run(cfg, "/memo 오늘은 맑음")
    assert seen["token"] == ""


# ── 플래너 왕복 ────────────────────────────────────────────────────────


def test_plan_add_then_view_then_done(cfg, today):
    """추가 → 조회 → 완료가 한 줄로 이어져야 한다. 번호는 ordinal 이다."""
    added = SL.run(cfg, "/plan 프로젝트 보고서 09:00-10:30 #프로젝트 !high")
    assert "프로젝트 보고서" in added

    listed = SL.run(cfg, "/view")
    assert "프로젝트 보고서" in listed
    assert "1." in listed  # ordinal 로 번호가 붙는다

    done = SL.run(cfg, "/done 1")
    assert "완료" in done

    conn = db.open_db(cfg)
    try:
        rows = plan_models.list_instances(conn, DAY)
    finally:
        conn.close()
    assert [r.status for r in rows] == ["done"]


# ── 날짜 지정 ──────────────────────────────────────────────────────────
#
# ★ 이 묶음이 없을 때 "내일 09시에 딥워크 넣어줘" 가 **오늘에** 들어갔다.
#   `/plan` 에 날짜를 줄 자리가 없어서 모델이 `@2026-08-25` 처럼 기간 토큰에
#   얹었고, 파싱이 조용히 실패한 뒤 "추가했습니다" 라고 답했다.
#   에러가 안 나는 실패라 툴 이름만 보는 채점으로는 안 잡힌다.


def test_plan_without_a_day_goes_to_today(cfg, today):
    SL.run(cfg, "/plan 오늘것 09:00-10:00")
    conn = db.open_db(cfg)
    try:
        assert [r.title for r in plan_models.list_instances(conn, DAY)] == ["오늘것"]
    finally:
        conn.close()


@pytest.mark.parametrize(("word", "expected"), [("내일", "2026-08-18"), ("2026-08-20", "2026-08-20")])
def test_plan_with_a_day_goes_to_that_day(cfg, today, word, expected):
    SL.run(cfg, "/plan 내일것 09:00-10:00", day=word)
    conn = db.open_db(cfg)
    try:
        assert [r.title for r in plan_models.list_instances(conn, expected)] == ["내일것"]
        assert plan_models.list_instances(conn, DAY) == []
    finally:
        conn.close()


def test_view_speaks_status_in_the_shared_words(cfg, today):
    """★ 영어 토큰을 주면 모델이 자기 식으로 옮긴다.

    `get_plans` 가 `todo` 를 그대로 실었더니 8B 가 **"진행 중"** 이라고 답했다 —
    아직 시작도 안 한 일이다. 낱말은 `llm/context.STATUS_KO` 한 곳에서만 온다.
    """
    from lifetrainer.llm.context import STATUS_KO

    SL.run(cfg, "/plan 안한것 09:00-10:00")
    out = SL.run(cfg, "/view")
    assert STATUS_KO["todo"] in out
    assert "todo" not in out


def test_view_reads_the_day_argument_too(cfg, today):
    SL.run(cfg, "/plan 내일것 09:00-10:00", day="내일")
    assert "내일것" in SL.run(cfg, "/view", day="내일")
    assert "내일것" not in SL.run(cfg, "/view")


# ★ 아래 셋이 없을 때 `/done`·`/del`·`/memo` 가 `day` 를 **조용히 버렸다.**
#   `/plan` 에서 고친 버그를 옆 명령들이 그대로 갖고 있었고, 모델이 `day="내일"` 을
#   보내는데도 **오늘의 1번**이 바뀌었다. 같은 부류를 한 번에 못 잡은 셈이다.


def test_done_changes_the_day_it_was_given(cfg, today):
    SL.run(cfg, "/plan 내일것 09:00-10:00", day="내일")
    SL.run(cfg, "/plan 오늘것 09:00-10:00")
    SL.run(cfg, "/done 1", day="내일")

    conn = db.open_db(cfg)
    try:
        assert [(r.title, r.status) for r in plan_models.list_instances(conn, "2026-08-18")] == [
            ("내일것", "done")
        ]
        assert [(r.title, r.status) for r in plan_models.list_instances(conn, DAY)] == [
            ("오늘것", "todo")
        ]
    finally:
        conn.close()


def test_del_removes_from_the_day_it_was_given(cfg, today):
    SL.run(cfg, "/plan 내일것 09:00-10:00", day="내일")
    SL.run(cfg, "/plan 오늘것 09:00-10:00")
    SL.run(cfg, "/del 1", day="내일")

    conn = db.open_db(cfg)
    try:
        assert plan_models.list_instances(conn, "2026-08-18") == []
        assert [r.title for r in plan_models.list_instances(conn, DAY)] == ["오늘것"]
    finally:
        conn.close()


def test_memo_lands_on_the_day_it_was_given(cfg, today):
    SL.run(cfg, "/memo 내일 메모", day="내일")
    conn = db.open_db(cfg)
    try:
        rows = dict(conn.execute("SELECT day, memo FROM day WHERE memo IS NOT NULL").fetchall())
    finally:
        conn.close()
    assert rows == {"2026-08-18": "내일 메모"}


def test_an_unreadable_day_is_refused_not_stored(cfg, today):
    """'내일' 을 못 읽으면 그 문자열이 `plan_instance.day` 에 들어가고,
    그 계획은 **어느 날짜에도 안 뜬다.** 조용히 사라지느니 거부한다."""
    with pytest.raises(SL.SlashError):
        SL.run(cfg, "/plan 어딘가 09:00-10:00", day="다음주쯤")
    conn = db.open_db(cfg)
    try:
        assert conn.execute("SELECT COUNT(*) FROM plan_instance").fetchone()[0] == 0
    finally:
        conn.close()


def test_plan_uses_the_shared_extension_syntax(cfg, today):
    """`#과목 @기간 !우선순위` 는 `slackio.commands` 의 문법이다."""
    SL.run(cfg, "/plan 딥워크 @90m !high")
    conn = db.open_db(cfg)
    try:
        row = plan_models.list_instances(conn, DAY)[0]
    finally:
        conn.close()
    assert row.title == "딥워크"
    assert row.priority == "high"
    assert row.end_min - row.start_min == 90


def test_view_shows_the_number_that_del_takes(cfg, today):
    SL.run(cfg, "/plan 첫째 09:00-10:00")
    SL.run(cfg, "/plan 둘째 10:00-11:00")
    assert "2. " in SL.run(cfg, "/view")

    SL.run(cfg, "/del 2")
    remaining = SL.run(cfg, "/view")
    assert "첫째" in remaining and "둘째" not in remaining


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("어제", "2026-08-16"),
        ("yesterday", "2026-08-16"),
        ("오늘", DAY),
        ("today", DAY),  # 모델이 실제로 이렇게 부른다 (실측)
        ("내일", "2026-08-18"),
        ("2026-08-10", "2026-08-10"),
    ],
)
def test_view_accepts_the_words_the_model_actually_uses(cfg, today, arg, expected):
    assert expected in SL.run(cfg, f"/view {arg}")


def test_view_refuses_an_unparseable_date(cfg, today):
    with pytest.raises(SL.SlashError):
        SL.run(cfg, "/view 지지난주쯤")


def test_empty_day_says_so_instead_of_going_quiet(cfg, today):
    """빈손일 때 빈손이라고 말해야 모델이 지어내지 않는다."""
    assert "없습니다" in SL.run(cfg, "/view")


# ── 블록 평문화 ────────────────────────────────────────────────────────


def test_blocks_become_plain_text():
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "*제목*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "<https://x.test|링크>"}},
    ]
    out = SL.flatten_blocks(blocks)
    assert "*제목*" in out
    assert "링크 (https://x.test)" in out


def test_buttons_are_dropped_but_checkbox_labels_survive():
    """버튼 글자는 답변으로 새어 나가고, 체크박스 라벨은 지울 번호를 담고 있다."""
    blocks = [
        {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "삭제"}}]},
        {
            "type": "section",
            "accessory": {
                "type": "checkboxes",
                "options": [{"text": {"type": "mrkdwn", "text": "3. 미팅"}, "value": "3"}],
            },
        },
    ]
    out = SL.flatten_blocks(blocks)
    assert "3. 미팅" in out
    assert "삭제" not in out


def test_collector_ignores_late_calls():
    """`/del` 이 15초 뒤에 거는 실행취소 타이머가 다음 턴에 섞이면 안 된다."""
    collector = SL._Collector()
    collector("첫 번째")
    assert collector.close() == "첫 번째"
    collector("늦게 온 것")
    assert collector.close() == "첫 번째"


def test_collector_drops_the_fallback_text_when_blocks_repeat_it():
    collector = SL._Collector()
    collector(text="삭제됨", blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "🗑 삭제됨"}}])
    assert collector.close() == "🗑 삭제됨"


# ── 길이 ──────────────────────────────────────────────────────────────


def test_output_is_capped_and_says_so(cfg, today, monkeypatch):
    """툴 결과가 곧 다음 턴의 입력이다. 깊이가 곧 비용이다."""
    monkeypatch.setattr(SL, "_week_text", lambda _cfg: "가" * 9000)
    out = SL.run(cfg, "/week")
    assert len(out) < 9000
    assert "자만 실었습니다" in out


# ── 번호가 아니라 제목으로 고르기 ─────────────────────────────────────────
#
# ★ 실측 사고: "'집중근무' 완료 처리해줘" 에 8B 가 목록도 안 보고 `/done 1` 을
#   불렀다. 1번은 '근무' 였다 — **엉뚱한 계획이 완료로 바뀌었고 에러는 없었다.**
#   번호만 받으면 모델이 이름을 번호로 옮겨야 하고, 그 변환이 곧 추측이다.


def test_done_은_제목으로도_고른다(cfg, today):
    SL.run(cfg, "/plan 근무 09:00-21:00")
    SL.run(cfg, "/plan 집중근무 14:00-15:00")
    out = SL.run(cfg, "/done 집중근무")

    assert "집중근무" in out
    conn = db.open_db(cfg)
    try:
        got = {r.title: r.status for r in plan_models.list_instances(conn, DAY)}
    finally:
        conn.close()
    assert got == {"근무": "todo", "집중근무": "done"}, "이름이 겹쳐도 지목한 것만 바뀐다"


def test_정확일치가_부분일치를_이긴다(cfg, today):
    """'근무' 는 '집중근무' 의 부분문자열이다. 정확히 같은 제목이 있으면 그것이다."""
    SL.run(cfg, "/plan 근무 09:00-21:00")
    SL.run(cfg, "/plan 집중근무 14:00-15:00")
    SL.run(cfg, "/doing 근무")

    conn = db.open_db(cfg)
    try:
        got = {r.title: r.status for r in plan_models.list_instances(conn, DAY)}
    finally:
        conn.close()
    assert got == {"근무": "doing", "집중근무": "todo"}


def test_제목이_애매하면_아무것도_안_바꾼다(cfg, today):
    """하나를 골라 바꾸면 그게 또 추측이다. 후보를 보여주고 멈춘다."""
    SL.run(cfg, "/plan 회의 준비 09:00-10:00")
    SL.run(cfg, "/plan 회의 정리 10:00-11:00")
    out = SL.run(cfg, "/done 회의")

    assert "여러 개" in out
    assert "1. 회의 준비" in out and "2. 회의 정리" in out
    conn = db.open_db(cfg)
    try:
        assert {r.status for r in plan_models.list_instances(conn, DAY)} == {"todo"}
    finally:
        conn.close()


def test_없는_제목이면_목록을_보여준다(cfg, today):
    SL.run(cfg, "/plan 근무 09:00-21:00")
    out = SL.run(cfg, "/done 없는것")
    assert "없습니다" in out
    assert "1. 근무" in out, "무엇이 있는지 같이 말해야 모델이 다시 고를 수 있다"


def test_번호는_예전처럼_동작한다(cfg, today):
    SL.run(cfg, "/plan 첫째 09:00-10:00")
    SL.run(cfg, "/plan 둘째 10:00-11:00")
    SL.run(cfg, "/done 2")

    conn = db.open_db(cfg)
    try:
        got = {r.title: r.status for r in plan_models.list_instances(conn, DAY)}
    finally:
        conn.close()
    assert got == {"첫째": "todo", "둘째": "done"}


# ── 복수 번호 (2026-08-24) ───────────────────────────────────────────────
#
# ★ 실측 사고: "2번 3번 완료처리해줘" 에 모델이 `/done 2 3` 을 불렀고, 예전 코드는
#   `text.split()[0]` 으로 **2번만** 바꾸고 성공했다고 답했다. 사용자는 둘 다 됐다고
#   믿었다. **조용히 절반만 하는 것**이 이 저장소가 계속 잡아온 실패 유형인데,
#   정작 내가 만든 코드에 같은 게 있었다.


def test_done_은_번호_여러_개를_한_번에_바꾼다(cfg, today):
    for n in ("첫째", "둘째", "셋째"):
        SL.run(cfg, f"/plan {n} 09:00-10:00")
    SL.run(cfg, "/done 1 2")

    conn = db.open_db(cfg)
    try:
        got = {r.title: r.status for r in plan_models.list_instances(conn, DAY)}
    finally:
        conn.close()
    assert got == {"첫째": "done", "둘째": "done", "셋째": "todo"}


def test_쉼표로_구분해도_된다(cfg, today):
    """`/del 3,5` 와 같은 파서를 쓴다 — 명령마다 다른 파서면 조용히 갈린다."""
    for n in ("첫째", "둘째"):
        SL.run(cfg, f"/plan {n} 09:00-10:00")
    SL.run(cfg, "/done 1,2")

    conn = db.open_db(cfg)
    try:
        assert {r.status for r in plan_models.list_instances(conn, DAY)} == {"done"}
    finally:
        conn.close()


def test_하나라도_없으면_아무것도_안_바꾼다(cfg, today):
    """절반만 바꾸고 성공했다고 답하면 사용자가 다 됐다고 믿는다."""
    SL.run(cfg, "/plan 첫째 09:00-10:00")
    out = SL.run(cfg, "/done 1 9")

    assert "9번" in out
    conn = db.open_db(cfg)
    try:
        assert [r.status for r in plan_models.list_instances(conn, DAY)] == ["todo"]
    finally:
        conn.close()


def test_숫자가_섞인_제목은_번호로_안_읽는다(cfg, today):
    """'실습 3-1' 같은 제목을 번호 목록으로 오인하면 엉뚱한 걸 바꾼다."""
    SL.run(cfg, "/plan 실습 3-1 09:00-10:00")
    SL.run(cfg, "/done 실습 3-1")

    conn = db.open_db(cfg)
    try:
        assert [r.status for r in plan_models.list_instances(conn, DAY)] == ["done"]
    finally:
        conn.close()


# ── 모델이 보내는 번호를 받아준다 (2026-08-24) ────────────────────────────
#
# ★ 채점으로 확인한 것: `get_plans` 가 목록을 `[134] … 어제표식` 으로 싣는다.
#   **목록에 번호가 붙어 있으니 모델은 그 번호로 명령한다** — `/done 134`.
#   ordinal 만 받던 시절 이 케이스가 **0/3** 이었고, 답변은
#   "먼저 /view 로 번호를 확인해 주세요" 로 사용자에게 떠넘겼다.
#
#   모델 쪽을 고치려 세 번 시도해 세 번 더 나빠졌다(ordinal 교체·`N번`·번호 제거).
#   **모델이 이미 보내는 것을 우리가 받는 편이 값싸다.**


def test_done_은_인스턴스_id_로도_고른다(cfg, today):
    SL.run(cfg, "/plan 표식 09:00-10:00")
    conn = db.open_db(cfg)
    try:
        row = plan_models.list_instances(conn, DAY)[0]
    finally:
        conn.close()

    SL.run(cfg, f"/done {row.id}")

    conn = db.open_db(cfg)
    try:
        assert [r.status for r in plan_models.list_instances(conn, DAY)] == ["done"]
    finally:
        conn.close()


def test_ordinal_이_id_보다_우선한다(cfg, today):
    """사람이 `/done 2` 라고 칠 때는 화면의 2번을 뜻한다 — 그게 우연히 다른
    계획의 id 여도 사람 의도가 이긴다."""
    for n in ("첫째", "둘째"):
        SL.run(cfg, f"/plan {n} 09:00-10:00")
    conn = db.open_db(cfg)
    try:
        rows = plan_models.list_instances(conn, DAY)
        second = next(r for r in rows if r.ordinal == 2)
    finally:
        conn.close()

    SL.run(cfg, "/done 2")

    conn = db.open_db(cfg)
    try:
        got = {r.title: r.status for r in plan_models.list_instances(conn, DAY)}
    finally:
        conn.close()
    assert got[second.title] == "done"
    assert list(got.values()).count("done") == 1


def test_없는_번호는_id_로도_없으면_거부한다(cfg, today):
    SL.run(cfg, "/plan 표식 09:00-10:00")
    out = SL.run(cfg, "/done 999999")
    assert "찾을 수 없습니다" in out

    conn = db.open_db(cfg)
    try:
        assert [r.status for r in plan_models.list_instances(conn, DAY)] == ["todo"]
    finally:
        conn.close()


# ── 이어가기 (1~3단계) ───────────────────────────────────────────────────


def test_조회_결과가_보고_있는_날짜를_말한다(cfg, today):
    """시스템 프롬프트의 '오늘'과 겨루려면 신호가 프롬프트 뒤쪽에 있어야 한다."""
    out = SL.run(cfg, "/view", day="어제")
    assert "지금 보고 있는 날짜" in out and "2026-08-16" in out


def test_쓰기_결과가_적용한_날짜를_말한다(cfg, today):
    """막지는 못해도 보이게 한다 — 무엇이 바뀌었는지 몰랐던 것이 피해의 절반이었다."""
    SL.run(cfg, "/plan 표식 09:00-10:00", day="어제")
    out = SL.run(cfg, "/done 표식", day="어제")
    assert "적용한 날짜" in out and "2026-08-16" in out


def test_직전과_같은_날은_마지막으로_다룬_날짜다(cfg, today):
    """모델에게 '이어간다'고 말할 낱말이 없어서 기본값(오늘)으로 떨어졌다."""
    SL.run(cfg, "/view", day="어제", channel="D_A")
    SL.run(cfg, "/plan 이어진것 09:00-10:00", day=SL.CARRY_WORD, channel="D_A")

    conn = db.open_db(cfg)
    try:
        assert [r.title for r in plan_models.list_instances(conn, "2026-08-16")] == ["이어진것"]
        assert plan_models.list_instances(conn, DAY) == []
    finally:
        conn.close()


def test_맥락이_없으면_오늘로_간다(cfg, today):
    """재기동 직후 등 기억이 없을 때. 오늘로 가는 것은 원래 동작이라 새 위험이 아니다."""
    SL.run(cfg, "/plan 새채널것 09:00-10:00", day=SL.CARRY_WORD, channel="D_NEW")
    conn = db.open_db(cfg)
    try:
        assert [r.title for r in plan_models.list_instances(conn, DAY)] == ["새채널것"]
    finally:
        conn.close()


# ── /private (2026-09-04) ──────────────────────────────────────────────
#
# ★ 프라이빗을 **에이전트 툴이 아니라 슬래시로** 붙인 이유가 여기 걸려 있다.
#   모델은 명령 문자열만 고르고 실제 동작은 코드가 한다 — 8B 가 툴을 안 부르고
#   "껐습니다" 로 끝내는 것은 **여전히 가능하다**(그건 어떤 툴이든 같다). 막히는 것은
#   **부르고 나서 다른 일을 하는 것**이고, 그래서 반환문이 결과 상태를 말한다.
#   다른 명령이면 답이 틀리고
#   말지만, **프라이빗은 사람이 켜졌다고 믿고 행동한다.**
#
#   그래서 답에 항상 **결과 상태**가 담겨야 한다. "껐습니다" 가 아니라 "프라이빗: 꺼짐".


def test_private_반환문에_결과_상태가_담긴다(cfg):
    """★ 이 파일에서 제일 중요한 것. 모델이 인용할 문자열에 **지금 상태**가 있어야 한다.

    "켰습니다" 만 돌려주면 요청과 결과가 어긋나도 사람 눈에 안 보인다.
    """
    out = SL.run(cfg, "/private 30")
    assert "프라이빗: 켜짐" in out and "30분 남음" in out

    out = SL.run(cfg, "/private off")
    assert "프라이빗: 꺼짐" in out


def test_private_status_는_아무것도_안_바꾼다(cfg):
    """상태 조회가 상태를 바꾸면 그건 조회가 아니다."""
    SL.run(cfg, "/private 30")
    before = SL.run(cfg, "/private status")
    after = SL.run(cfg, "/private status")
    assert "켜짐" in before and "켜짐" in after


def test_private_는_상한을_넘기지_않는다(cfg):
    """`max_minutes` 를 넘는 요청은 거절한다.

    ★ 모델이 "하루 종일 꺼줘" 를 1440 으로 옮길 수 있다. 상한이 없으면 그 하루가
      통째로 안 재진 채로 지나가고, **지나간 시간은 되돌릴 수 없다.**
    """
    out = SL.run(cfg, f"/private {cfg.private.max_minutes + 1}")
    assert "이하여야" in out
    assert "켜짐" not in SL.run(cfg, "/private status")


def test_private_는_못_읽는_인자로_켜지지_않는다(cfg):
    """★ 안 켜져야 할 때 안 켜지는 쪽. 파싱 실패가 **켜짐으로 떨어지면 안 된다.**

    반대 방향(끄기로 떨어짐)보다 이쪽이 덜 위험해 보이지만, 사용법을 돌려주고
    아무 일도 안 하는 것이 유일하게 정직한 답이다 — 사람이 켜졌다고 믿으면 안 된다.
    """
    out = SL.run(cfg, "/private 헛소리")
    assert "/private" in out  # 사용법
    assert "켜짐" not in SL.run(cfg, "/private status")
