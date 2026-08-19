"""Life Trainer CLI — `lt` (담당 H: 배선).

이 모듈은 다른 담당들이 만든 계층을 엮어 하나의 명령행 도구로 노출한다.
Phase 2/3 모듈(`collect.feeds`, `collect.score`, `llm.worker`, `slackio.app` 등)은
서브커맨드 함수 **안에서** 지연 import 한다 — 그중 하나가 깨져 있어도
`lt doctor`/`lt rollup` 같은 Phase 1 명령은 계속 동작해야 한다.

종료 코드: 성공 0, 사용자 오류(잘못된 인자·설정) 2, 런타임 실패(예외) 1.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from lifetrainer import db, timeutil
from lifetrainer.config import Config, load_config, setup_logging

logger = logging.getLogger(__name__)


class CliError(Exception):
    """사용자가 고칠 수 있는 오류 (잘못된 인자, 잘못된 설정). 종료 코드 2."""


# ── argparse 구성 ────────────────────────────────────────────────────────
#
# --config/-v 는 최상위 파서와 모든 서브커맨드 파서 양쪽에 붙인다 (계약서: "모든
# 명령에 --config, -v/--verbose 를 받는다"). 서브커맨드 쪽 정의는 default 를
# argparse.SUPPRESS 로 둬서, 서브커맨드 뒤에 안 붙였을 때 이미 최상위에서 파싱된
# 값을 덮어쓰지 않게 한다 (argparse 서브파서의 흔한 함정).


def _common_parser() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--config", type=str, default=argparse.SUPPRESS, help="설정 파일 경로 (기본: config/lifetrainer.toml)"
    )
    parent.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="DEBUG 로그 레벨"
    )
    return parent


def build_parser() -> argparse.ArgumentParser:
    """`lt` 의 argparse 파서를 만든다."""
    common = _common_parser()

    parser = argparse.ArgumentParser(prog="lt", description="Life Trainer — 개인 생활 로깅 에이전트")
    parser.add_argument("--config", type=str, default=None, help="설정 파일 경로 (기본: config/lifetrainer.toml)")
    parser.add_argument("-v", "--verbose", action="store_true", default=False, help="DEBUG 로그 레벨")

    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", parents=[common], help="환경 점검 (DB, AW, LLM, Slack, 폰트, 디스크, 큐, 검색)")
    p_doctor.set_defaults(func=cmd_doctor)

    p_initdb = sub.add_parser("init-db", parents=[common], help="스키마 생성")
    p_initdb.set_defaults(func=cmd_init_db)

    p_sync = sub.add_parser("sync", parents=[common], help="ActivityWatch 동기화")
    p_sync.set_defaults(func=cmd_sync)

    p_import = sub.add_parser(
        "import", parents=[common], help="폰에서 뽑은 ActivityWatch export 파일 넣기"
    )
    p_import.add_argument("--from", dest="src", required=True, help="JSON 파일 또는 디렉터리")
    p_import.add_argument(
        "--device", default=None, help="기기 이름 (생략하면 버킷 메타의 hostname 에서 추론)"
    )
    p_import.add_argument(
        "--rollup", action="store_true", help="넣은 뒤 해당 날짜들을 다시 롤업한다"
    )
    p_import.set_defaults(func=cmd_import)

    p_synth = sub.add_parser("synth", parents=[common], help="합성 데이터 생성 (AW 없이 파이프라인 검증)")
    p_synth.add_argument("--days", type=int, default=7, help="생성할 일수 (기본 7)")
    p_synth.add_argument("--end-day", type=str, default=None, help="마지막 날짜 'YYYY-MM-DD' (기본: 오늘)")
    p_synth.add_argument("--seed", type=int, default=42, help="재현용 시드")
    p_synth.set_defaults(func=cmd_synth)

    p_rollup = sub.add_parser("rollup", parents=[common], help="10분 슬롯 롤업")
    rollup_group = p_rollup.add_mutually_exclusive_group()
    rollup_group.add_argument("--day", type=str, help="'YYYY-MM-DD'")
    rollup_group.add_argument("--yesterday", action="store_true", help="어제")
    rollup_group.add_argument("--today", action="store_true", help="오늘 (인자 없을 때의 기본값과 동일)")
    rollup_group.add_argument("--range", nargs=2, metavar=("START", "END"), help="시작일 종료일 (양 끝 포함)")
    p_rollup.set_defaults(func=cmd_rollup)

    p_stats = sub.add_parser("stats", parents=[common], help="콘솔 요약")
    p_stats.add_argument("--day", type=str, default=None, help="'YYYY-MM-DD' (기본: 오늘)")
    p_stats.set_defaults(func=cmd_stats)

    p_timeline = sub.add_parser("timeline", parents=[common], help="타임라인 PNG 생성")
    p_timeline.add_argument("--day", type=str, required=True, help="'YYYY-MM-DD'")
    p_timeline.add_argument("--out", type=str, default=None, help="출력 경로 (기본: cfg.report.png_dir)")
    p_timeline.set_defaults(func=cmd_timeline)

    p_report = sub.add_parser("report", parents=[common], help="리포트 조립 (+ 선택적 Slack 발송)")
    report_sub = p_report.add_subparsers(dest="report_kind", required=True)

    p_report_daily = report_sub.add_parser("daily", parents=[common], help="일일 리포트")
    p_report_daily.add_argument("--day", type=str, default=None, help="'YYYY-MM-DD' (기본: 오늘)")
    p_report_daily.add_argument("--post", action="store_true", help="Slack 으로 발송 (없으면 절대 보내지 않음)")
    p_report_daily.set_defaults(func=cmd_report_daily)

    p_report_weekly = report_sub.add_parser("weekly", parents=[common], help="주간 리포트")
    p_report_weekly.add_argument("--end", dest="end_day", type=str, default=None, help="'YYYY-MM-DD' (기본: 오늘)")
    p_report_weekly.add_argument("--post", action="store_true", help="Slack 으로 발송 (없으면 절대 보내지 않음)")
    p_report_weekly.set_defaults(func=cmd_report_weekly)

    p_log = sub.add_parser("log", parents=[common], help="수동 활동 기록 (Slack 없이도)")
    p_log.add_argument("category", type=str, help="카테고리 (예: 운동)")
    p_log.add_argument("period", type=str, help="기간 (예: 60m, 1h30m)")
    p_log.add_argument("note", type=str, nargs="*", help="메모 (선택)")
    p_log.add_argument("--at", type=str, default=None, help="종료 시각 'HH:MM' (기본: 지금)")
    p_log.set_defaults(func=cmd_log)

    p_collect = sub.add_parser("collect", parents=[common], help="피드 수집 1회")
    p_collect.set_defaults(func=cmd_collect)

    p_score = sub.add_parser("score", parents=[common], help="미채점 문서 스코어링")
    p_score.set_defaults(func=cmd_score)

    p_digest = sub.add_parser("digest", parents=[common], help="아침 다이제스트 (키워드 랭킹, LLM 없음)")
    p_digest.add_argument("--post", action="store_true", help="Slack 으로 발송 (없으면 절대 보내지 않음)")
    p_digest.set_defaults(func=cmd_digest)

    p_nightly = sub.add_parser(
        "nightly", parents=[common], help="야간 배치 적재 (문서 요약 + 미분류 태깅을 큐에 넣는다)"
    )
    p_nightly.add_argument("--summaries", type=int, default=None, help="요약할 문서 수 (기본: 설정값)")
    p_nightly.add_argument("--tags", type=int, default=None, help="한 번에 태깅할 미분류 수 (0 이면 건너뜀)")
    p_nightly.add_argument("--dry-run", action="store_true", help="큐에 넣지 않고 대상만 센다")
    p_nightly.add_argument(
        "--stop", action="store_true",
        help="적재 대신 **남은 배치 잡을 큐에서 비운다** (새벽 창이 끝날 때. 알림은 안 건드림)",
    )
    p_nightly.set_defaults(func=cmd_nightly)

    p_worker = sub.add_parser("worker", parents=[common], help="GPU 잡 워커")
    p_worker.add_argument("--once", action="store_true", help="큐를 한 번 비우고 종료 (기본: 상시 폴링)")
    p_worker.set_defaults(func=cmd_worker)

    p_queue = sub.add_parser("queue", parents=[common], help="GPU 잡 큐 상태")
    queue_sub = p_queue.add_subparsers(dest="queue_cmd", required=True)
    p_queue_stats = queue_sub.add_parser("stats", parents=[common], help="상태별 잡 개수")
    p_queue_stats.set_defaults(func=cmd_queue_stats)

    p_backup = sub.add_parser("backup", parents=[common], help="DB 온라인 백업")
    p_backup.add_argument("--out", type=str, default=None, help="출력 경로 (기본: data/backup/lifetrainer-<날짜>.db)")
    p_backup.set_defaults(func=cmd_backup)

    p_slack = sub.add_parser("slack", parents=[common], help="Slack 관련 명령")
    slack_sub = p_slack.add_subparsers(dest="slack_cmd", required=True)
    p_slack_serve = slack_sub.add_parser("serve", parents=[common], help="Socket Mode 상시 실행 (bolt 모드)")
    p_slack_serve.set_defaults(func=cmd_slack_serve)
    p_slack_test = slack_sub.add_parser("test", parents=[common], help="테스트 메시지 발송")
    p_slack_test.add_argument("--channel", type=str, default=None, help="채널 (기본: default_channel)")
    p_slack_test.set_defaults(func=cmd_slack_test)

    # ── plan / slot / planner / web (담당 P4 — 배선, 계약서 §6) ────────────

    p_plan = sub.add_parser("plan", parents=[common], help="계획(plan) CRUD")
    plan_sub = p_plan.add_subparsers(dest="plan_cmd", required=True)

    p_plan_add = plan_sub.add_parser("add", parents=[common], help="계획 추가")
    p_plan_add.add_argument("title", type=str, help="계획 제목")
    p_plan_add.add_argument("timerange", type=str, metavar="HH:MM-HH:MM", help="시간 범위 (예: 09:00-12:00)")
    p_plan_add.add_argument(
        "--category", type=str, default=None, help="카테고리 id (미지정이면 off/away 를 뺀 전체 활동으로 달성률 계산)"
    )
    p_plan_add.add_argument(
        "--days", type=str, default=None, help="반복 요일 (예: 평일, 월수금, mon,wed,fri, 1-5). 기본: 매일"
    )
    p_plan_add.add_argument(
        "--day", type=str, default=None, help="일회성 계획의 날짜 'YYYY-MM-DD' (지정하면 일회성, --days 와 동시 사용 불가)"
    )
    p_plan_add.add_argument("--color", type=str, default=None, help="칸/범례에 쓸 색 (기본: 미지정)")
    p_plan_add.add_argument("--sort-order", type=int, default=0, help="목록/격자 정렬 순서 (기본 0)")
    p_plan_add.set_defaults(func=cmd_plan_add)

    p_plan_list = plan_sub.add_parser("list", parents=[common], help="계획 목록 (달성률 포함)")
    p_plan_list.add_argument("--day", type=str, default=None, help="'YYYY-MM-DD' (기본: 오늘)")
    p_plan_list.set_defaults(func=cmd_plan_list)

    p_plan_rm = plan_sub.add_parser("rm", parents=[common], help="계획 삭제")
    p_plan_rm.add_argument("plan_id", type=int, help="계획 id")
    p_plan_rm.set_defaults(func=cmd_plan_rm)

    p_plan_check = plan_sub.add_parser("check", parents=[common], help="체크박스 토글")
    p_plan_check.add_argument("plan_id", type=int, help="계획 id")
    p_plan_check.add_argument("--day", type=str, default=None, help="'YYYY-MM-DD' (기본: 오늘)")
    p_plan_check.add_argument("--off", action="store_true", help="체크 해제 (기본: 체크함)")
    p_plan_check.set_defaults(func=cmd_plan_check)

    p_plan_skip = plan_sub.add_parser("skip", parents=[common], help="반복 계획을 그 날짜만 건너뛰기")
    p_plan_skip.add_argument("plan_id", type=int, help="계획 id")
    p_plan_skip.add_argument("--day", type=str, required=True, help="'YYYY-MM-DD'")
    p_plan_skip.set_defaults(func=cmd_plan_skip)

    p_slot = sub.add_parser("slot", parents=[common], help="타임테이블 칸 수동 보정")
    slot_sub = p_slot.add_subparsers(dest="slot_cmd", required=True)

    p_slot_set = slot_sub.add_parser("set", parents=[common], help="슬롯 범위를 한 카테고리로 고정")
    p_slot_set.add_argument("day", type=str, help="'YYYY-MM-DD'")
    p_slot_set.add_argument(
        "range", type=str, metavar="START-END", help="슬롯 범위 (0..143, 슬롯당 기본 10분. 예: 54-72 = 09:00-12:00)"
    )
    p_slot_set.add_argument("category", type=str, help="카테고리 id (예: coding, ops)")
    p_slot_set.set_defaults(func=cmd_slot_set)

    p_slot_clear = slot_sub.add_parser("clear", parents=[common], help="슬롯 범위 보정 해제")
    p_slot_clear.add_argument("day", type=str, help="'YYYY-MM-DD'")
    p_slot_clear.add_argument("range", type=str, metavar="START-END", help="슬롯 범위 (예: 54-72)")
    p_slot_clear.set_defaults(func=cmd_slot_clear)

    p_planner = sub.add_parser("planner", parents=[common], help="하루 플래너 PNG 생성")
    p_planner.add_argument("--day", type=str, required=True, help="'YYYY-MM-DD'")
    p_planner.add_argument("--out", type=str, default=None, help="출력 경로 (기본: cfg.report.png_dir)")
    p_planner.add_argument("--theme", type=str, default="light", choices=["light", "dark"], help="테마 (기본 light)")
    p_planner.set_defaults(func=cmd_planner)

    p_web = sub.add_parser("web", parents=[common], help="플래너 웹 서버 실행 (상시)")
    p_web.add_argument("--host", type=str, default=None, help="바인드 호스트 (기본: cfg.web.host)")
    p_web.add_argument("--port", type=int, default=None, help="포트 (기본: cfg.web.port)")
    p_web.set_defaults(func=cmd_web)

    return parser


# ── 공용 헬퍼 ──────────────────────────────────────────────────────────


def _today(cfg: Config) -> str:
    # 경계값을 반드시 넘긴다. 빼먹으면 기본값으로 동작해 롤업과 조용히 어긋난다
    # (사용자가 day_boundary_hour 를 바꾼 순간 드러난다).
    return timeutil.day_str(
        timeutil.now_ts(), cfg.tz, boundary_hour=cfg.rollup.day_boundary_hour
    )


def _load_classifier(cfg: Config):
    from lifetrainer.rollup.classify import Classifier

    return Classifier.from_yaml(cfg.rollup.rules_path)


# ── 서브커맨드 구현 ───────────────────────────────────────────────────────


def cmd_doctor(args: argparse.Namespace, cfg: Config) -> int:
    """환경 점검. 사람이 설치 후 가장 먼저 돌리는 명령이라 실패 원인을 명확히 알려준다."""
    print(f"Life Trainer doctor — root={cfg.root} tz={cfg.timezone}")
    print()

    checks: list[tuple[str, str, str]] = []  # (status, name, detail)

    def ok(name: str, detail: str = "") -> None:
        checks.append(("OK", name, detail))

    def warn(name: str, detail: str) -> None:
        checks.append(("WARN", name, detail))

    def fail(name: str, detail: str) -> None:
        checks.append(("FAIL", name, detail))

    # 1. DB 존재 / 스키마 버전
    conn: sqlite3.Connection | None = None
    if cfg.db_path.exists():
        try:
            conn = db.connect(cfg.db_path)
            version = db.schema_version(conn)
            if version == db.SCHEMA_VERSION:
                ok("DB", f"{cfg.db_path} (schema v{version})")
            elif version == 0:
                warn("DB", f"{cfg.db_path} 존재하지만 스키마 미적용 — `lt init-db` 실행 필요")
            else:
                warn(
                    "DB",
                    f"{cfg.db_path} 스키마 버전 {version} (코드 기준 v{db.SCHEMA_VERSION}) — 마이그레이션 확인 필요",
                )
        except Exception as exc:  # noqa: BLE001
            fail("DB", f"{cfg.db_path} 열기 실패: {exc} — 파일 손상 시 `lt init-db` 로 재생성 검토")
    else:
        fail("DB", f"{cfg.db_path} 없음 — `lt init-db` 실행 필요")

    # 2. ActivityWatch 연결 + 버킷 목록
    try:
        from lifetrainer.collect.aw_client import AWClient

        client = AWClient(cfg.aw.base_url, api_key=cfg.aw.api_key, timeout=cfg.aw.timeout_sec)
        if client.ping():
            try:
                buckets = client.buckets()
                names = ", ".join(list(buckets)[:5]) + (" …" if len(buckets) > 5 else "")
                ok("ActivityWatch", f"{cfg.aw.base_url} 연결됨, 버킷 {len(buckets)}개: {names}")
            except Exception as exc:  # noqa: BLE001
                warn("ActivityWatch", f"ping 은 됐지만 버킷 조회 실패: {exc}")
        else:
            warn(
                "ActivityWatch",
                f"{cfg.aw.base_url} 연결 실패 — Windows PC 에 설치 필요"
                " (scripts/setup-activitywatch-windows.ps1). 그 전까지는 `lt synth` 로 합성 데이터 사용",
            )
    except Exception as exc:  # noqa: BLE001
        warn("ActivityWatch", f"클라이언트 생성 실패: {exc}")

    # 3. 마지막 동기화 시각 / 이벤트 수 / 롤업 범위
    if conn is not None:
        try:
            cursor_row = conn.execute(
                "SELECT MAX(value) AS v FROM sync_state WHERE key LIKE 'aw_cursor:%'"
            ).fetchone()
            event_count = conn.execute("SELECT COUNT(*) FROM aw_event").fetchone()[0]
            if event_count:
                last_sync = f", 마지막 커서={timeutil.iso_utc(float(cursor_row['v']))}" if cursor_row and cursor_row["v"] else ""
                ok("이벤트", f"총 {event_count}개{last_sync}")
            else:
                warn("이벤트", "0개 — `lt sync` 또는 `lt synth --days 14` 로 데이터 생성")

            rollup_row = conn.execute("SELECT MIN(day) AS a, MAX(day) AS b FROM slot").fetchone()
            if rollup_row and rollup_row["a"]:
                ok("롤업", f"{rollup_row['a']} ~ {rollup_row['b']}")
            else:
                warn("롤업", "롤업된 날짜가 없음 — `lt rollup --range <시작> <끝>` 실행 필요")
        except Exception as exc:  # noqa: BLE001
            warn("DB 조회", f"이벤트/롤업 조회 실패: {exc}")

    # 4. LLM 헬스 + 실제 모델 id
    try:
        from lifetrainer.llm.client import LLMClient

        llm = LLMClient(cfg)
        if llm.health():
            model_id = llm.model_id()
            if model_id == cfg.llm.model:
                ok("LLM", f"{cfg.llm.base_url} 정상, 모델={model_id}")
            elif model_id:
                warn(
                    "LLM",
                    f"{cfg.llm.base_url} 응답하지만 모델 불일치 (실제={model_id!r}, 설정={cfg.llm.model!r})"
                    " — 포트만 보고 넘어가면 엉뚱한 모델을 오래 쓸 수 있다. config 를 실제 모델에 맞추세요.",
                )
            else:
                warn("LLM", f"{cfg.llm.base_url} 헬스체크는 통과했지만 모델 id 조회 실패")
        else:
            warn("LLM", f"{cfg.llm.base_url} 연결 실패 — llama-server 가 떠 있는지 확인 (Phase 3 기능만 영향)")
    except Exception as exc:  # noqa: BLE001
        warn("LLM", f"클라이언트 생성 실패: {exc}")

    # 5. Slack 토큰 / 채널
    if cfg.slack.bot_token:
        if cfg.slack.default_channel:
            ok("Slack", f"토큰 있음 (mode={cfg.slack.mode}), 채널={cfg.slack.default_channel}")
        else:
            warn(
                "Slack",
                "토큰은 있지만 default_channel 미설정 — config/lifetrainer.toml [slack].default_channel 설정 필요",
            )
    else:
        warn(
            "Slack",
            "봇 토큰 없음(openclaw 폴백 포함) — 리포트 발송 불가."
            " config/lifetrainer.toml [slack].bot_token 또는 openclaw_config 확인",
        )

    # 6. 한글 폰트
    try:
        from matplotlib import font_manager

        available = {f.name for f in font_manager.fontManager.ttflist}
        candidates = [cfg.report.font_family, "NanumGothic", "Noto Sans CJK KR", "DejaVu Sans"]
        found = next((c for c in candidates if c in available), None)
        if found and found != "DejaVu Sans":
            ok("한글 폰트", f"{found} 사용 가능")
        elif found == "DejaVu Sans":
            warn(
                "한글 폰트",
                "한글 폰트 없음 — DejaVu Sans 로 대체, PNG 의 한글이 두부(□)로 깨질 수 있음."
                " `sudo apt install fonts-nanum` 권장",
            )
        else:
            warn("한글 폰트", "폰트 후보를 하나도 찾지 못함")
    except Exception as exc:  # noqa: BLE001
        warn("한글 폰트", f"확인 실패: {exc}")

    # 7. 디스크 여유
    try:
        target = cfg.data_dir if cfg.data_dir.exists() else cfg.root
        usage = shutil.disk_usage(target)
        free_gb = usage.free / (1024**3)
        if free_gb > 2.0:
            ok("디스크", f"{free_gb:.1f} GB 여유 ({target})")
        elif free_gb > 0.5:
            warn("디스크", f"{free_gb:.1f} GB 여유 ({target}) — 여유가 줄고 있음, 오래된 백업/PNG 정리 검토")
        else:
            fail("디스크", f"{free_gb:.1f} GB 여유 ({target}) — 공간을 확보하지 않으면 쓰기가 실패할 수 있음")
    except Exception as exc:  # noqa: BLE001
        warn("디스크", f"확인 실패: {exc}")

    # 8. 큐 상태
    if conn is not None:
        try:
            from lifetrainer.llm.queue import stats as queue_stats

            qs = queue_stats(conn)
            ok("큐", ", ".join(f"{k}={v}" for k, v in sorted(qs.items())) if qs else "비어 있음")
        except Exception as exc:  # noqa: BLE001
            warn("큐", f"조회 실패: {exc}")

    # 8-B. 웹 검색 키 (없으면 대화가 URL 을 추측한다 — known-issues §1)
    try:
        from lifetrainer.llm.websearch import providers_available

        have = providers_available(cfg)
        if have["naver"] and have["serper"]:
            ok("웹 검색", "네이버(한국어) + Serper(영어)")
        elif have["naver"]:
            warn("웹 검색", "네이버만 설정됨 — 영어 질의도 네이버로 간다 ([search] serper_api_key)")
        elif have["serper"]:
            warn("웹 검색", "Serper 만 설정됨 — 한국어 질의도 구글로 간다 ([search] naver_client_id/secret)")
        else:
            warn(
                "웹 검색",
                "키 없음 — 대화가 주소를 추측한다. "
                "config/lifetrainer.toml 의 [search] 에 naver_client_id/naver_client_secret "
                "(하루 25,000건 무료) 또는 serper_api_key 를 넣는다",
            )
    except Exception as exc:  # noqa: BLE001
        warn("웹 검색", f"확인 실패: {exc}")

    if conn is not None:
        conn.close()

    # ── 출력 ──
    name_width = max((len(name) for _, name, _ in checks), default=0)
    for status, name, detail in checks:
        print(f"[{status:>4}] {name:<{name_width}}  {detail}")

    fails = [c for c in checks if c[0] == "FAIL"]
    warns = [c for c in checks if c[0] == "WARN"]
    oks = len(checks) - len(fails) - len(warns)

    print()
    print(f"총 {len(checks)}개 항목 — OK {oks} / WARN {len(warns)} / FAIL {len(fails)}")

    if fails:
        print()
        print("조치가 필요합니다:")
        for _, name, detail in fails:
            print(f"  - {name}: {detail}")
        return 1

    return 0


def cmd_init_db(args: argparse.Namespace, cfg: Config) -> int:
    conn = db.open_db(cfg)
    conn.close()
    print(f"스키마 적용 완료: {cfg.db_path} (schema v{db.SCHEMA_VERSION})")
    return 0


def cmd_sync(args: argparse.Namespace, cfg: Config) -> int:
    from lifetrainer.collect.aw_client import AWClient
    from lifetrainer.collect.aw_sync import sync

    conn = db.open_db(cfg)
    try:
        client = AWClient(cfg.aw.base_url, api_key=cfg.aw.api_key, timeout=cfg.aw.timeout_sec)
        result = sync(conn, client, cfg)
        print(f"동기화 완료: 버킷 {result.buckets_seen}개, 이벤트 {result.events_upserted}개 upsert")
        for err in result.errors:
            print(f"  경고: {err}")
        return 0
    finally:
        conn.close()


def _days_touched(cfg: Config, result) -> set[str]:
    """이번에 들어온 이벤트가 걸친 논리적 하루(06:00 경계) 목록.

    폰 데이터는 **늦게 도착한다** (수집 1시간 주기 + 자정 파싱). 오늘만 롤업하면
    어제 후반부가 영영 반영되지 않으므로, 실제로 건드린 날짜를 전부 돌려준다.
    """
    if result.ts_min is None or result.ts_max is None:
        return set()
    lo = timeutil.day_str(result.ts_min, cfg.tz, boundary_hour=cfg.rollup.day_boundary_hour)
    hi = timeutil.day_str(result.ts_max, cfg.tz, boundary_hour=cfg.rollup.day_boundary_hour)
    return set(timeutil.day_range(lo, hi))


def cmd_import(args: argparse.Namespace, cfg: Config) -> int:
    """폰에서 뽑은 export 파일을 넣는다 (`POST /ingest/aw` 와 같은 코드 경로).

    adb 로 뽑은 표본과 상시 경로가 갈라지면, 파일로는 되는데 네트워크로는 안 되는
    상황을 나중에 디버깅하게 된다. 검증·삽입은 `collect/ingest.apply_payload` 하나뿐이다.
    """
    import json as _json

    from lifetrainer.collect import ingest

    src = Path(args.src)
    if src.is_dir():
        files = sorted(src.glob("*.json"))
        if not files:
            print(f"JSON 파일이 없습니다: {src}", file=sys.stderr)
            return 1
    elif src.exists():
        files = [src]
    else:
        print(f"경로를 찾을 수 없습니다: {src}", file=sys.stderr)
        return 1

    conn = db.open_db(cfg)
    try:
        total_events = 0
        failed = 0
        touched_days: set[str] = set()
        for path in files:
            try:
                payload = _json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                print(f"  {path.name}: 읽기 실패 — {exc}", file=sys.stderr)
                failed += 1
                continue

            device = args.device or ingest.infer_device_name(payload)
            if not device:
                print(
                    f"  {path.name}: 기기 이름을 추론할 수 없습니다 — --device 로 지정하세요",
                    file=sys.stderr,
                )
                failed += 1
                continue

            try:
                with db.transaction(conn):
                    result = ingest.apply_payload(
                        conn,
                        payload,
                        device=device,
                        allow_new_device=True,
                        device_kind=ingest.kind_for_payload(payload),
                    )
            except ingest.IngestError as exc:
                print(f"  {path.name}: 거부됨 — {exc}", file=sys.stderr)
                failed += 1
                continue

            total_events += result.events
            print(
                f"  {path.name}: 기기 {result.device} — 버킷 {result.buckets}개 / "
                f"이벤트 {result.events}건"
            )
            for note in result.skipped:
                print(f"      건너뜀: {note}")
            touched_days |= _days_touched(cfg, result)

        print(f"완료: 파일 {len(files) - failed}/{len(files)}개, 이벤트 {total_events}건")

        if args.rollup and touched_days:
            from lifetrainer.rollup.rollup import rollup_day

            classifier = _load_classifier(cfg)
            for day in sorted(touched_days):
                # rollup_day 는 자기 안에서 트랜잭션을 연다 — 밖에서 또 감싸면
                # sqlite 가 "cannot start a transaction within a transaction" 을 낸다.
                rollup_day(conn, cfg, classifier, day)
                print(f"  재롤업: {day}")

        return 1 if failed else 0
    finally:
        conn.close()


def cmd_synth(args: argparse.Namespace, cfg: Config) -> int:
    from lifetrainer.collect.synthetic import generate

    conn = db.open_db(cfg)
    try:
        n = generate(conn, cfg, days=args.days, end_day=args.end_day, seed=args.seed)
        print(f"합성 이벤트 {n}개 생성 (최근 {args.days}일)")
        return 0
    finally:
        conn.close()


def cmd_rollup(args: argparse.Namespace, cfg: Config) -> int:
    from lifetrainer.rollup.rollup import rollup_day, rollup_range

    classifier = _load_classifier(cfg)
    conn = db.open_db(cfg)
    try:
        today = _today(cfg)
        if args.range:
            start, end = args.range
            results = rollup_range(conn, cfg, classifier, start, end)
        elif args.yesterday:
            day = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
            results = [rollup_day(conn, cfg, classifier, day)]
        elif args.day:
            results = [rollup_day(conn, cfg, classifier, args.day)]
        else:
            # --today 또는 인자 없음: 오늘.
            results = [rollup_day(conn, cfg, classifier, today)]

        for r in results:
            print(
                f"{r.day}: 활동 {r.active_sec / 60:.0f}분 · 커버리지 {r.coverage * 100:.0f}%"
                f" · 미분류 지문 {r.unclassified_seen}개"
            )
        return 0
    finally:
        conn.close()


def cmd_stats(args: argparse.Namespace, cfg: Config) -> int:
    from lifetrainer.report.stats import compute_daily, render_daily_text

    classifier = _load_classifier(cfg)
    day = args.day or _today(cfg)
    conn = db.open_db(cfg)
    try:
        day_stats = compute_daily(conn, cfg, day)
        print(render_daily_text(day_stats, classifier))
        return 0
    finally:
        conn.close()


def cmd_timeline(args: argparse.Namespace, cfg: Config) -> int:
    from lifetrainer.report.timeline import render_day

    classifier = _load_classifier(cfg)
    conn = db.open_db(cfg)
    try:
        out_path = Path(args.out) if args.out else None
        out = render_day(conn, cfg, classifier, args.day, out_path=out_path)
        print(f"PNG 생성: {out} ({out.stat().st_size:,} bytes)")
        return 0
    finally:
        conn.close()


def _maybe_post(cfg: Config, conn, built, *, post: bool) -> None:
    """`--post` 가 있을 때만 Slack 으로 보낸다. 없으면 절대 발송하지 않는다."""
    if not post:
        return
    from lifetrainer.slackio.notify import SlackNotifier

    notifier = SlackNotifier(cfg)
    if not notifier.enabled:
        print("Slack 비활성화(토큰 없음) — 발송 생략", file=sys.stderr)
        return
    ts = notifier.post_report(conn, built)
    if ts:
        print(f"Slack 발송 완료 (ts={ts})")
    else:
        print("Slack 발송 실패 또는 채널 미지정", file=sys.stderr)


def cmd_report_daily(args: argparse.Namespace, cfg: Config) -> int:
    from lifetrainer.report.daily import build_daily

    classifier = _load_classifier(cfg)
    day = args.day or _today(cfg)
    conn = db.open_db(cfg)
    try:
        built = build_daily(conn, cfg, classifier, day)
        print(built.text)
        if built.png_path:
            print(f"PNG: {built.png_path}")
        _maybe_post(cfg, conn, built, post=args.post)
        return 0
    finally:
        conn.close()


def cmd_report_weekly(args: argparse.Namespace, cfg: Config) -> int:
    from lifetrainer.report.daily import build_weekly

    classifier = _load_classifier(cfg)
    end_day = args.end_day or _today(cfg)
    conn = db.open_db(cfg)
    try:
        built = build_weekly(conn, cfg, classifier, end_day)
        print(built.text)
        if built.png_path:
            print(f"PNG: {built.png_path}")
        _maybe_post(cfg, conn, built, post=args.post)
        return 0
    finally:
        conn.close()


def cmd_log(args: argparse.Namespace, cfg: Config) -> int:
    try:
        duration_sec = timeutil.parse_duration(args.period)
    except ValueError as exc:
        raise CliError(f"기간을 해석할 수 없습니다: {args.period!r} ({exc})") from exc

    if args.at:
        try:
            hh_str, mm_str = args.at.split(":")
            hh, mm = int(hh_str), int(mm_str)
        except ValueError as exc:
            raise CliError(f"시각을 해석할 수 없습니다: {args.at!r}. 형식: HH:MM") from exc
        day = _today(cfg)
        boundary = cfg.rollup.day_boundary_hour
        day_start, _day_end = timeutil.day_bounds(day, cfg.tz, boundary_hour=boundary)
        # day_start 는 논리적 하루의 시작(기본 06:00)이지 자정이 아니다.
        # 벽시계 HH:MM 을 경계 기준 오프셋으로 환산해야 한다 —
        # 그냥 day_start + hh 시간을 더하면 09:00 이 15:00 으로 기록된다.
        # 경계보다 이른 시각(예: 01:00)은 자동으로 다음 날 새벽에 배치된다.
        offset_min = ((hh * 60 + mm) - boundary * 60) % 1440
        end_ts = day_start + offset_min * 60
    else:
        end_ts = timeutil.now_ts()
    start_ts = end_ts - duration_sec
    note = " ".join(args.note) if args.note else None

    conn = db.open_db(cfg)
    try:
        actor = os.environ.get("USER") or os.environ.get("LOGNAME") or "cli"
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO manual_entry"
                "(start_ts, end_ts, category, subcategory, note, source, actor, revoked, created_at) "
                "VALUES (?, ?, ?, NULL, ?, 'cli', ?, 0, ?)",
                (start_ts, end_ts, args.category, note, actor, timeutil.now_ts()),
            )
        print(f"기록됨: {args.category} · {args.period} ({note or '메모 없음'})")

        # 영향받는 날짜를 자동으로 재롤업한다 (계약서: "lt log ... 영향받는 날짜를 자동으로 재롤업").
        from lifetrainer.rollup.rollup import rollup_day

        classifier = _load_classifier(cfg)
        bh = cfg.rollup.day_boundary_hour
        start_day = timeutil.day_str(start_ts, cfg.tz, boundary_hour=bh)
        end_day = timeutil.day_str(end_ts, cfg.tz, boundary_hour=bh)
        for day in timeutil.day_range(start_day, end_day):
            rollup_day(conn, cfg, classifier, day)
        print(f"재롤업 완료: {start_day} ~ {end_day}")
        return 0
    finally:
        conn.close()


def cmd_collect(args: argparse.Namespace, cfg: Config) -> int:
    from lifetrainer.collect.feeds import run_once

    conn = db.open_db(cfg)
    try:
        results = run_once(conn, cfg)
        new_total = sum(r.new_docs for r in results)
        dup_total = sum(r.dup_docs for r in results)
        errors = [r for r in results if r.error]
        print(f"수집 완료: 소스 {len(results)}개 · 신규 {new_total}건 · 중복 {dup_total}건 · 오류 {len(errors)}건")
        for r in errors:
            print(f"  {r.name}: {r.error}")
        return 0
    finally:
        conn.close()


def cmd_score(args: argparse.Namespace, cfg: Config) -> int:
    from lifetrainer.collect.score import score_pending

    conn = db.open_db(cfg)
    try:
        n = score_pending(conn, cfg)
        print(f"스코어링 완료: {n}건")
        return 0
    finally:
        conn.close()


def cmd_digest(args: argparse.Namespace, cfg: Config) -> int:
    """아침 다이제스트 — 키워드 랭킹 상위 문서만 나열한다 (LLM 없음)."""
    from lifetrainer.collect.score import top_docs
    from lifetrainer.slackio import blocks as blocks_mod

    conn = db.open_db(cfg)
    try:
        today = _today(cfg)
        docs = top_docs(conn, limit=5)

        if not docs:
            text = f"{today} 아침 다이제스트: 새로 수집된 관심 문서가 없습니다."
            digest_blocks = [
                blocks_mod.header(f"아침 다이제스트 — {today}"),
                blocks_mod.section("오늘은 새로 수집된 관심 문서가 없습니다."),
            ]
        else:
            lines = [f"• <{d['url']}|{d['title']}>" + (f" — {d['summary']}" if d["summary"] else "") for d in docs]
            text = f"{today} 아침 다이제스트\n" + "\n".join(lines)
            digest_blocks = [
                blocks_mod.header(f"아침 다이제스트 — {today}"),
                blocks_mod.section("\n".join(lines)),
            ]

        print(text)

        now = timeutil.now_ts()
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO report(kind, day, text, blocks_json, created_at) VALUES ('morning', ?, ?, ?, ?) "
                "ON CONFLICT(kind, day) DO UPDATE SET text=excluded.text, blocks_json=excluded.blocks_json, "
                "created_at=excluded.created_at",
                (today, text, json.dumps(digest_blocks, ensure_ascii=False), now),
            )
        row = conn.execute("SELECT id FROM report WHERE kind = 'morning' AND day = ?", (today,)).fetchone()
        report_id = int(row["id"])

        if args.post:
            from lifetrainer.slackio.notify import SlackNotifier

            notifier = SlackNotifier(cfg)
            if not notifier.enabled:
                print("Slack 비활성화(토큰 없음) — 발송 생략", file=sys.stderr)
            else:
                ts = notifier.post(text, blocks=digest_blocks)
                if ts:
                    with db.transaction(conn):
                        conn.execute(
                            "UPDATE report SET posted_at = ?, slack_ts = ? WHERE id = ?",
                            (timeutil.now_ts(), ts, report_id),
                        )
                    print(f"Slack 발송 완료 (ts={ts})")
                else:
                    print("Slack 발송 실패 또는 채널 미지정", file=sys.stderr)
        return 0
    finally:
        conn.close()


def cmd_nightly(args: argparse.Namespace, cfg: Config) -> int:
    """야간 배치 잡을 큐에 넣는다. 실제 처리는 상시 워커가 한다 (여기서 LLM 을 부르지 않는다)."""
    from lifetrainer.llm.nightly import enqueue_nightly, pending_docs, untagged_count  # noqa: F401

    conn = db.open_db(cfg)
    try:
        if args.stop:
            from lifetrainer.llm.nightly import cancel_pending_batch

            dropped = cancel_pending_batch(conn)
            print(f"대기 중이던 배치 잡 {dropped}건을 큐에서 비웠습니다 (예약 알림은 그대로).")
            return 0

        if args.dry_run:
            s_limit = cfg.nightly.summary_limit if args.summaries is None else args.summaries
            docs = pending_docs(conn, s_limit)
            untagged = untagged_count(conn)
            print(f"요약 대상 {len(docs)}건 (상한 {s_limit}) · 미태깅 지문 {untagged}개")
            print("(--dry-run 이라 큐에 넣지 않았습니다)")
            return 0

        result = enqueue_nightly(
            conn, cfg, summary_limit=args.summaries, tag_limit=args.tags
        )
        print(
            f"요약 잡 {result['summaries']}건 적재"
            f" (후보 {result['candidates']}건 중 중복 {result['summaries_skipped']}건 제외)"
        )
        print(f"태깅 잡 {result['tags']}건 적재 (미태깅 지문 {result['untagged']}개)")
        if result["summaries"] or result["tags"]:
            print("처리는 상시 워커(lifetrainer-worker.service)가 합니다.")
        return 0
    finally:
        conn.close()


def cmd_worker(args: argparse.Namespace, cfg: Config) -> int:
    from lifetrainer.llm.worker import run_forever, run_once

    conn = db.open_db(cfg)
    try:
        if args.once:
            n = run_once(conn, cfg)
            print(f"처리한 잡: {n}개")
        else:
            run_forever(conn, cfg)
        return 0
    finally:
        conn.close()


def cmd_queue_stats(args: argparse.Namespace, cfg: Config) -> int:
    from lifetrainer.llm.queue import stats as queue_stats

    conn = db.open_db(cfg)
    try:
        s = queue_stats(conn)
        if not s:
            print("큐가 비어 있습니다.")
        else:
            for state, count in sorted(s.items()):
                print(f"{state}: {count}")
        return 0
    finally:
        conn.close()


def cmd_backup(args: argparse.Namespace, cfg: Config) -> int:
    conn = db.open_db(cfg)
    try:
        if args.out:
            out = Path(args.out)
        else:
            out = cfg.data_dir / "backup" / f"lifetrainer-{_today(cfg)}.db"
        dest = db.backup(conn, out)
        print(f"백업 완료: {dest}")
        return 0
    finally:
        conn.close()


def _snap_to_slot(minutes: int, slot_minutes: int) -> int:
    """분 단위 시각을 슬롯 경계로 반올림한다 (기본 슬롯 10분).

    계획 시각이 슬롯 경계에 어긋나면 `plan.achieve.plans_for_day` 가
    `start_min // slot_minutes` 로 슬롯 범위를 구할 때 실제 계획 구간과
    슬롯 구간이 어긋나 달성률이 부정확해진다. 그래서 `lt plan add` 는
    항상 이 함수로 스냅한 값을 저장한다.
    """
    return int(round(minutes / slot_minutes)) * slot_minutes


_SLOT_RANGE_RE = re.compile(r"^(\d+)-(\d+)$")


def _parse_slot_range(s: str, *, max_slot: int) -> tuple[int, int]:
    """'54-72' 형태의 슬롯 범위를 (start_slot, end_slot) 으로. 사용자 오류는 CliError."""
    m = _SLOT_RANGE_RE.match(s.strip())
    if not m:
        raise CliError(f"슬롯 범위를 해석할 수 없습니다: {s!r} (형식: '<start_slot>-<end_slot>', 예: '54-72')")
    start_slot, end_slot = int(m.group(1)), int(m.group(2))
    if not (0 <= start_slot < end_slot <= max_slot):
        raise CliError(
            f"잘못된 슬롯 범위입니다: {s!r} (0 <= start < end <= {max_slot} 이어야 합니다)"
        )
    return start_slot, end_slot


def cmd_plan_add(args: argparse.Namespace, cfg: Config) -> int:
    """계획을 추가한다. `--day` 가 있으면 일회성, 없으면 반복(기본: 매일)."""
    from lifetrainer.plan.models import create_plan, parse_time_range

    if args.day and args.days:
        raise CliError("--day 와 --days 는 함께 쓸 수 없습니다 (일회성 vs 반복 계획 중 하나만 고르세요)")

    try:
        start_min, end_min = parse_time_range(args.timerange)
    except ValueError as exc:
        raise CliError(str(exc)) from exc

    slot_minutes = cfg.rollup.slot_minutes
    snapped_start = _snap_to_slot(start_min, slot_minutes)
    snapped_end = _snap_to_slot(end_min, slot_minutes)
    if snapped_start >= snapped_end:
        raise CliError(
            f"시간 범위가 {slot_minutes}분 경계로 스냅한 뒤 비어버렸습니다: {args.timerange!r}"
        )
    if (snapped_start, snapped_end) != (start_min, end_min):
        print(
            f"시각을 {slot_minutes}분 경계로 스냅했습니다: {args.timerange} -> "
            f"{snapped_start // 60:02d}:{snapped_start % 60:02d}-{snapped_end // 60:02d}:{snapped_end % 60:02d}"
        )

    kind = "oneoff" if args.day else "recurring"
    weekdays = args.days if args.days else "1234567"

    conn = db.open_db(cfg)
    try:
        try:
            plan_id = create_plan(
                conn,
                title=args.title,
                start_min=snapped_start,
                end_min=snapped_end,
                category=args.category,
                kind=kind,
                weekdays=weekdays,
                day=args.day,
                color=args.color,
                sort_order=args.sort_order,
            )
        except ValueError as exc:
            raise CliError(str(exc)) from exc
        print(f"계획 생성됨: id={plan_id} {args.title!r} ({kind})")
        return 0
    finally:
        conn.close()


def cmd_plan_list(args: argparse.Namespace, cfg: Config) -> int:
    """계획 목록을 그날의 달성률과 함께 보여준다 (`plan.achieve.plans_for_day`)."""
    from lifetrainer.plan.achieve import plans_for_day

    day = args.day or _today(cfg)
    conn = db.open_db(cfg)
    try:
        instances = plans_for_day(conn, cfg, day)
        if not instances:
            print(f"{day}: 등록된 계획이 없습니다.")
            return 0
        print(f"{day} 계획 목록:")
        for pi in instances:
            mark = "☑" if pi.checked else "☐"
            sh, sm = divmod(pi.plan.start_min, 60)
            eh, em = divmod(pi.plan.end_min, 60)
            cat = pi.plan.category or "(전체)"
            print(
                f"  {mark} [{pi.plan.id}] {pi.plan.title} {sh:02d}:{sm:02d}-{eh:02d}:{em:02d}"
                f" · {cat} · 달성률 {pi.achievement * 100:.0f}%"
            )
        return 0
    finally:
        conn.close()


def cmd_plan_rm(args: argparse.Namespace, cfg: Config) -> int:
    from lifetrainer.plan.models import delete_plan, get_plan

    conn = db.open_db(cfg)
    try:
        existing = get_plan(conn, args.plan_id)
        if existing is None:
            raise CliError(f"계획을 찾을 수 없습니다: id={args.plan_id}")
        delete_plan(conn, args.plan_id)
        print(f"계획 삭제됨: id={args.plan_id} ({existing.title!r})")
        return 0
    finally:
        conn.close()


def cmd_plan_check(args: argparse.Namespace, cfg: Config) -> int:
    from lifetrainer.plan.models import get_plan, set_check

    conn = db.open_db(cfg)
    try:
        existing = get_plan(conn, args.plan_id)
        if existing is None:
            raise CliError(f"계획을 찾을 수 없습니다: id={args.plan_id}")
        day = args.day or _today(cfg)
        checked = not args.off
        set_check(conn, args.plan_id, day, checked)
        print(f"{day} {existing.title!r} {'체크됨' if checked else '체크 해제됨'}")
        return 0
    finally:
        conn.close()


def cmd_plan_skip(args: argparse.Namespace, cfg: Config) -> int:
    from lifetrainer.plan.models import get_plan, skip_plan

    conn = db.open_db(cfg)
    try:
        existing = get_plan(conn, args.plan_id)
        if existing is None:
            raise CliError(f"계획을 찾을 수 없습니다: id={args.plan_id}")
        skip_plan(conn, args.plan_id, args.day)
        print(f"{args.day} {existing.title!r} 건너뜀")
        return 0
    finally:
        conn.close()


def cmd_slot_set(args: argparse.Namespace, cfg: Config) -> int:
    """슬롯 범위를 수동 보정하고, 격자에 즉시 반영되도록 그 날짜를 재롤업한다."""
    from lifetrainer.plan.override import set_override_range
    from lifetrainer.rollup.rollup import rollup_day

    start_slot, end_slot = _parse_slot_range(args.range, max_slot=cfg.slots_per_day)

    classifier = _load_classifier(cfg)
    conn = db.open_db(cfg)
    try:
        actor = os.environ.get("USER") or os.environ.get("LOGNAME") or "cli"
        n = set_override_range(conn, args.day, start_slot, end_slot, args.category, actor=actor)
        rollup_day(conn, cfg, classifier, args.day)
        print(f"{args.day} 슬롯 {start_slot}-{end_slot} -> {args.category} ({n}칸) · 재롤업 완료")
        return 0
    finally:
        conn.close()


def cmd_slot_clear(args: argparse.Namespace, cfg: Config) -> int:
    """슬롯 범위의 수동 보정을 해제하고, 격자에 즉시 반영되도록 그 날짜를 재롤업한다."""
    from lifetrainer.plan.override import clear_override_range
    from lifetrainer.rollup.rollup import rollup_day

    start_slot, end_slot = _parse_slot_range(args.range, max_slot=cfg.slots_per_day)

    classifier = _load_classifier(cfg)
    conn = db.open_db(cfg)
    try:
        n = clear_override_range(conn, args.day, start_slot, end_slot)
        rollup_day(conn, cfg, classifier, args.day)
        print(f"{args.day} 슬롯 {start_slot}-{end_slot} 보정 해제 ({n}칸) · 재롤업 완료")
        return 0
    finally:
        conn.close()


def cmd_planner(args: argparse.Namespace, cfg: Config) -> int:
    """하루 플래너 PNG 를 만든다. P3 가 아직 `report.planner` 를 안 만들었으면 안내만 하고 죽지 않는다."""
    try:
        from lifetrainer.report.planner import render_planner_day
    except ImportError as exc:
        print(
            "플래너 PNG 렌더러(lifetrainer.report.planner.render_planner_day)가 아직 준비되지 않았습니다"
            " — 담당(P3) 작업이 끝나면 다시 시도하세요.",
            file=sys.stderr,
        )
        logger.debug("render_planner_day import 실패: %s", exc)
        return 1

    conn = db.open_db(cfg)
    try:
        out_path = Path(args.out) if args.out else None
        out = render_planner_day(conn, cfg, args.day, out_path=out_path, theme=args.theme)
        print(f"플래너 PNG 생성: {out}")
        return 0
    finally:
        conn.close()


def cmd_web(args: argparse.Namespace, cfg: Config) -> int:
    """플래너 웹 서버를 상시 구동한다. P2 가 아직 `web.app` 을 안 만들었으면 안내만 하고 죽지 않는다."""
    try:
        from lifetrainer.web.app import create_app
    except ImportError as exc:
        print(
            "웹 서버 모듈(lifetrainer.web.app.create_app)이 아직 준비되지 않았습니다"
            " — 담당(P2) 작업이 끝나면 다시 시도하세요.",
            file=sys.stderr,
        )
        logger.debug("create_app import 실패: %s", exc)
        return 1

    host = args.host or cfg.web.host
    port = args.port if args.port is not None else cfg.web.port
    run_cfg = replace(cfg, web=replace(cfg.web, host=host, port=port))
    app = create_app(run_cfg)
    print(f"웹 서버 시작: http://{host}:{port}")
    app.run(host=host, port=port)
    return 0


def cmd_slack_serve(args: argparse.Namespace, cfg: Config) -> int:
    if cfg.slack.mode != "bolt":
        raise CliError(
            f"slack.mode 가 'bolt' 가 아닙니다 (현재: {cfg.slack.mode!r})."
            " config/lifetrainer.toml [slack].mode 를 'bolt' 로 바꾸고 bot_token/app_token 을 채우세요."
        )
    from lifetrainer.slackio.app import run

    run(cfg)
    return 0


def cmd_slack_test(args: argparse.Namespace, cfg: Config) -> int:
    from lifetrainer.slackio.notify import SlackNotifier

    notifier = SlackNotifier(cfg)
    if not notifier.enabled:
        print(
            "Slack 비활성화(토큰 없음) — config/lifetrainer.toml [slack].bot_token 또는"
            " OpenClaw 설정(openclaw_config)을 확인하세요.",
            file=sys.stderr,
        )
        return 1
    ts = notifier.post("Life Trainer 테스트 메시지입니다. :white_check_mark:", channel=args.channel)
    if ts:
        print(f"발송 완료 (ts={ts})")
        return 0
    print("발송 실패 또는 채널이 지정되지 않았습니다.", file=sys.stderr)
    return 1


# ── 진입점 ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        cfg = load_config(getattr(args, "config", None))
    except Exception as exc:  # noqa: BLE001 - 설정 로드 실패는 런타임 실패로 취급
        print(f"설정 로드 실패: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "verbose", False):
        cfg = replace(cfg, log_level="DEBUG")
    setup_logging(cfg)

    func: Callable[[argparse.Namespace, Config], int] = args.func
    try:
        return func(args, cfg)
    except CliError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI 최상단이라 여기서는 잡아서 exit code 로 변환
        logger.exception("명령 실행 중 오류")
        print(f"실행 실패: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
