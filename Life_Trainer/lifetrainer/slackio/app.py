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
from typing import Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from lifetrainer import db, timeutil
from lifetrainer.config import Config
from lifetrainer.plan import models as plan_models
from lifetrainer.slackio import blocks as blocks_mod
from lifetrainer.slackio import commands as commands_mod
from lifetrainer.slackio.notify import SlackNotifier

logger = logging.getLogger(__name__)

_LT_USAGE = "사용법: /lt ping|today|yesterday|week|status"
_LOG_USAGE = "사용법: /log <카테고리> <기간> [메모]  예) /log 운동 60m 헬스장  (기간 뒤에 `at HH:MM` 을 붙이면 그 시각을 종료 기준으로 삼는다)"
_PLAN_USAGE = "사용법: /plan <텍스트>  예) /plan 예시 작업 A #수학 @60m !high"
_MEMO_USAGE = "사용법: /memo <텍스트>  예) /memo 예시 자료 다운로드"

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
    """자연어 한 턴을 처리해 답을 올린다. 어떤 예외에도 반드시 무언가를 답한다."""
    from lifetrainer.llm.client import LLMClient
    from lifetrainer.llm.converse import converse

    text = _MENTION_RE.sub("", str(event.get("text") or "")).strip()
    channel = str(event.get("channel") or "")
    actor = str(event.get("user") or "user")
    thread_ts = event.get("thread_ts")
    if not text:
        return

    _react(client, channel, event, add=True)
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

    _safe_say(say, reply, thread_ts=thread_ts)


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


def _dispatch_plan(cfg: Config, text: str, respond) -> None:
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
            day = _today(cfg)
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


def _dispatch_del(cfg: Config, command: dict, respond) -> None:
    try:
        text = (command.get("text") or "").strip()
        conn = db.open_db(cfg)
        try:
            day = _today(cfg)

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

        card_blocks = blocks_mod.planner_card_blocks(day, overall, achieved, total)
        channel = command.get("channel_id")

        notifier = _build_notifier(cfg)
        upload = notifier.upload_png(png_path, title=f"플래너 {day}", channel=channel)
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


# ── /done · /doing · /defer 처리 ────────────────────────────────────

def _dispatch_set_status(cfg: Config, command_name: str, text: str, respond, actor: str | None) -> None:
    try:
        if not text:
            respond(f"사용법: /{command_name} <번호>  예) /{command_name} 3")
            return
        try:
            ordinal = int(text.split()[0])
        except ValueError:
            respond(f"번호를 해석할 수 없습니다: {text!r}")
            return

        status = _STATUS_BY_COMMAND[command_name]
        conn = db.open_db(cfg)
        try:
            day = _today(cfg)
            target = next(
                (
                    row
                    for row in plan_models.list_instances(conn, day, include_archived=False, include_done=True)
                    if row.ordinal == ordinal
                ),
                None,
            )
            if target is None:
                respond(f"{ordinal}번 계획을 오늘 목록에서 찾을 수 없습니다. /view 로 번호를 확인하세요.")
                return
            try:
                plan_models.set_status(conn, cfg, target.id, status, actor=actor or "user")
            except ValueError as exc:
                respond(str(exc))
                return
        finally:
            conn.close()

        respond(f"{_STATUS_EMOJI[status]} {ordinal}. {target.title} — {_STATUS_VERB[status]}")
    except Exception:  # noqa: BLE001
        logger.exception("/%s 처리 중 오류", command_name)
        _safe_respond(respond, "처리 중 오류가 발생했습니다. 서버 로그를 확인하세요.")


# ── /memo 처리 ────────────────────────────────────────────────────────


def _dispatch_memo(cfg: Config, text: str, respond) -> None:
    try:
        if not text:
            respond(_MEMO_USAGE)
            return

        conn = db.open_db(cfg)
        try:
            day = _today(cfg)
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
