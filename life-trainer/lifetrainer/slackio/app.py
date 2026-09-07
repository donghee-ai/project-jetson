"""Slack Socket Mode 앱 (`cfg.slack.mode == 'bolt'` 이고 두 토큰이 다 있을 때만 동작).

명령표(계약서-v2 §5, 이 시스템의 **주 입력 경로** — 실측 25~40초 걸리는 에이전트
경유 대신 밀리초 만에 끝나고, 소형 모델의 "툴 안 부르고 했다고 답하는" 실패
모드가 원천적으로 불가능하다):
  /lt ping|today|yesterday|week|status
  /log <카테고리> <기간> [메모]
  /plan <텍스트>              -- 태스크 추가 (번호 목록 일괄, #과목/@기간/!우선순위/시각범위 확장 문법)
  /del [번호,번호…]           -- 인자 없으면 체크박스 목록, 있으면 즉시 소프트 삭제(15초 실행취소)
  /view [어제|YYYY-MM-DD]     -- 플래너 PNG 업로드 + 카드
  /done|/doing|/defer <번호>  -- plan_instance 상태 전환 (defer 는 내일 인스턴스 자동 생성)
  /memo <텍스트>              -- day.memo upsert
  /week                       -- 주간 카드 + PNG

설계 원칙(조사 문서 §7·§9):
  - `ack()` 는 3초 안에 하고, 무거운 작업(DB 조회, 리포트 생성)은 별도 스레드에서 한다.
    Bolt 의 lazy listener 는 FaaS 지향이라 상시 프로세스에는 `threading.Thread` 가 더 단순하다.
  - 핸들러 안에서 DB 커넥션은 매번 새로 연다 (sqlite3 는 스레드 간 공유가 기본적으로 안 된다).
  - SIGTERM/SIGINT 에 `handler.close()` 를 걸어 systemd 아래에서 깔끔히 종료한다.
  - `/del` 의 체크박스·버튼은 `block_actions` 인터랙션(`@app.action`)으로 처리한다.
    각 dispatch 함수는 테스트에서 Bolt 라우팅 없이 직접 호출할 수 있도록
    `(cfg, ..., respond)` 형태의 순수 함수로 분리해 둔다.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import signal
import threading
import time
from typing import Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from lifetrainer import db, timeutil
from lifetrainer.config import Config
from lifetrainer.plan import models as plan_models
from lifetrainer.slackio import blocks as blocks_mod
from lifetrainer.slackio import commands as commands_mod
from lifetrainer.slackio.notify import SlackNotifier
from lifetrainer.web import auth as web_auth

logger = logging.getLogger(__name__)

_LT_USAGE = "사용법: /lt ping|today|yesterday|week|status"
_LOG_USAGE = "사용법: /log <카테고리> <기간> [메모]  예) /log 운동 60m 헬스장  (기간 뒤에 `at HH:MM` 을 붙이면 그 시각을 종료 기준으로 삼는다)"
_PLAN_USAGE = "사용법: /plan <텍스트>  예) /plan 프로젝트 보고서 #프로젝트 @90m !high"
_MEMO_USAGE = "사용법: /memo <텍스트>  예) /memo 신청 작업 장바구니 담기"

_DEFAULT_TASK_MINUTES = 30  # /plan 에서 기간/시각 범위를 아무것도 안 준 태스크의 기본 길이
_VALID_PRIORITIES = frozenset({"low", "normal", "high"})
_UNDO_WINDOW_SEC = 15.0  # SPEC §7-2: 삭제 후 실행취소 버튼 유효 시간

_STATUS_BY_COMMAND = {"done": "done", "doing": "doing", "defer": "deferred"}
_STATUS_EMOJI = {"done": "✅", "doing": "▶️", "deferred": "↩️"}
_STATUS_VERB = {"done": "완료 처리했습니다", "doing": "진행 중으로 표시했습니다", "deferred": "내일로 연기했습니다"}


def build_app(cfg: Config, *, verify_token: bool = True) -> App:
    """Socket Mode 용 Bolt `App` 을 만든다. `bolt` 모드가 아니거나 토큰이 없으면 예외.

    `verify_token=False` 는 테스트·점검용이다. Bolt 의 `App(...)` 은 **생성 시점에
    `auth.test` 를 실제로 호출한다** — 기본값 그대로 두면 이 함수를 부르는 것만으로
    네트워크를 타고 Slack 에 붙는다. 운영에서는 켜두는 게 맞다(잘못된 토큰을 즉시 잡는다).
    """
    if cfg.slack.mode != "bolt":
        raise RuntimeError(f"slack.mode 가 'bolt' 가 아닙니다 (현재: {cfg.slack.mode!r}).")
    if not cfg.slack.bot_token or not cfg.slack.app_token:
        raise RuntimeError("Slack bot_token/app_token 이 모두 설정되어야 Socket Mode 를 시작할 수 있습니다.")

    app = App(token=cfg.slack.bot_token, token_verification_enabled=verify_token)
    _register_commands(app, cfg)
    _register_actions(app, cfg)
    _register_conversation(app, cfg)
    return app


def run(cfg: Config) -> None:
    """Socket Mode 핸들러를 시작한다 (블로킹). SIGTERM/SIGINT 에 정리하고 종료."""
    app = build_app(cfg)
    handler = SocketModeHandler(app, cfg.slack.app_token)

    def _shutdown(signum: int, _frame: Any) -> None:
        logger.info("Slack Socket Mode 종료 신호 수신(%s) — 연결 정리 중", signum)
        handler.close()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info("Slack Socket Mode 시작")
    handler.start()


# ── 명령 등록 ──────────────────────────────────────────────────────────


def _register_commands(app: App, cfg: Config) -> None:
    @app.command("/lt")
    def _handle_lt(ack, respond, command) -> None:  # noqa: ANN001 - Bolt 가 이름으로 주입
        ack()  # 3초 제한을 지키기 위해 즉시 ack, 무거운 일은 스레드로 넘긴다
        text = (command.get("text") or "").strip()
        sub = text.split(maxsplit=1)[0].lower() if text else "ping"
        threading.Thread(target=_dispatch_lt, args=(cfg, sub, respond), daemon=True).start()

    @app.command("/log")
    def _handle_log(ack, respond, command) -> None:  # noqa: ANN001
        ack()
        threading.Thread(target=_dispatch_log, args=(cfg, command, respond), daemon=True).start()

    @app.command("/plan")
    def _handle_plan(ack, respond, command) -> None:  # noqa: ANN001
        ack()
        text = (command.get("text") or "").strip()
        threading.Thread(target=_dispatch_plan, args=(cfg, text, respond), daemon=True).start()

    @app.command("/del")
    def _handle_del(ack, respond, command) -> None:  # noqa: ANN001
        ack()
        threading.Thread(target=_dispatch_del, args=(cfg, command, respond), daemon=True).start()

    @app.command("/view")
    def _handle_view(ack, respond, command) -> None:  # noqa: ANN001
        ack()
        threading.Thread(target=_dispatch_view, args=(cfg, command, respond), daemon=True).start()

    @app.command("/done")
    def _handle_done(ack, respond, command) -> None:  # noqa: ANN001
        ack()
        text = (command.get("text") or "").strip()
        actor = command.get("user_id")
        threading.Thread(target=_dispatch_set_status, args=(cfg, "done", text, respond, actor), daemon=True).start()

    @app.command("/doing")
    def _handle_doing(ack, respond, command) -> None:  # noqa: ANN001
        ack()
        text = (command.get("text") or "").strip()
        actor = command.get("user_id")
        threading.Thread(target=_dispatch_set_status, args=(cfg, "doing", text, respond, actor), daemon=True).start()

    @app.command("/defer")
    def _handle_defer(ack, respond, command) -> None:  # noqa: ANN001
        ack()
        text = (command.get("text") or "").strip()
        actor = command.get("user_id")
        threading.Thread(target=_dispatch_set_status, args=(cfg, "defer", text, respond, actor), daemon=True).start()

    @app.command("/memo")
    def _handle_memo(ack, respond, command) -> None:  # noqa: ANN001
        ack()
        text = (command.get("text") or "").strip()
        threading.Thread(target=_dispatch_memo, args=(cfg, text, respond), daemon=True).start()

    @app.command("/private")
    def _handle_private(ack, respond, command) -> None:  # noqa: ANN001
        ack()
        text = (command.get("text") or "").strip()
        threading.Thread(target=_dispatch_private, args=(cfg, text, respond), daemon=True).start()

    @app.command("/week")
    def _handle_week(ack, respond, command) -> None:  # noqa: ANN001
        ack()
        threading.Thread(target=_dispatch_week, args=(cfg, command, respond), daemon=True).start()


# ── 자연어 대화 (슬래시 명령이 못 받는 것만) ─────────────────────────────
#
# 슬래시 명령이 주 경로다(모듈 docstring). 여기는 **폴백**이다 — 명령으로 표현할 수
# 없는 질문("오늘 계획대로 잘 했어?")만 LLM 을 태운다. 실측 지연은 1.5~7초로
# OpenClaw 경유(25~40초)보다 훨씬 짧은데, 시스템 프롬프트가 144 토큰이기 때문이다
# (OpenClaw 는 12,541 토큰).

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")

_history: dict[str, list[dict]] = {}
_seen_events: dict[tuple[str, str], float] = {}
_conv_lock = threading.Lock()

_HISTORY_TURNS = 3  # (user, assistant) 쌍 기준. converse 가 다시 한 번 자른다.
_SEEN_TTL_SEC = 300.0
_THINKING_EMOJI = "hourglass_flowing_sand"

# 스트리밍 중 메시지를 고쳐 쓰는 간격(초).
#
# ★ 이 기기에서 생성이 **9.4 tok/s** 다. 400토큰이면 42초고 그동안 화면이 비어 있다.
#   총 시간은 안 줄지만 기다리는 느낌이 바뀐다.
# ★ Slack `chat.update` 는 Tier 3(분당 약 50회) 다. 1.5초면 42초 답변에 28번이라
#   한도 안에 든다. 더 촘촘히 하면 429 를 맞고 오히려 갱신이 끊긴다.
_STREAM_UPDATE_SEC = 1.5
_STREAM_CURSOR = " ▌"


def _register_conversation(app: App, cfg: Config) -> None:
    """DM 과 멘션을 자연어 대화로 처리한다.

    DM 에서 봇을 @멘션하면 `message.im` 과 `app_mention` 이 **둘 다** 온다.
    `_claim_event` 가 (채널, ts) 로 한 번만 처리되게 막지 않으면 같은 질문에
    두 번 답하고 GPU 도 두 번 쓴다.
    """

    @app.event("app_mention")
    def _handle_app_mention(event, client, say) -> None:  # noqa: ANN001
        if not _claim_event(event):
            return
        _spawn_chat(cfg, event, client, say)

    @app.event("message")
    def _handle_message(event, client, say) -> None:  # noqa: ANN001
        # DM 만 받는다. 채널 메시지까지 받으면 대화마다 GPU 를 쓰게 된다 —
        # 채널에서는 멘션했을 때만 답하는 것이 맞다.
        if event.get("channel_type") != "im":
            return
        # 봇 자신의 메시지·편집·삭제 이벤트를 되받으면 무한 루프가 된다.
        if event.get("bot_id") or event.get("subtype"):
            return
        if not _claim_event(event):
            return
        _spawn_chat(cfg, event, client, say)


def _claim_event(event: dict) -> bool:
    """이 이벤트를 처음 보는 것이면 True. 중복(재전송·이중 구독)이면 False."""
    key = (str(event.get("channel") or ""), str(event.get("event_ts") or event.get("ts") or ""))
    if not key[1]:
        return True
    now = timeutil.now_ts()
    with _conv_lock:
        for stale in [k for k, seen in _seen_events.items() if now - seen > _SEEN_TTL_SEC]:
            del _seen_events[stale]
        if key in _seen_events:
            return False
        _seen_events[key] = now
    return True


def _spawn_chat(cfg: Config, event: dict, client: Any, say: Any) -> None:
    """대화 처리를 별도 스레드로 넘긴다 — 핸들러가 돌아가야 Bolt 가 ack 한다."""
    threading.Thread(
        target=_dispatch_chat, args=(cfg, event, client, say), daemon=True
    ).start()


def _dispatch_chat(cfg: Config, event: dict, client: Any, say: Any) -> None:
    """자연어 한 턴을 처리해 답을 올린다. 어떤 예외에도 반드시 무언가를 답한다.

    ## 경로가 둘이다 (2026-08-24)

        [agent] slack = true   OpenClaw 에이전트 — **모델이** 툴을 고른다   약 35초
        그 외                  llm/converse.py — 규칙이 툴을 고른다       2~9초

    ★ **슬래시 명령은 어느 쪽도 아니다.** Slack 에서 `/lt` 는 이 핸들러가 아니라
    `_register_commands` 로 간다 — 밀리초 그대로다. 여기 바뀌는 것은 **자연어 DM** 뿐.

    ★ 에이전트가 실패하면 `converse()` 로 내려온다. **이건 라우팅 규칙이 아니라
    강등이다** — 게이트웨이가 죽었을 때 사용자가 에러 대신 답을 받게 하려는 것이고,
    로그에 왜 내려왔는지 남는다.
    """
    from lifetrainer.llm.client import LLMClient
    from lifetrainer.llm.converse import converse

    text = _MENTION_RE.sub("", str(event.get("text") or "")).strip()
    channel = str(event.get("channel") or "")
    actor = str(event.get("user") or "user")
    thread_ts = event.get("thread_ts")
    if not text:
        return

    _react(client, channel, event, add=True)
    stream = _Streamer(client, channel, thread_ts)

    agent_reply = _try_agent(cfg, text, channel=channel, stream=stream)
    if agent_reply is not None:
        _react(client, channel, event, add=False)
        if not stream.finish(agent_reply):
            _safe_say(say, agent_reply, thread_ts=thread_ts)
        return

    try:
        conn = db.open_db(cfg)  # sqlite3 커넥션은 스레드 간 공유가 안 된다
        try:
            result = converse(
                conn,
                cfg,
                LLMClient(cfg, conn=conn),
                user_text=text,
                day=_today(cfg),
                channel=channel,
                actor=actor,
                history=_get_history(channel),
                on_progress=stream.on_progress,
            )
            reply = result.text
            if result.ok:
                _append_history(channel, text, reply)
            logger.info(
                "대화 처리 완료 (channel=%s, %dms, 라운드 %d, 툴 %s, 트리거 %s)",
                channel,
                result.latency_ms,
                result.rounds,
                result.tools_used,
                result.triggers_fired,
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - 무응답이 최악이다. 반드시 뭐라도 답한다.
        logger.exception("대화 처리 중 예외 (channel=%s)", channel)
        reply = "처리 중 오류가 났습니다. 로그를 확인해 주세요."
    finally:
        _react(client, channel, event, add=False)

    # ★ 스트리밍으로 무엇이 보였든 **최종본으로 덮어쓴다.** 중간 글은 진행 표시일
    #   뿐이고 사실은 `result.text` 다 (재생성·반복 감지가 본문을 바꿀 수 있다).
    if not stream.finish(reply):
        _safe_say(say, reply, thread_ts=thread_ts)


def _try_agent(cfg: Config, text: str, *, channel: str, stream: "_Streamer") -> str | None:
    """설정이 켜져 있으면 에이전트에게 넘긴다. 넘기지 않았거나 실패하면 None.

    None 을 돌려주면 호출부가 `converse()` 로 간다 — 사용자는 어느 쪽이든 답을 받는다.
    """
    from lifetrainer.agent import delegate
    from lifetrainer.agent.config import load_settings

    try:
        settings = load_settings(cfg)
    except Exception:  # noqa: BLE001 - 설정 하나 때문에 대화가 죽으면 안 된다
        logger.exception("에이전트 설정을 읽지 못했습니다 — 대화 경로로 갑니다")
        return None

    if not (settings.enabled and settings.slack):
        return None
    if not delegate.available(settings.openclaw_bin):
        # ★ 이 경고가 실제로 찍혔는데 아무도 안 봤다. 답이 빠르고 그럴듯해서
        #   강등된 줄 몰랐다. `lt doctor` 가 같은 것을 본다 — 로그만으로는 부족하다.
        logger.warning(
            "openclaw CLI 를 찾지 못해 대화 경로로 갑니다 (channel=%s) — `lt doctor` 로 확인하세요",
            channel,
        )
        return None

    ticker = _Ticker(stream)
    ticker.start()
    try:
        reply = delegate.ask(
            cfg, text,
            channel=channel,
            agent_id=settings.agent_id,
            timeout_sec=settings.slack_timeout_sec,
            openclaw_bin=settings.openclaw_bin,
        )
    except delegate.DelegateError as exc:
        logger.warning("에이전트 위임 실패(%s) — 대화 경로로 갑니다 (channel=%s)", exc, channel)
        return None
    except Exception:  # noqa: BLE001
        logger.exception("에이전트 위임 중 예외 — 대화 경로로 갑니다 (channel=%s)", channel)
        return None
    finally:
        ticker.stop()

    if not reply.ok:
        # 타임아웃·빈 답·파싱 실패. **여기서는 안 내려간다** — 이미 수십 초를 썼고,
        # 또 한 바퀴를 돌리면 사용자가 1분 넘게 기다린다. 무슨 일이 있었는지 말한다.
        return reply.text
    return reply.text


class _Ticker:
    """에이전트가 도는 동안 "몇 초째"를 보여준다.

    ## 왜 필요한가

    에이전트 한 턴이 실측 24~87초다. `_Streamer` 는 생성 중인 **본문**을 고쳐 쓰지만,
    에이전트는 CLI 가 끝날 때 한 번에 답을 준다 — 중간 글이 없다. 그래서 화면이
    통째로 비어 있고, 사용자는 봇이 죽은 줄 안다.

    총 시간은 안 줄지만 **살아 있다는 것이 보인다.** `_Streamer` 와 같은 판단이다.

    ## 한도

    `chat.update` 는 Tier 3(분당 약 50회)다. 8초 간격이면 분당 7.5회라 넉넉하다.
    """

    INTERVAL_SEC = 8.0

    def __init__(self, stream: "_Streamer") -> None:
        self._stream = stream
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        started = time.monotonic()
        # 첫 줄은 즉시. 기다리는 이유를 같이 말한다 — 느린 것이 고장으로 안 보이게.
        self._write("🤔 에이전트가 처리 중입니다… (보통 30초 안팎)")
        while not self._stop.wait(self.INTERVAL_SEC):
            elapsed = int(time.monotonic() - started)
            self._write(f"🤔 에이전트가 처리 중입니다… {elapsed}초")

    def _write(self, text: str) -> None:
        try:
            self._stream.on_progress(text)
        except Exception:  # noqa: BLE001 - 진행 표시가 대화를 죽이면 안 된다
            logger.debug("진행 표시 갱신 실패", exc_info=True)


class _Streamer:
    """생성 중인 답을 Slack 메시지 하나에 고쳐 쓴다.

    ## 왜

    이 기기는 생성이 9.4 tok/s 다. 한 턴이 15~25초인데 그동안 화면이 비어 있으면
    죽은 것처럼 보인다. **총 시간은 안 줄지만 첫 글자가 보이는 시각이 당겨진다** —
    실측으로 12.9초 → 7.1초.

    ## 지키는 것 셋

    ① **실패해도 대화를 죽이지 않는다.** 스트리밍은 편의 기능이다. 게시·수정이
       실패하면 조용히 포기하고(`self._ts = None`) 평소 경로로 한 번에 답한다.
    ② **최종본이 사실이다.** `finish()` 가 `ConverseResult.text` 로 덮어쓴다.
       재생성·반복 감지가 본문을 바꿀 수 있어서, 중간 글을 그대로 두면 안 된다.
    ③ **한도를 지킨다.** `chat.update` 는 Tier 3(분당 약 50회)다. `_STREAM_UPDATE_SEC`
       간격으로 묶어 보낸다 — 더 촘촘히 하면 429 를 맞고 갱신이 아예 끊긴다.
    """

    def __init__(self, client: Any, channel: str, thread_ts: str | None) -> None:
        self._client = client
        self._channel = channel
        self._thread_ts = thread_ts
        self._ts: str | None = None
        self._last_sent = 0.0
        self._last_text = ""

    def on_progress(self, partial: str) -> None:
        if not self._client or not self._channel:
            return
        now = time.monotonic()
        if not partial.strip():
            return  # 라운드 시작(빈 문자열)에는 아무것도 하지 않는다
        if self._ts is not None and now - self._last_sent < _STREAM_UPDATE_SEC:
            return
        if partial == self._last_text:
            return
        self._last_text = partial
        self._last_sent = now
        self._write(partial + _STREAM_CURSOR)

    def finish(self, final_text: str) -> bool:
        """최종본으로 덮어쓴다. 게시한 적이 없으면 False — 호출부가 평소대로 답한다."""
        if self._ts is None:
            return False
        return self._write(final_text)

    def _write(self, text: str) -> bool:
        try:
            if self._ts is None:
                kw = {"thread_ts": self._thread_ts} if self._thread_ts else {}
                resp = self._client.chat_postMessage(channel=self._channel, text=text, **kw)
                self._ts = (resp or {}).get("ts")
                return self._ts is not None
            self._client.chat_update(channel=self._channel, ts=self._ts, text=text)
            return True
        except Exception as exc:  # noqa: BLE001 - 스트리밍 때문에 답을 잃으면 안 된다
            logger.debug("스트리밍 갱신 실패 (평소 경로로 넘어간다): %s", exc)
            self._ts = None
            return False


def _react(client: Any, channel: str, event: dict, *, add: bool) -> None:
    """생각 중 표시. 실패해도 대화 자체는 계속돼야 하므로 예외를 삼킨다."""
    ts = event.get("ts")
    if not (client and channel and ts):
        return
    try:
        fn = client.reactions_add if add else client.reactions_remove
        fn(channel=channel, timestamp=ts, name=_THINKING_EMOJI)
    except Exception as exc:  # noqa: BLE001
        logger.debug("반응 %s 실패: %s", "추가" if add else "제거", exc)


def _safe_say(say: Any, text: str, *, thread_ts: str | None = None) -> None:
    try:
        if thread_ts:
            say(text=text, thread_ts=thread_ts)
        else:
            say(text=text)
    except Exception:  # noqa: BLE001
        logger.exception("대화 응답 발송 실패")


def _get_history(channel: str) -> list[dict]:
    with _conv_lock:
        return list(_history.get(channel, []))


def _append_history(channel: str, user_text: str, reply: str) -> None:
    with _conv_lock:
        turns = _history.setdefault(channel, [])
        turns.append({"role": "user", "content": user_text})
        turns.append({"role": "assistant", "content": reply})
        del turns[: max(0, len(turns) - _HISTORY_TURNS * 2)]


def _register_actions(app: App, cfg: Config) -> None:
    """`/del` 의 Block Kit 인터랙션(체크박스·버튼) 처리(계약서-v2 §5, SPEC §7-2)."""

    @app.action("del_select")
    def _handle_del_select(ack) -> None:  # noqa: ANN001
        # 체크박스 토글 자체는 서버 상태를 바꾸지 않는다 — Slack 이 3초 안에
        # ack 를 요구하므로 즉시 확인만 하고, 실제 삭제는 [삭제] 버튼 클릭 때 한다.
        ack()

    @app.action("open_planner")
    def _handle_open_planner(ack) -> None:  # noqa: ANN001
        # URL 버튼도 Slack 이 block_actions 를 보낼 수 있다. 브라우저 이동은 Slack 이
        # 처리하므로 서버에서는 3초 안에 확인만 한다.
        ack()

    @app.action("del_cancel")
    def _handle_del_cancel(ack, respond) -> None:  # noqa: ANN001
        ack()
        _safe_respond(respond, "삭제를 취소했습니다.", blocks=[], replace_original=True)

    @app.action("del_confirm")
    def _handle_del_confirm(ack, body, respond) -> None:  # noqa: ANN001
        ack()
        threading.Thread(target=_dispatch_del_confirm, args=(cfg, body, respond), daemon=True).start()

    @app.action("del_undo")
    def _handle_del_undo(ack, action, respond) -> None:  # noqa: ANN001
        ack()
        threading.Thread(target=_dispatch_del_undo, args=(cfg, action, respond), daemon=True).start()


# ── /lt 처리 ──────────────────────────────────────────────────────────


def _dispatch_lt(cfg: Config, sub: str, respond) -> None:
    try:
        if sub == "ping":
            respond(f"pong · lifetrainer 0.1.0 · db={cfg.db_path}")
            return
        conn = db.open_db(cfg)
        try:
            if sub == "today":
                _respond_daily(cfg, conn, "today", respond)
            elif sub == "yesterday":
                _respond_daily(cfg, conn, "yesterday", respond)
            elif sub == "week":
                _respond_weekly(cfg, conn, respond)
            elif sub == "status":
                _respond_status(cfg, conn, respond)
            else:
                respond(f"알 수 없는 명령: {sub!r}. {_LT_USAGE}")
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - 핸들러 스레드가 죽어도 사용자에게는 응답을 남긴다
        logger.exception("/lt %s 처리 중 오류", sub)
        _safe_respond(respond, "내부 오류가 발생했습니다. 서버 로그를 확인하세요.")


def _respond_daily(cfg: Config, conn, which: str, respond) -> None:
    today = timeutil.day_str(timeutil.now_ts(), cfg.tz)
    if which == "yesterday":
        day = (_dt.date.fromisoformat(today) - _dt.timedelta(days=1)).isoformat()
    else:
        day = today

    try:
        from lifetrainer.report import daily as daily_mod
        from lifetrainer.rollup.classify import Classifier
    except ImportError:
        respond("리포트 모듈(report.daily / rollup.classify)이 아직 준비되지 않았습니다. 나중에 다시 시도하세요.")
        return

    try:
        classifier = Classifier.from_yaml(cfg.rollup.rules_path)
        built = daily_mod.build_daily(conn, cfg, classifier, day)
        respond(text=built.text, blocks=built.blocks or None)
    except Exception:
        logger.exception("일일 리포트(%s) 생성 실패", day)
        _safe_respond(respond, f"{day} 리포트 생성 중 오류가 발생했습니다.")


def _respond_weekly(cfg: Config, conn, respond) -> None:
    try:
        from lifetrainer.report import daily as daily_mod
        from lifetrainer.rollup.classify import Classifier
    except ImportError:
        respond("리포트 모듈(report.daily / rollup.classify)이 아직 준비되지 않았습니다. 나중에 다시 시도하세요.")
        return

    try:
        classifier = Classifier.from_yaml(cfg.rollup.rules_path)
        end_day = timeutil.day_str(timeutil.now_ts(), cfg.tz)
        built = daily_mod.build_weekly(conn, cfg, classifier, end_day)
        respond(text=built.text, blocks=built.blocks or None)
    except Exception:
        logger.exception("주간 리포트 생성 실패")
        _safe_respond(respond, "주간 리포트 생성 중 오류가 발생했습니다.")


def _respond_status(cfg: Config, conn, respond) -> None:
    def _scalar(sql: str, default: Any = None) -> Any:
        try:
            row = conn.execute(sql).fetchone()
            return row[0] if row is not None and row[0] is not None else default
        except Exception:
            return default

    event_count = _scalar("SELECT COUNT(*) FROM aw_event", 0)
    last_ts = _scalar("SELECT MAX(ts) FROM aw_event")
    last_str = timeutil.iso_utc(float(last_ts)) if last_ts is not None else "없음"

    try:
        rows = conn.execute("SELECT state, COUNT(*) AS c FROM job GROUP BY state").fetchall()
        queue_summary = ", ".join(f"{r['state']}={r['c']}" for r in rows) if rows else "없음"
    except Exception:
        queue_summary = "알 수 없음"

    respond(f"마지막 이벤트: {last_str} · 총 이벤트 {event_count}개 · 큐: {queue_summary}")


# ── /log 처리 ─────────────────────────────────────────────────────────


def _dispatch_log(cfg: Config, command: dict, respond) -> None:
    try:
        text = (command.get("text") or "").strip()
        if not text:
            respond(_LOG_USAGE)
            return
        parts = text.split()
        if len(parts) < 2:
            respond(_LOG_USAGE)
            return

        category = parts[0]
        duration_token = parts[1]
        rest = parts[2:]

        at_time: str | None = None
        note_parts: list[str] = []
        i = 0
        while i < len(rest):
            if rest[i] == "at" and i + 1 < len(rest):
                at_time = rest[i + 1]
                i += 2
                continue
            note_parts.append(rest[i])
            i += 1
        note = " ".join(note_parts) or None

        try:
            duration_sec = timeutil.parse_duration(duration_token)
        except ValueError:
            respond(f"기간을 해석할 수 없습니다: {duration_token!r}. 예: 60m, 1h30m, 90")
            return

        if at_time:
            try:
                hh_str, mm_str = at_time.split(":")
                hh, mm = int(hh_str), int(mm_str)
            except ValueError:
                respond(f"시각을 해석할 수 없습니다: {at_time!r}. 형식: HH:MM")
                return
            day = timeutil.day_str(timeutil.now_ts(), cfg.tz)
            day_start, _day_end = timeutil.day_bounds(day, cfg.tz)
            end_ts = day_start + hh * 3600 + mm * 60
        else:
            end_ts = timeutil.now_ts()
        start_ts = end_ts - duration_sec

        conn = db.open_db(cfg)
        try:
            actor = command.get("user_id")
            with db.transaction(conn):
                conn.execute(
                    "INSERT INTO manual_entry"
                    "(start_ts, end_ts, category, subcategory, note, source, actor, revoked, created_at) "
                    "VALUES (?, ?, ?, NULL, ?, 'slack', ?, 0, ?)",
                    (start_ts, end_ts, category, note, actor, timeutil.now_ts()),
                )
        finally:
            conn.close()

        note_disp = note or "메모 없음"
        respond(f"기록됨: {category} · {duration_token} ({note_disp})")
    except Exception:  # noqa: BLE001
        logger.exception("/log 처리 중 오류")
        _safe_respond(respond, "기록 중 오류가 발생했습니다. 서버 로그를 확인하세요.")


# ── 공통 헬퍼 ─────────────────────────────────────────────────────────


def _today(cfg: Config) -> str:
    """논리적 오늘 날짜. 하루 경계가 06:00 이므로 반드시 `boundary_hour` 를 넘긴다."""
    return timeutil.day_str(timeutil.now_ts(), cfg.tz, boundary_hour=cfg.rollup.day_boundary_hour)


def _build_notifier(cfg: Config) -> SlackNotifier:
    """`SlackNotifier` 생성 팩토리. 테스트는 이 함수를 monkeypatch 해 가짜 client 를 주입한다."""
    return SlackNotifier(cfg)


def _resolve_subject_id(conn, name: str) -> int | None:
    """subject 이름 -> id. 등록된 subject 가 없으면 None(달성률 판정은 category 없이 진행)."""
    from lifetrainer.plan.subjects import list_subjects

    for s in list_subjects(conn):
        if s["name"] == name:
            return int(s["id"])
    return None


def _resolve_view_day(cfg: Config, arg: str) -> str:
    """`/view` 인자(없음 | '어제' | 'YYYY-MM-DD') -> 논리적 날짜 문자열."""
    today = _today(cfg)
    if not arg:
        return today
    if arg in ("어제", "yesterday"):
        return (_dt.date.fromisoformat(today) - _dt.timedelta(days=1)).isoformat()
    try:
        _dt.date.fromisoformat(arg)
    except ValueError as exc:
        raise ValueError(f"날짜를 해석할 수 없습니다: {arg!r} (형식: 어제 | YYYY-MM-DD)") from exc
    return arg


def _schedule_undo_expiry(respond, text: str) -> None:
    """`_UNDO_WINDOW_SEC` 뒤 실행취소 버튼을 제거한다.

    `response_url` 은 30분 안에 최대 5회까지만 쓸 수 있어(조사 문서 §7) 예산을
    아껴야 하지만, 확인(1회) + 만료(1회, 사용자가 직접 취소를 안 눌렀을 때만)
    정도는 넉넉히 안쪽이다. 데몬 스레드라 프로세스 종료를 막지 않는다.
    """
    timer = threading.Timer(_UNDO_WINDOW_SEC, _expire_undo, args=(respond, text))
    timer.daemon = True
    timer.start()


def _expire_undo(respond, text: str) -> None:
    _safe_respond(respond, text, blocks=[], replace_original=True)


# ── /plan 처리 ────────────────────────────────────────────────────────


def _dispatch_plan(cfg: Config, text: str, respond, day: str | None = None) -> None:
    """`/plan` 처리. `day` 를 주면 그 날짜에 넣는다 (기본은 논리적 오늘).

    ★ `day` 는 Slack 이 아니라 **에이전트를 위해** 있다. Slack 에서 `/plan` 을 치는
    사람은 오늘 것을 넣지만, 에이전트는 "내일 09시에 딥워크 넣어줘" 를 받는다.
    이 인자가 없을 때 그 요청이 **오늘에 조용히 들어갔다** — 에러도 안 났고
    "추가했습니다" 라고 답했다. `agent/slash.py` 가 넘긴다.
    """
    try:
        if not text:
            respond(_PLAN_USAGE)
            return

        try:
            tasks = commands_mod.parse_plan_text(text)
        except ValueError as exc:
            respond(f"입력을 해석할 수 없습니다: {exc}")
            return
        if not tasks:
            respond("추가할 태스크를 찾지 못했습니다.")
            return

        conn = db.open_db(cfg)
        try:
            day = day or _today(cfg)
            now_dt = _dt.datetime.fromtimestamp(timeutil.now_ts(), tz=cfg.tz)
            default_start = now_dt.hour * 60 + now_dt.minute

            added: list[str] = []
            failed: list[str] = []
            for task in tasks:
                try:
                    subject_id = _resolve_subject_id(conn, task.subject) if task.subject else None

                    if task.start_min is not None and task.end_min is not None:
                        start_min, end_min = task.start_min, task.end_min
                    else:
                        duration = task.minutes if task.minutes is not None else _DEFAULT_TASK_MINUTES
                        start_min = min(default_start, 1440 - cfg.rollup.slot_minutes)
                        end_min = min(1440, start_min + max(duration, cfg.rollup.slot_minutes))

                    priority = task.priority if task.priority in _VALID_PRIORITIES else "normal"

                    plan_models.add_instance(
                        conn,
                        cfg,
                        day,
                        title=task.title,
                        start_min=start_min,
                        end_min=end_min,
                        subject_id=subject_id,
                        priority=priority,
                        source="manual",
                    )
                    added.append(task.title)
                except ValueError as exc:
                    failed.append(f"{task.title or text}: {exc}")
        finally:
            conn.close()

        lines = [f"계획을 추가했습니다 ({day}):"] + [f"• {t}" for t in added]
        if failed:
            lines.append("추가하지 못한 항목:")
            lines += [f"• {f}" for f in failed]
        respond("\n".join(lines))
    except Exception:  # noqa: BLE001
        logger.exception("/plan 처리 중 오류")
        _safe_respond(respond, "계획 추가 중 오류가 발생했습니다. 서버 로그를 확인하세요.")


# ── /del 처리 (SPEC §7-2) ────────────────────────────────────────────


def _dispatch_del(cfg: Config, command: dict, respond, day: str | None = None) -> None:
    """`day` 를 주면 그 날짜의 목록을 대상으로 한다 (기본은 논리적 오늘).
    `_dispatch_plan` 과 같은 이유로 있다 — 에이전트는 다른 날을 다룬다."""
    try:
        text = (command.get("text") or "").strip()
        conn = db.open_db(cfg)
        try:
            day = day or _today(cfg)

            if text:
                try:
                    ordinals = commands_mod.parse_index_list(text)
                except ValueError as exc:
                    respond(str(exc))
                    return

                by_ordinal = {
                    row.ordinal: row
                    for row in plan_models.list_instances(conn, day, include_archived=False, include_done=True)
                }
                archived_ids: list[int] = []
                found_ordinals: list[int] = []
                missing: list[int] = []
                for n in ordinals:
                    row = by_ordinal.get(n)
                    if row is None:
                        missing.append(n)
                        continue
                    plan_models.archive_instance(conn, row.id)
                    archived_ids.append(row.id)
                    found_ordinals.append(n)

                if not archived_ids:
                    respond(f"삭제할 항목을 찾지 못했습니다: {ordinals} (오늘 목록에 없음)")
                    return

                msg = f"{', '.join(str(n) for n in found_ordinals)}번 삭제됨"
                if missing:
                    msg += f" (없음: {', '.join(str(n) for n in missing)})"
                respond(text=msg, blocks=blocks_mod.del_result_blocks(msg, archived_ids))
                _schedule_undo_expiry(respond, msg)
                return

            # 인자 없음: 체크박스 목록. 완료(done) 항목은 기본 숨김 —
            # 실적이 사라지면 주간 통계가 깨진다(SPEC §7-2).
            rows = plan_models.list_instances(conn, day, include_archived=False, include_done=False)
            if not rows:
                respond("오늘 삭제할 계획이 없습니다 (완료된 항목은 숨겨집니다).")
                return
            respond(text=f"삭제할 계획을 선택하세요 ({day})", blocks=blocks_mod.del_picker_blocks(day, rows))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.exception("/del 처리 중 오류")
        _safe_respond(respond, "삭제 처리 중 오류가 발생했습니다. 서버 로그를 확인하세요.")


def _dispatch_del_confirm(cfg: Config, body: dict, respond) -> None:
    """`/del` 체크박스 목록의 [삭제] 버튼(`del_confirm`) 처리."""
    try:
        state_values = (body.get("state") or {}).get("values") or {}
        selected = ((state_values.get("del_picker") or {}).get("del_select") or {}).get("selected_options") or []
        ids = [int(opt["value"]) for opt in selected]
    except Exception:  # noqa: BLE001
        logger.exception("/del 체크박스 상태 파싱 실패")
        _safe_respond(respond, "선택 상태를 읽는 중 오류가 발생했습니다.")
        return

    if not ids:
        _safe_respond(respond, "선택된 항목이 없습니다. 체크박스를 먼저 선택하세요.")
        return

    conn = db.open_db(cfg)
    try:
        for instance_id in ids:
            plan_models.archive_instance(conn, instance_id)
    finally:
        conn.close()

    text = f"{len(ids)}개 항목을 삭제했습니다"
    _safe_respond(respond, text, blocks=blocks_mod.del_result_blocks(text, ids), replace_original=True)
    _schedule_undo_expiry(respond, text)


def _dispatch_del_undo(cfg: Config, action: dict, respond) -> None:
    """실행취소 버튼(`del_undo`) 처리 — `unarchive_instance` 로 되살린다."""
    raw = (action or {}).get("value", "")
    try:
        ids = [int(v) for v in raw.split(",") if v]
    except ValueError:
        ids = []

    if not ids:
        _safe_respond(respond, "복구할 항목을 찾지 못했습니다.")
        return

    conn = db.open_db(cfg)
    try:
        for instance_id in ids:
            plan_models.unarchive_instance(conn, instance_id)
    finally:
        conn.close()

    _safe_respond(respond, "실행취소했습니다 — 삭제를 되돌렸습니다.", blocks=[], replace_original=True)


# ── /view 처리 ────────────────────────────────────────────────────────


def _dispatch_view(cfg: Config, command: dict, respond) -> None:
    try:
        arg = (command.get("text") or "").strip()
        try:
            day = _resolve_view_day(cfg, arg)
        except ValueError as exc:
            respond(str(exc))
            return

        conn = db.open_db(cfg)
        try:
            try:
                from lifetrainer.plan.achieve import day_achievement
                from lifetrainer.report.planner import render_planner_day
            except ImportError:
                respond("플래너 렌더러(report.planner)가 아직 준비되지 않았습니다. 나중에 다시 시도하세요.")
                return

            try:
                png_path = render_planner_day(conn, cfg, day)
            except Exception:
                logger.exception("플래너 PNG 생성 실패 (day=%s)", day)
                respond(f"{day} 플래너 PNG 생성 중 오류가 발생했습니다.")
                return

            overall, achieved, total = day_achievement(conn, cfg, day)
        finally:
            conn.close()

        planner_url = web_auth.signed_planner_url(cfg, str(command.get("user_id") or "slack-user"), day)
        card_blocks = blocks_mod.planner_card_blocks(day, overall, achieved, total, planner_url)
        channel = command.get("channel_id")

        notifier = _build_notifier(cfg)
        # share=False — 아래에서 이미지 블록으로 싣는다. 채널 공유까지 하면
        # 파일 메시지 + 카드 이미지로 **같은 그림이 두 번 뜬다** (2026-08-19 사고).
        upload = notifier.upload_png(png_path, title=f"플래너 {day}", channel=channel, share=False)
        file_id = (upload or {}).get("file", {}).get("id") if isinstance(upload, dict) else None
        if file_id:
            card_blocks = [*card_blocks, blocks_mod.image_block(str(file_id), title=f"플래너 {day}", alt=f"{day} 플래너")]

        ts = notifier.post(f"플래너 — {day}", blocks=card_blocks, channel=channel)
        if not ts:
            # 발송 비활성(토큰 없음)이거나 채널 미지정이면 ephemeral 로라도 결과를 남긴다.
            respond(text=f"플래너 — {day}", blocks=card_blocks)
    except Exception:  # noqa: BLE001
        logger.exception("/view 처리 중 오류")
        _safe_respond(respond, "플래너를 불러오는 중 오류가 발생했습니다. 서버 로그를 확인하세요.")


# ── 계획 하나 고르기 (번호 또는 제목) ──────────────────────────────────


class _PickError(Exception):
    """고를 수 없다. 메시지가 그대로 사용자(또는 모델)에게 간다."""


def _pick_instance(text: str, rows: list) -> list:
    """`/done 3` 의 "3" 또는 `/done 집중근무` 의 "집중근무" 로 인스턴스 하나를 고른다.

    ## 왜 제목도 받나 — 실측 사고

    "오늘 목록 보여주고 **'집중근무'** 완료 처리해줘" 에 8B 가 목록도 안 보고
    `/done 1` 을 불렀다. 1번은 '근무' 였다 — **엉뚱한 계획이 완료로 바뀌었고
    에러는 안 났다.** 모델이 번호를 **추측**한 것이다.

    번호밖에 못 받으면 모델은 이름을 번호로 옮겨야 하고, 그 변환이 곧 추측이다.
    이름을 그대로 받으면 변환할 일이 없다 — **모델에게 시키지 않는 것이 답이다**
    (이 저장소가 날짜에서 이미 배운 것과 같다: `catalog.py` 의 `day` 주석).

    ## 애매하면 거절한다

    같은 제목이 둘 이상이면 **아무것도 안 바꾸고** 후보를 보여준다. 하나를 골라
    바꾸면 그게 또 추측이다. 이때의 되묻기는 정당하다 — 프롬프트도 그렇게 적혀 있다.
    """
    needle = text.strip()
    if not needle:
        raise _PickError("무엇을 바꿀지 지정하세요 (번호 또는 제목).")

    # ★ **복수 번호를 조용히 첫 개만 처리하지 않는다.**
    #   실측 사고: "2번 3번 완료처리해줘" 에 모델이 `/done 2 3` 을 불렀고, 예전
    #   코드는 `text.split()[0]` 으로 **2번만** 바꾸고 성공했다고 답했다.
    #   사용자는 둘 다 됐다고 믿었다. 조용히 절반만 하는 것이 최악이다.
    #   `/del` 이 이미 `parse_index_list` 로 복수를 받으므로 같은 파서를 쓴다 —
    #   명령마다 다른 파서를 쓰면 두 명령이 조용히 달라진다.
    if _looks_like_indices(needle):
        try:
            ordinals = commands_mod.parse_index_list(needle)
        except ValueError as exc:
            raise _PickError(str(exc)) from exc
        by_ordinal = {row.ordinal: row for row in rows}
        # ★ **`plan_instance.id` 도 받는다.**
        #
        #   `llm/tools.get_plans` 가 목록을 `[134] 15:00~15:30 어제표식` 으로 싣는다.
        #   목록에 번호가 붙어 있으니 **모델은 그 번호로 명령한다** — 채점에서
        #   `/done 134` 를 3/3 로 봤고, ordinal 만 받던 시절에는 3/3 실패했다.
        #   답변은 "먼저 /view 로 번호를 확인해 주세요" 로 사용자에게 떠넘겼다.
        #
        #   모델을 고치려고 세 번(ordinal 교체·`N번` 표기·번호 제거) 시도해 세 번
        #   더 나빠졌다. **모델이 이미 보내는 것을 우리가 받는 편이 값싸다.**
        #
        #   ordinal 이 먼저다 — 사람이 `/done 2` 라고 칠 때는 화면의 2번을 뜻하고,
        #   그게 우연히 다른 계획의 id 여도 사람 의도가 이긴다.
        by_id = {row.id: row for row in rows if row.id not in by_ordinal}
        picked = [by_ordinal.get(n) or by_id[n] for n in ordinals if n in by_ordinal or n in by_id]
        missing = [n for n in ordinals if n not in by_ordinal and n not in by_id]
        if not picked:
            raise _PickError(
                f"{', '.join(str(n) for n in missing)}번 계획을 목록에서 찾을 수 없습니다. "
                "/view 로 번호를 확인하세요."
            )
        if missing:
            raise _PickError(
                f"{', '.join(str(n) for n in missing)}번이 목록에 없습니다. "
                "있는 것만 바꾸지 않고 멈췄습니다 — /view 로 확인하고 다시 지정하세요."
            )
        return picked

    lowered = needle.casefold()
    exact = [r for r in rows if r.title.casefold() == lowered]
    partial = exact or [r for r in rows if lowered in r.title.casefold()]
    if not partial:
        titles = ", ".join(f"{r.ordinal}. {r.title}" for r in rows) or "(없음)"
        raise _PickError(f"'{needle}' 에 해당하는 계획이 없습니다. 목록: {titles}")
    if len(partial) > 1:
        candidates = ", ".join(f"{r.ordinal}. {r.title}" for r in partial)
        raise _PickError(f"'{needle}' 에 해당하는 계획이 여러 개입니다. 번호로 지정하세요: {candidates}")
    return [partial[0]]


_INDEX_ONLY_RE = re.compile(r"^\d+([,\s]+\d+)*$")


def _looks_like_indices(text: str) -> bool:
    """'2' · '2 3' · '2,3' 처럼 **번호만** 있는가. 제목에 숫자가 섞인 것과 구분한다."""
    return bool(_INDEX_ONLY_RE.match(text.strip()))


# ── /done · /doing · /defer 처리 ────────────────────────────────────

def _dispatch_set_status(
    cfg: Config, command_name: str, text: str, respond, actor: str | None, day: str | None = None
) -> None:
    """`day` 를 주면 그 날짜의 번호를 바꾼다 (기본은 논리적 오늘).
    `_dispatch_plan` 과 같은 이유로 있다 — 에이전트는 다른 날을 다룬다."""
    try:
        if not text:
            respond(f"사용법: /{command_name} <번호|제목>  예) /{command_name} 3  ·  /{command_name} 집중근무")
            return

        status = _STATUS_BY_COMMAND[command_name]
        conn = db.open_db(cfg)
        try:
            day = day or _today(cfg)
            rows = plan_models.list_instances(conn, day, include_archived=False, include_done=True)
            try:
                targets = _pick_instance(text, rows)
            except _PickError as exc:
                respond(str(exc))
                return
            try:
                for target in targets:
                    plan_models.set_status(conn, cfg, target.id, status, actor=actor or "user")
            except ValueError as exc:
                respond(str(exc))
                return
        finally:
            conn.close()

        changed = ", ".join(f"{t.ordinal}. {t.title}" for t in targets)
        respond(f"{_STATUS_EMOJI[status]} {changed} — {_STATUS_VERB[status]}")
    except Exception:  # noqa: BLE001
        logger.exception("/%s 처리 중 오류", command_name)
        _safe_respond(respond, "처리 중 오류가 발생했습니다. 서버 로그를 확인하세요.")


# ── /memo 처리 ────────────────────────────────────────────────────────


_PRIVATE_USAGE = (
    "`/private <분>` — 지금부터 그 시간 동안 안 잰다 (예: `/private 60`)\n"
    "`/private off` — 지금 끈다   ·   `/private status` — 지금 상태"
)


def _dispatch_private(cfg: Config, text: str, respond, day: str | None = None) -> None:
    """프라이빗 구간을 켜고·끄고·본다.

    ## ★ 왜 에이전트 툴이 아니라 슬래시인가

    이 경로는 `agent/slash.py` 를 통해 **모델도 부를 수 있다** — `slash` 는 MCP 툴
    13개 중 하나다. 모델이 고르는 것은 *명령 문자열*이고 **실제 동작은 코드가 한다.**

    ★ 그렇다고 "안 부르고 껐습니다로 끝내는" 실패가 사라지는 건 아니다. 막히는 것은
      **부르고 나서 다른 일을 하는 것**이고, 그래서 반환문에 결과 상태를 담는다
      (아래). 프라이빗은 다른 명령과 달리 **사람이 켜졌다고 믿고 행동한다.**

    ## 그래서 답에 항상 결과 상태를 담는다

    "껐습니다" 가 아니라 **"프라이빗: 꺼짐"** 이라고 답한다. 모델이 이 문자열을 그대로
    인용하게 만드는 것이 목적이다 — 요청과 결과가 어긋나면 사람 눈에 보여야 한다.

    ★ `day` 는 안 쓴다. 프라이빗은 **지금 이 순간부터**의 구간이라 날짜 인자가 없다.
      `_delegate` 가 모든 명령에 `day` 를 넘기므로 받아만 두고 버린다.
    """
    from lifetrainer import privacy

    arg = (text or "").strip().lower()
    try:
        conn = db.open_db(cfg)
        try:
            if arg in ("off", "끄기", "꺼"):
                st = privacy.end_now(conn)
                tail = "\n  끈 시각부터 다시 기록됩니다 — 구간 안의 기록은 돌아오지 않습니다."
            elif arg in ("", "status", "상태"):
                st = privacy.state(conn)
                tail = "" if arg else "\n" + _PRIVATE_USAGE
            else:
                try:
                    minutes = int(arg.rstrip("분m"))
                except ValueError:
                    respond(_PRIVATE_USAGE)
                    return
                if not (0 < minutes <= cfg.private.max_minutes):
                    respond(f"분은 0 보다 크고 {cfg.private.max_minutes} 이하여야 합니다.")
                    return
                st = privacy.begin(conn, minutes, source="slack")
                tail = "\n  이 시간의 창 제목·앱 이름은 저장되지 않습니다."
        finally:
            conn.close()

        if st.active:
            left = max(0.0, st.until_ts - st.server_ts)
            line = f"프라이빗: 켜짐 — {left / 60:.0f}분 남음"
        else:
            line = "프라이빗: 꺼짐"
        respond(line + tail)
    except Exception:
        logger.exception("/private 처리 실패")
        respond("프라이빗 상태를 바꾸지 못했습니다. 지금 상태는 `/private status` 로 확인하세요.")


def _dispatch_memo(cfg: Config, text: str, respond, day: str | None = None) -> None:
    """`day` 를 주면 그 날짜의 메모다 (기본은 논리적 오늘).
    `_dispatch_plan` 과 같은 이유로 있다 — 에이전트는 다른 날을 다룬다."""
    try:
        if not text:
            respond(_MEMO_USAGE)
            return

        conn = db.open_db(cfg)
        try:
            day = day or _today(cfg)
            now = timeutil.now_ts()
            # `day` 테이블은 이미 스키마에 있다(계약서-v2 §3-B) — memo 컬럼에 upsert 만 한다.
            with db.transaction(conn) as tx:
                tx.execute(
                    "INSERT INTO day(day, memo, created_at, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(day) DO UPDATE SET memo = excluded.memo, updated_at = excluded.updated_at",
                    (day, text, now, now),
                )
        finally:
            conn.close()

        respond(f"메모를 저장했습니다 ({day}): {text}")
    except Exception:  # noqa: BLE001
        logger.exception("/memo 처리 중 오류")
        _safe_respond(respond, "메모 저장 중 오류가 발생했습니다. 서버 로그를 확인하세요.")


# ── /week 처리 ────────────────────────────────────────────────────────


def _dispatch_week(cfg: Config, command: dict, respond) -> None:
    try:
        conn = db.open_db(cfg)
        try:
            try:
                from lifetrainer.report import daily as daily_mod
                from lifetrainer.rollup.classify import Classifier
            except ImportError:
                respond("리포트 모듈(report.daily / rollup.classify)이 아직 준비되지 않았습니다. 나중에 다시 시도하세요.")
                return

            try:
                classifier = Classifier.from_yaml(cfg.rollup.rules_path)
                end_day = _today(cfg)
                built = daily_mod.build_weekly(conn, cfg, classifier, end_day)
            except Exception:
                logger.exception("주간 리포트 생성 실패")
                respond("주간 리포트 생성 중 오류가 발생했습니다.")
                return

            notifier = _build_notifier(cfg)
            channel = command.get("channel_id")
            ts = notifier.post_report(conn, built, channel=channel)
            if not ts:
                respond(text=built.text, blocks=built.blocks or None)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.exception("/week 처리 중 오류")
        _safe_respond(respond, "주간 리포트 처리 중 오류가 발생했습니다. 서버 로그를 확인하세요.")


def _safe_respond(respond, text: str, **kwargs: Any) -> None:
    """respond() 자체가 실패해도(예: response_url 만료) 스레드를 죽이지 않는다."""
    try:
        respond(text=text, **kwargs)
    except Exception:  # noqa: BLE001
        logger.exception("Slack respond() 실패")
