"""lifetrainer.slackio.app 의 자연어 대화 핸들러 테스트.

`tests/test_slack_commands.py` 와 같은 방어를 쓴다 — `load_config()` 가 이 기기의
진짜 Slack 토큰을 채워 넣을 수 있으므로 `cfg` 픽스처에서 항상 빈 토큰으로 덮고,
`converse` 도 가짜로 갈아끼워 GPU·네트워크를 쓰지 않는다.

이 파일이 지키려는 계약:
- **DM 에서 멘션하면 두 이벤트가 온다.** 한 번만 답해야 한다 (안 그러면 GPU 도 두 번).
- **무응답이 최악이다.** 어떤 예외가 나도 사용자에게 뭐라도 답한다.
- **에이전트 위임은 명시적으로 켤 때만.** 기본값 기기는 빠른 경로 그대로다.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from lifetrainer.config import SlackConfig, load_config
from lifetrainer.slackio import app as app_mod


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
    return dataclasses.replace(base, db_path=tmp_path / "lt.db", slack=safe_slack)


@pytest.fixture(autouse=True)
def _reset_module_state():
    """모듈 전역(이력·중복 방지)은 테스트 간에 새어 나가면 안 된다."""
    app_mod._history.clear()
    app_mod._seen_events.clear()
    yield
    app_mod._history.clear()
    app_mod._seen_events.clear()


class FakeSlackClient:
    def __init__(self) -> None:
        self.added: list[tuple] = []
        self.removed: list[tuple] = []

    def reactions_add(self, channel, timestamp, name):  # noqa: ANN001
        self.added.append((channel, timestamp, name))

    def reactions_remove(self, channel, timestamp, name):  # noqa: ANN001
        self.removed.append((channel, timestamp, name))


class FakeSay:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def __call__(self, **kwargs):  # noqa: ANN003
        self.messages.append(kwargs)


def _result(text: str, *, ok: bool = True):
    from lifetrainer.llm.converse import ConverseResult

    return ConverseResult(text=text, rounds=1, latency_ms=10, ok=ok)


@pytest.fixture()
def fake_converse(monkeypatch):
    """`converse` 를 가짜로. 호출 인자를 기록해 돌려준다."""
    import lifetrainer.llm.converse as converse_mod

    calls: list[dict] = []

    def _fake(conn, cfg, client, **kwargs):  # noqa: ANN001
        calls.append(kwargs)
        return _result("가짜 응답")

    monkeypatch.setattr(converse_mod, "converse", _fake)
    return calls


# ── 중복 이벤트 ──────────────────────────────────────────────────────────


def test_같은_이벤트는_한_번만_처리한다():
    """DM 에서 멘션하면 message.im 과 app_mention 이 둘 다 온다."""
    event = {"channel": "D1", "event_ts": "1700000000.000100"}
    assert app_mod._claim_event(event) is True
    assert app_mod._claim_event(dict(event)) is False


def test_다른_이벤트는_각각_처리한다():
    assert app_mod._claim_event({"channel": "D1", "event_ts": "1.1"}) is True
    assert app_mod._claim_event({"channel": "D1", "event_ts": "1.2"}) is True
    assert app_mod._claim_event({"channel": "D2", "event_ts": "1.1"}) is True


def test_ts_가_없으면_막지_않는다():
    assert app_mod._claim_event({"channel": "D1"}) is True
    assert app_mod._claim_event({"channel": "D1"}) is True


def test_오래된_기록은_정리된다(monkeypatch):
    app_mod._claim_event({"channel": "D1", "event_ts": "1.1"})

    # 패치 전 원본을 먼저 붙잡는다 — 람다 안에서 다시 부르면 자기 자신을 부른다.
    original_now = app_mod.timeutil.now_ts
    later = original_now() + app_mod._SEEN_TTL_SEC + 10
    monkeypatch.setattr(app_mod.timeutil, "now_ts", lambda: later)

    app_mod._claim_event({"channel": "D2", "event_ts": "2.2"})
    assert ("D1", "1.1") not in app_mod._seen_events


# ── 대화 처리 ────────────────────────────────────────────────────────────


def test_답을_올리고_반응을_붙였다_뗀다(cfg, fake_converse):
    client, say = FakeSlackClient(), FakeSay()
    event = {"channel": "D1", "ts": "1.1", "user": "U1", "text": "오늘 뭐 했지?"}

    app_mod._dispatch_chat(cfg, event, client, say)

    assert say.messages == [{"text": "가짜 응답"}]
    assert client.added == [("D1", "1.1", app_mod._THINKING_EMOJI)]
    assert client.removed == [("D1", "1.1", app_mod._THINKING_EMOJI)]


def test_멘션_표기를_떼고_넘긴다(cfg, fake_converse):
    event = {"channel": "D1", "ts": "1.1", "user": "U1", "text": "<@U0BOT> 오늘 뭐 했지?"}
    app_mod._dispatch_chat(cfg, event, FakeSlackClient(), FakeSay())
    assert fake_converse[0]["user_text"] == "오늘 뭐 했지?"


def test_대화가_일어난_채널이_예약_대상이_된다(cfg, fake_converse):
    event = {"channel": "D_ABC", "ts": "1.1", "user": "U1", "text": "안녕"}
    app_mod._dispatch_chat(cfg, event, FakeSlackClient(), FakeSay())
    assert fake_converse[0]["channel"] == "D_ABC"
    assert fake_converse[0]["actor"] == "U1"


def test_빈_메시지는_무시한다(cfg, fake_converse):
    say = FakeSay()
    app_mod._dispatch_chat(cfg, {"channel": "D1", "ts": "1.1", "text": "  "}, FakeSlackClient(), say)
    assert say.messages == []
    assert fake_converse == []


def test_스레드에서는_스레드로_답한다(cfg, fake_converse):
    say = FakeSay()
    event = {"channel": "D1", "ts": "1.2", "user": "U1", "text": "안녕", "thread_ts": "1.0"}
    app_mod._dispatch_chat(cfg, event, FakeSlackClient(), say)
    assert say.messages == [{"text": "가짜 응답", "thread_ts": "1.0"}]


def test_예외가_나도_반드시_답한다(cfg, monkeypatch):
    import lifetrainer.llm.converse as converse_mod

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("터짐")

    monkeypatch.setattr(converse_mod, "converse", _boom)
    client, say = FakeSlackClient(), FakeSay()
    app_mod._dispatch_chat(cfg, {"channel": "D1", "ts": "1.1", "text": "안녕"}, client, say)

    assert len(say.messages) == 1
    assert "오류" in say.messages[0]["text"]
    assert client.removed  # 반응은 반드시 떼어야 한다


def test_반응_실패가_대화를_막지_않는다(cfg, fake_converse):
    class BrokenClient(FakeSlackClient):
        def reactions_add(self, **kwargs):  # noqa: ANN003
            raise RuntimeError("권한 없음")

    say = FakeSay()
    app_mod._dispatch_chat(cfg, {"channel": "D1", "ts": "1.1", "text": "안녕"}, BrokenClient(), say)
    assert say.messages == [{"text": "가짜 응답"}]


# ── 이력 ────────────────────────────────────────────────────────────────


def test_성공한_턴만_이력에_쌓인다(cfg, monkeypatch):
    import lifetrainer.llm.converse as converse_mod

    monkeypatch.setattr(converse_mod, "converse", lambda *a, **k: _result("실패", ok=False))
    app_mod._dispatch_chat(cfg, {"channel": "D1", "ts": "1.1", "text": "안녕"}, FakeSlackClient(), FakeSay())
    assert app_mod._get_history("D1") == []


def test_이력은_채널별로_나뉘고_상한이_있다():
    for i in range(10):
        app_mod._append_history("D1", f"질문{i}", f"답변{i}")
    app_mod._append_history("D2", "다른 채널", "다른 답")

    d1 = app_mod._get_history("D1")
    assert len(d1) == app_mod._HISTORY_TURNS * 2
    assert d1[0]["content"] == f"질문{10 - app_mod._HISTORY_TURNS}"
    assert len(app_mod._get_history("D2")) == 2


# ── OpenClaw 에이전트 위임 (2026-08-24) ──────────────────────────────────
#
# 이 묶음이 지키는 계약:
#
# - **기본은 안 넘긴다.** 설정이 사라진 기기에서 자연어가 조용히 35초짜리가
#   되면 안 된다. 켜는 것이 명시적인 행위여야 한다.
# - **에이전트가 죽어도 사용자는 답을 받는다.** 게이트웨이가 없으면 대화 경로로
#   내려온다. 이건 라우팅 규칙이 아니라 강등이다.
# - **한 번 넘겼으면 두 번 안 부른다.** 이미 수십 초를 썼는데 또 한 바퀴를
#   돌리면 사용자가 1분을 넘게 기다린다.


@pytest.fixture(autouse=True)
def _agent_settings_are_defaults(monkeypatch):
    """★ **이 기기의 `config/lifetrainer.toml` 을 안 읽게 한다.**

    `_try_agent` 는 `load_settings(cfg)` 로 실제 설정 파일을 읽는다. 그 파일에
    `[agent] slack = true` 가 들어간 순간 "기본값에서는 안 넘어간다" 테스트의
    전제가 조용히 무너진다 — 테스트가 **돌리는 기기의 설정에 의존**하게 된다.
    이 파일 맨 위 docstring 이 Slack 토큰에 대해 하는 방어와 같은 이유다.
    """
    from lifetrainer.agent import config as agent_config

    monkeypatch.setattr(agent_config, "load_settings", lambda _cfg: agent_config.AgentSettings())


@pytest.fixture()
def agent_on(cfg, monkeypatch):
    """`[agent] slack = true` 인 것처럼 (위 autouse 를 덮어쓴다)."""
    from lifetrainer.agent import config as agent_config

    settings = agent_config.AgentSettings(slack=True)
    monkeypatch.setattr(agent_config, "load_settings", lambda _cfg: settings)
    return settings


@pytest.fixture()
def fake_agent(monkeypatch):
    """`delegate.ask` 를 가짜로. 호출 인자를 기록한다."""
    from lifetrainer.agent import delegate as delegate_mod

    calls: list[dict] = []

    def _fake(cfg, text, **kwargs):  # noqa: ANN001
        calls.append({"text": text, **kwargs})
        return delegate_mod.AgentReply(text="에이전트 답", ok=True, seconds=30.0, tools_used=["slash"])

    monkeypatch.setattr(delegate_mod, "ask", _fake)
    monkeypatch.setattr(delegate_mod, "available", lambda *_a: True)
    return calls


def test_기본값에서는_에이전트로_안_넘어간다(cfg, fake_converse, fake_agent):
    """설정을 안 건드린 기기는 예전 그대로 빠른 경로다."""
    client, say = FakeSlackClient(), FakeSay()
    app_mod._dispatch_chat(cfg, {"channel": "D1", "ts": "1.1", "user": "U1", "text": "오늘 뭐 했지?"}, client, say)

    assert fake_agent == []
    assert len(fake_converse) == 1
    assert say.messages[0]["text"] == "가짜 응답"


def test_켜면_에이전트가_답한다(cfg, agent_on, fake_converse, fake_agent):
    client, say = FakeSlackClient(), FakeSay()
    app_mod._dispatch_chat(cfg, {"channel": "D1", "ts": "1.1", "user": "U1", "text": "1번 완료해줘"}, client, say)

    assert len(fake_agent) == 1
    assert fake_agent[0]["text"] == "1번 완료해줘"
    assert fake_agent[0]["channel"] == "D1"
    assert fake_converse == [], "에이전트가 답했으면 대화 경로를 또 타면 안 된다"
    assert say.messages[0]["text"] == "에이전트 답"


def test_반응은_에이전트_경로에서도_붙였다_뗀다(cfg, agent_on, fake_converse, fake_agent):
    """35초 동안 모래시계가 안 붙어 있으면 사용자는 무시당했다고 느낀다."""
    client, say = FakeSlackClient(), FakeSay()
    app_mod._dispatch_chat(cfg, {"channel": "D1", "ts": "1.1", "user": "U1", "text": "뭐해"}, client, say)

    assert client.added and client.removed


def test_게이트웨이가_없으면_대화_경로로_내려온다(cfg, agent_on, fake_converse, monkeypatch):
    from lifetrainer.agent import delegate as delegate_mod

    monkeypatch.setattr(delegate_mod, "available", lambda *_a: False)
    client, say = FakeSlackClient(), FakeSay()
    app_mod._dispatch_chat(cfg, {"channel": "D1", "ts": "1.1", "user": "U1", "text": "오늘 뭐 했지?"}, client, say)

    assert len(fake_converse) == 1
    assert say.messages[0]["text"] == "가짜 응답"


def test_위임_중_예외가_나도_답은_나간다(cfg, agent_on, fake_converse, monkeypatch):
    from lifetrainer.agent import delegate as delegate_mod

    def _explode(*_args, **_kwargs):
        raise RuntimeError("게이트웨이 폭발")

    monkeypatch.setattr(delegate_mod, "available", lambda *_a: True)
    monkeypatch.setattr(delegate_mod, "ask", _explode)
    client, say = FakeSlackClient(), FakeSay()
    app_mod._dispatch_chat(cfg, {"channel": "D1", "ts": "1.1", "user": "U1", "text": "오늘 뭐 했지?"}, client, say)

    assert len(fake_converse) == 1, "예외가 나면 대화 경로로 내려와야 한다"
    assert say.messages[0]["text"] == "가짜 응답"


def test_타임아웃은_내려가지_않고_사실을_말한다(cfg, agent_on, fake_converse, monkeypatch):
    """이미 수십 초를 썼다. 또 한 바퀴 돌리면 1분을 넘긴다."""
    from lifetrainer.agent import delegate as delegate_mod

    monkeypatch.setattr(delegate_mod, "available", lambda *_a: True)
    monkeypatch.setattr(
        delegate_mod, "ask",
        lambda *a, **k: delegate_mod.AgentReply(text="240초 안에 답하지 못했습니다.", ok=False, seconds=240.0),
    )
    client, say = FakeSlackClient(), FakeSay()
    app_mod._dispatch_chat(cfg, {"channel": "D1", "ts": "1.1", "user": "U1", "text": "오늘 뭐 했지?"}, client, say)

    assert fake_converse == []
    assert "답하지 못했습니다" in say.messages[0]["text"]


def test_슬래시_명령은_에이전트를_안_거친다(cfg, agent_on, fake_agent):
    """`/lt` 는 이 핸들러가 아니라 슬래시 커맨드로 간다 — 밀리초 그대로여야 한다."""
    from lifetrainer.agent import slash as slash_mod

    out = slash_mod.run(cfg, "/lt ping")
    assert "pong" in out
    assert fake_agent == [], "슬래시가 에이전트를 거치면 밀리초가 35초가 된다"


# ── 세션 회전 (2026-08-24) ───────────────────────────────────────────────
#
# ★ 실측 사고: 한 시간쯤 대화하자 Slack 이 **"Context overflow: prompt too large"**
#   를 그대로 뱉었다. ctx 20,480 에 시스템 프롬프트 5,326 인데 압축 설정이
#   ctx 40,960 시절 값(keepRecent 8000 · reserve 6000)이라 여유가 1,154 토큰뿐이었다.
#   설정을 낮춰 여유를 늘렸지만 그건 **한계를 미룰 뿐**이라, 세션 자체에 상한을 뒀다.


@pytest.fixture(autouse=True)
def _reset_sessions():
    from lifetrainer.agent import delegate as delegate_mod

    delegate_mod._sessions.clear()
    yield
    delegate_mod._sessions.clear()


def test_같은_채널은_같은_세션을_쓴다():
    from lifetrainer.agent import delegate as delegate_mod

    first = delegate_mod._next_key("lifetrainer", "D1")
    assert delegate_mod._next_key("lifetrainer", "D1") == first
    assert delegate_mod._next_key("lifetrainer", "D2") != first, "채널이 다르면 맥락도 다르다"


def test_턴_상한을_넘으면_세션을_간다():
    from lifetrainer.agent import delegate as delegate_mod

    keys = [delegate_mod._next_key("lifetrainer", "D1") for _ in range(delegate_mod.MAX_TURNS_PER_SESSION + 1)]
    assert keys[0] != keys[-1], "히스토리가 무한정 자라면 언젠가 컨텍스트가 넘친다"
    assert len(set(keys)) == 2


def test_오래_조용하면_세션을_간다(monkeypatch):
    """어제 대화가 오늘 답에 섞이면 안 된다 — 지운 계획이 '완료'로 나온 적이 있다."""
    from lifetrainer.agent import delegate as delegate_mod

    clock = [1000.0]
    monkeypatch.setattr(delegate_mod.time, "monotonic", lambda: clock[0])
    first = delegate_mod._next_key("lifetrainer", "D1")
    clock[0] += delegate_mod.SESSION_IDLE_SEC + 1
    assert delegate_mod._next_key("lifetrainer", "D1") != first


def test_ask_의_인자가_run_까지_그대로_간다(monkeypatch):
    """★ 이 테스트가 없어서 `cfg` 를 안 넘긴 것을 못 잡았다.

    다른 테스트들이 `_run` 을 통째로 가짜로 바꿔서, **검증 대상을 모킹한** 셈이
    됐다. 실제 서비스에서 `NameError: name 'cfg' is not defined` 로 죽었고
    강등 경로가 받아냈다. 시그니처가 맞는지는 여기서 본다.
    """
    import inspect

    from lifetrainer.agent import delegate as delegate_mod

    seen: dict = {}

    def capture(cfg, binary, agent_id, channel, text, timeout_sec, *, rotate):
        seen.update(cfg=cfg, binary=binary, agent_id=agent_id, channel=channel, text=text)
        return delegate_mod.AgentReply(text="답", ok=True, seconds=1.0)

    # 가짜의 **인자 이름**이 진짜와 같은지 먼저 본다 — 안 그러면 이 테스트도 같은
    # 함정에 빠진다 (타입 주석은 비교하지 않는다. 이름이 어긋난 것이 그 버그였다).
    assert list(inspect.signature(capture).parameters) == list(
        inspect.signature(delegate_mod._run).parameters
    )

    monkeypatch.setattr(delegate_mod, "resolve_bin", lambda *_a, **_k: "/usr/bin/openclaw")
    monkeypatch.setattr(delegate_mod, "_run", capture)

    sentinel = object()
    delegate_mod.ask(sentinel, "질문", channel="D9", agent_id="lifetrainer")
    assert seen["cfg"] is sentinel
    assert seen["channel"] == "D9"


def test_컨텍스트가_넘치면_세션을_갈고_한_번_재시도한다(monkeypatch):
    """영어 오류 문장을 그대로 보여주는 것은 답이 아니다 — 우리 설정 문제다."""
    from lifetrainer.agent import delegate as delegate_mod

    calls: list[bool] = []

    def fake_run(cfg, binary, agent_id, channel, text, timeout_sec, *, rotate):
        calls.append(rotate)
        if not rotate:
            return delegate_mod.AgentReply(
                text="Context overflow: prompt too large for the model.", ok=False, seconds=1.0
            )
        return delegate_mod.AgentReply(text="정상 답", ok=True, seconds=1.0)

    monkeypatch.setattr(delegate_mod, "resolve_bin", lambda *_a, **_k: "/usr/bin/openclaw")
    monkeypatch.setattr(delegate_mod, "_run", fake_run)

    reply = delegate_mod.ask(object(), "어제 계획은 뭐였어?", channel="D1")
    assert calls == [False, True], "한 번만 재시도한다 (두 번이면 1분을 넘게 기다린다)"
    assert reply.ok and reply.text == "정상 답"


def test_재시도해도_넘치면_두_번은_안_한다(monkeypatch):
    from lifetrainer.agent import delegate as delegate_mod

    calls: list[bool] = []

    def always_overflow(cfg, binary, agent_id, channel, text, timeout_sec, *, rotate):
        calls.append(rotate)
        return delegate_mod.AgentReply(text="Context overflow: prompt too large.", ok=False, seconds=1.0)

    monkeypatch.setattr(delegate_mod, "resolve_bin", lambda *_a, **_k: "/usr/bin/openclaw")
    monkeypatch.setattr(delegate_mod, "_run", always_overflow)

    reply = delegate_mod.ask(object(), "질문", channel="D1")
    assert len(calls) == 2
    assert reply.ok is False
