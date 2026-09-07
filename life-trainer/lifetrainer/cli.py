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
import subprocess
import time
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

    p_abs = sub.add_parser(
        "backfill-abstracts", parents=[common], help="제목뿐인 문서에 초록 채우기 (RAG 근거)"
    )
    p_abs.add_argument("--limit", type=int, default=50, help="이번에 처리할 문서 수")
    p_abs.add_argument("--source", type=str, default=None, help="소스 이름으로 한정")
    p_abs.add_argument("--dry-run", action="store_true", help="쓰지 않고 결과만 센다")
    p_abs.set_defaults(func=cmd_backfill_abstracts)

    p_embed = sub.add_parser("embed", parents=[common], help="문서 임베딩 생성 (RAG)")
    p_embed.add_argument("--limit", type=int, default=500, help="한 번에 처리할 문서 수")
    p_embed.add_argument("--all", action="store_true", help="남은 것을 전부 (여러 배치)")
    p_embed.set_defaults(func=cmd_embed)

    p_rollup = sub.add_parser("rollup", parents=[common], help="10분 슬롯 롤업")
    rollup_group = p_rollup.add_mutually_exclusive_group()
    rollup_group.add_argument("--day", type=str, help="'YYYY-MM-DD'")
    rollup_group.add_argument("--yesterday", action="store_true", help="어제")
    rollup_group.add_argument("--today", action="store_true", help="오늘 (인자 없을 때의 기본값과 동일)")
    rollup_group.add_argument("--range", nargs=2, metavar=("START", "END"), help="시작일 종료일 (양 끝 포함)")
    p_rollup.set_defaults(func=cmd_rollup)

    p_priv = sub.add_parser("private", parents=[common], help="프라이빗 모드 — 그 시간을 안 재게 한다")
    priv_sub = p_priv.add_subparsers(dest="private_cmd", required=True)
    pv_on = priv_sub.add_parser("on", parents=[common], help="지금부터 N분간 프라이빗 (이미 켜져 있으면 연장)")
    pv_on.add_argument("--minutes", type=float, default=None, help="기본: config 의 default_minutes")
    pv_on.set_defaults(func=cmd_private_on)
    pv_off = priv_sub.add_parser("off", parents=[common], help="지금 끈다 (구간의 끝을 당긴다)")
    pv_off.set_defaults(func=cmd_private_off)
    pv_st = priv_sub.add_parser("status", parents=[common], help="지금 상태와 오늘의 구간")
    pv_st.set_defaults(func=cmd_private_status)
    pv_pg = priv_sub.add_parser("purge", parents=[common], help="이미 저장된 구간을 지운다")
    pv_pg.add_argument("--minutes", type=float, default=None, help="지금부터 거슬러 N분")
    pv_pg.add_argument("--range", nargs=2, metavar=("START", "END"),
                       help="임의 구간 (ISO8601 또는 'YYYY-MM-DD HH:MM'). --yes 필요")
    pv_pg.add_argument("--day", type=str, default=None,
                       help="'YYYY-MM-DD' 하루 통째로 (논리적 하루: 06:00~다음날 06:00). --yes 필요")
    pv_pg.add_argument("--yes", action="store_true", help="확인 없이 실행")
    pv_pg.add_argument("--aw", action="store_true", help="엔드포인트 AW 로컬 DB 에서도 지운다")
    pv_pg.set_defaults(func=cmd_private_purge)
    pv_un = priv_sub.add_parser("undo", parents=[common], help="방금 지운 것을 되돌린다")
    pv_un.add_argument("--span-id", type=int, default=None,
                       help="되돌릴 삭제의 번호 (기본: 가장 최근 것)")
    pv_un.set_defaults(func=cmd_private_undo)
    pv_fg = priv_sub.add_parser("forget", parents=[common],
                                help="표시 삭제된 것을 **진짜로** 지운다 (되돌릴 수 없다)")
    pv_fg.add_argument("--yes", action="store_true", help="확인 없이 실행")
    pv_fg.set_defaults(func=cmd_private_forget)

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

    p_bodies = sub.add_parser(
        "bodies", parents=[common], help="초록이 짧은 문서의 전문 수집 (RAG 재료)"
    )
    p_bodies.add_argument("--limit", type=int, default=50, help="이번에 받을 문서 수 (기본 50)")
    p_bodies.set_defaults(func=cmd_bodies)

    p_score = sub.add_parser("score", parents=[common], help="미채점 문서 스코어링")
    p_score.add_argument(
        "--rescore",
        action="store_true",
        help="이미 채점된 문서까지 기준점수로 다시 계산 (관심사 가중치를 고친 뒤)",
    )
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
    p_plan_skip.add_argument("--off", action="store_true", help="건너뛰기 해제 (되돌리기)")
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

    # ── agent — OpenClaw 결합층 (lifetrainer/agent/) ──────────────────
    p_agent = sub.add_parser("agent", parents=[common], help="OpenClaw 에이전트 (MCP 서버 · 설치 · 예산)")
    agent_sub = p_agent.add_subparsers(dest="agent_cmd", required=True)

    p_agent_mcp = agent_sub.add_parser("mcp", parents=[common], help="MCP stdio 서버 실행 (게이트웨이가 띄운다)")
    p_agent_mcp.set_defaults(func=cmd_agent_mcp)

    p_agent_budget = agent_sub.add_parser("budget", parents=[common], help="프롬프트 토큰 예산 실측")
    p_agent_budget.set_defaults(func=cmd_agent_budget)

    p_agent_prompt = agent_sub.add_parser("prompt", parents=[common], help="워크스페이스 AGENTS.md 를 만들어 설치")
    p_agent_prompt.add_argument("--print", action="store_true", dest="print_only", help="쓰지 않고 표준출력으로만")
    p_agent_prompt.add_argument(
        "--keep-seeded",
        action="store_true",
        help="OpenClaw 가 깔아 둔 범용 인격 파일(SOUL/IDENTITY/BOOTSTRAP…)을 지우지 않는다",
    )
    p_agent_prompt.set_defaults(func=cmd_agent_prompt)

    p_agent_call = agent_sub.add_parser("call", parents=[common], help="툴 하나를 직접 호출 (배선 없이 확인)")
    p_agent_call.add_argument("tool", help="툴 이름 (예: slash)")
    p_agent_call.add_argument("args", nargs="*", help="key=value 인자")
    p_agent_call.set_defaults(func=cmd_agent_call)

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


def _top_job_error(conn: sqlite3.Connection) -> str:
    """실패 잡의 가장 흔한 오류를 한 줄로. 없거나 조회에 실패하면 빈 문자열.

    `lt doctor` 는 진단 도구라 **무엇이 잘못됐는지까지** 말해야 한다. 개수만 보여주면
    사람이 sqlite3 를 열어야 하고, 그 한 단계가 실제로 이틀을 잡아먹었다.
    """
    try:
        row = conn.execute(
            "SELECT error, COUNT(*) AS c FROM job WHERE state = 'failed' AND error IS NOT NULL"
            " GROUP BY substr(error, 1, 60) ORDER BY c DESC LIMIT 1"
        ).fetchone()
    except Exception:  # noqa: BLE001 - 진단이 진단 때문에 죽으면 안 된다
        return ""
    if row is None or not row["error"]:
        return ""
    text = " ".join(str(row["error"]).split())
    return f"{text[:70]}… ×{row['c']}" if len(text) > 70 else f"{text} ×{row['c']}"


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

    # 1-b. 프라이빗 모드 — ★ **정보로만 낸다. WARN 으로 올리지 않는다.**
    #
    #   프라이빗이 켜져 있는 것은 고장이 아니라 사람이 그렇게 정한 상태다.
    #   여기서 노란불을 켜면 "켤 때마다 doctor 가 운다" 가 되고, 그러면 사람이
    #   doctor 를 안 본다 (CLAUDE.md §1 — 안 울려야 할 때 우는 검사).
    #
    #   그래도 **적기는 한다.** 커버리지가 왜 낮은지 물었을 때 답이 여기 있어야 한다.
    if conn is not None:
        try:
            from lifetrainer import privacy

            st = privacy.state(conn)
            if st.active:
                left = max(0.0, st.until_ts - st.server_ts) / 60.0
                ok("프라이빗", f"켜짐 — {left:.0f}분 남음. 이 시간은 수집되지 않는다")
            else:
                today = conn.execute(
                    "SELECT COUNT(*) AS c FROM private_span WHERE revoked = 0 AND end_ts > ?",
                    (st.server_ts - 86400,),
                ).fetchone()["c"]
                ok("프라이빗", f"꺼짐 (최근 24시간 구간 {today}개)")
        except Exception as exc:  # noqa: BLE001
            warn("프라이빗", f"상태 조회 실패: {exc}")

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

            # ── 롤업이 **지금도 돌고 있나** ─────────────────────────────
            #
            # ★ 2026-09-01 에 생겼다. 그날 마이그레이션 하나가 밀려서 롤업이 10분마다
            #   죽었는데, **아무것도 그걸 안 잡았다.** doctor 는 "롤업 08-12 ~ 09-01"
            #   이라고 OK 를 찍었다 — 범위는 맞았고, 그 범위가 멈춰 있다는 것만 몰랐다.
            #
            # ★ 왜 `aw_cursor` 로 안 재나 (이걸로 쟀으면 매일 아침 헛경보다):
            #   커서는 **PC 에 새 이벤트가 있을 때만** 전진한다. 노트북을 꺼 두면
            #   밤새 안 움직이는데 그건 고장이 아니다. AW 에 못 닿는 것은 위의
            #   ActivityWatch 항목이 이미 따로 말한다 — 한 사건에 경보는 하나다.
            #
            # ★ `slot.updated_at` 은 다르다. 롤업은 이벤트가 없어도 오늘 144칸을
            #   전부 다시 쓴다(빈 칸은 off). **PC 가 꺼져 있어도 전진한다.**
            #   멈췄다면 멈춘 것은 우리 쪽이다.
            #
            # 언제 꺼지나: 롤업이 한 번 성공하면 즉시. 누적이 아니라 마지막 시각이다.
            STALE_WARN_H = 1.0   # 10분 주기 기준 6회 연속 누락. 재부팅 정도로는 안 운다
            fresh = conn.execute("SELECT MAX(updated_at) AS m FROM slot").fetchone()
            if fresh is None or fresh["m"] is None:
                # ★ 실패가 아니라 **미확인**이다. 갓 만든 DB 는 아직 롤업한 적이 없다.
                ok("롤업 신선도", "아직 롤업한 적 없음 — `lt rollup --today`")
            else:
                age_h = (time.time() - float(fresh["m"])) / 3600.0
                if age_h > STALE_WARN_H:
                    warn(
                        "롤업 신선도",
                        f"마지막 갱신 {age_h:.1f}시간 전 — 10분마다 돌아야 한다."
                        " `journalctl --user -u lifetrainer-sync.service -n 30` 로 원인 확인",
                    )
                else:
                    ok("롤업 신선도", f"{age_h * 60:.0f}분 전")

            # ── 워처 하나가 **혼자** 조용한가 ──────────────────────────
            #
            # ★ 2026-09-04 에 생겼다. 폰 웹 워처가 닷새(08-29~09-03), 미디어 워처가
            #   15시간(09-02~03) 멈춰 있었는데 아무것도 안 잡았다. 위의 "롤업 신선도"는
            #   *우리 쪽 파이프가 도나* 를 보는데, 이번 고장은 **파이프는 도는데 원료
            #   하나가 끊긴 것**이라 그대로 통과한다.
            #
            # ★ 왜 절대 시간으로 안 재나: 밤에는 모든 버킷이 같이 조용하다. "N시간째
            #   조용" 으로 걸면 매일 아침 운다. 그래서 **같은 기기의 세션 버킷과 비교**
            #   한다 — 기기가 살아 있는데 이 워처만 조용하면 그건 고장이다.
            #   기기를 안 쓰면(밤·꺼짐·프라이빗) 세션도 같이 끊겨 조건이 성립하지 않는다.
            #
            # ★ 왜 `aw_bucket.last_seen` 을 못 쓰나: `upsert_bucket` 이 **이벤트가 없어도**
            #   매 sync 마다 now 로 갱신한다. 죽은 워처의 버킷도 계속 신선해 보인다.
            #   그래서 `MAX(ts_end)` 를 본다 (`idx_aw_event_bkt_end` 가 덮는다).
            #
            # 임계값은 실측으로 정했다. 같은 방법을 두 번 돌렸다 —
            # 30분 간격 표본 중 **기기가 살아 있던 시점**만 세고, 발동한 *날* 수를 본다:
            #
            #     대상    6h    12h                    24h   48h
            #     media   5일   1일(실제 사고 09-03)   0일   0일   ← 12h 가 딱 그 하루만 잡는다
            #     unlock  3일   1일(실제 사고 09-04)   0일   0일   ← 〃 (09-04 재측정, 표본 547)
            #     web     13일  9일                    8일   6일
            #
            # ★ **웹은 아직 안 넣는다.** 48시간에도 6일 발동하는데, 관측 기간 대부분
            #   웹 워처가 **실제로 죽어 있었다**(08-22~25 권한 꺼짐, 08-29~09-03 크래시).
            #   저 발동은 오검출이 아니라 진짜다 — 즉 **깨끗한 구간이 없어 임계값을
            #   정할 근거가 없다.** 09-03 16:00 에 살아났으니 일주일 뒤 다시 재서 넣는다.
            #   근거 없는 숫자를 지금 박으면 그게 그대로 굳는다.
            #
            # ★ unlock 은 09-04 에 더했다. **media 를 넣은 그날 unlock 이 죽어 있었다** —
            #   09-03 15:12 이후 조용한데 폰은 계속 쓰이고 있었고, 23일간 최장 간격은
            #   10.2시간(밤)이었다. 하나를 붙이면서 옆의 것을 안 본 것이라,
            #   목록을 아래 표로 뺐다. 다음에 웹을 넣을 때는 한 줄이다.
            #
            # 언제 꺼지나: 그 워처가 이벤트를 하나 넣는 즉시.
            SESSION_FRESH_H = 2.0   # 기기가 "살아 있다"고 볼 기준
            # (버킷 접미사, 화면에 쓸 이름, 뒤처짐 경보 기준 시간)
            WATCHED_PHONE_BUCKETS = (
                ("-media", "media", 12.0),
                ("-unlock", "unlock", 12.0),
            )
            rows = conn.execute(
                """
                SELECT d.name AS device, b.bucket_id, MAX(e.ts_end) AS last_end
                FROM aw_event e
                JOIN aw_bucket b ON b.bucket_id = e.bucket_id
                JOIN device d ON d.id = b.device_id
                WHERE d.kind = 'phone'
                GROUP BY d.name, b.bucket_id
                """
            ).fetchall()
            now = time.time()
            by_device: dict[str, dict[str, float]] = {}
            for row in rows:
                by_device.setdefault(row["device"], {})[row["bucket_id"]] = float(row["last_end"])

            quiet: list[str] = []
            for device, ends in by_device.items():
                # 세션 = 앱 사용 버킷. 미디어·unlock·web 은 세션이 아니다.
                session_ends = [
                    v for k, v in ends.items()
                    if not any(k.endswith(sfx) for sfx in ("-media", "-unlock", "-web"))
                ]
                if not session_ends:
                    continue
                session_end = max(session_ends)
                if (now - session_end) / 3600.0 > SESSION_FRESH_H:
                    continue  # 기기가 조용하다 — 워처 탓이 아니다
                for suffix, label, warn_h in WATCHED_PHONE_BUCKETS:
                    last = next((v for k, v in ends.items() if k.endswith(suffix)), None)
                    if last is None:
                        # ★ 옳지만 아직 증명 못 한 상태다. 한 번도 안 튼 기기를 고장으로
                        #   세지 않는다 (CLAUDE.md §1).
                        continue
                    lag_h = (session_end - last) / 3600.0
                    if lag_h > warn_h:
                        quiet.append(f"{device} {label} {lag_h:.0f}시간 뒤처짐")
            if not rows:
                ok("워처 침묵", "폰 기기가 없다")
            elif quiet:
                warn(
                    "워처 침묵",
                    ", ".join(quiet)
                    + " — 폰이 쓰이는 중인데 그 워처만 조용하다."
                    " 알림 접근/접근성 권한이 앱 업데이트로 풀렸을 수 있다",
                )
            else:
                ok("워처 침묵", "폰 워처가 기기와 같이 살아 있다")

            # ── 수집 소스가 **죽어 있나** ──────────────────────────────
            #
            # ★ 2026-09-07 에 생겼다. 그날 `source` 표를 눈으로 보니
            #   BAIR 가 11회 연속 실패(9.7일째), Microsoft Research 중복 항목이 404 였다.
            #   **`fail_count` 를 읽는 코드가 하나도 없었다** — 세기만 하고 아무도 안 봤다.
            #   *만들었다 ≠ 그게 실제로 불린다* (저장소 규칙 §2).
            #
            #   같은 날 더 나쁜 것도 나왔다: arXiv 8개는 `last_status` 가 전부 NULL 이었다.
            #   `_mark_source_result` 가 `url_state` 를 `source.url`("cat:cs.CL")로 찾는데
            #   실제 요청 URL 은 파라미터가 붙은 다른 주소라 **언제나 못 찾았다**.
            #   그래서 아래 두 번째 갈래가 있다 — 결과가 **안 적히는 것** 자체가 고장이다.
            #   (그건 "실패 0건" 과 구분이 안 되므로 fail_count 로는 영영 안 보인다.)
            #
            # 언제 꺼지나: 그 소스가 **한 번 성공하면 즉시**. 누적이 아니다
            #   (`_mark_source_result` 가 성공 시 fail_count 를 0 으로 되돌린다).
            #
            # 왜 3회인가: 백오프가 붙어 있어 3회면 이미 수 시간~하루다. 1회로 잡으면
            #   남의 서버 502 한 번에 매번 노란불이 켜지고, 그러면 사람이 doctor 를 안 본다.
            #
            # 한 번도 안 받아 본 소스(`last_fetched IS NULL`)는 **미확인이지 실패가 아니다.**
            SOURCE_FAIL_WARN = 3
            dead = conn.execute(
                "SELECT name, last_status, fail_count FROM source "
                "WHERE enabled = 1 AND fail_count >= ? ORDER BY fail_count DESC, name",
                (SOURCE_FAIL_WARN,),
            ).fetchall()
            mute = conn.execute(
                "SELECT name FROM source "
                "WHERE enabled = 1 AND last_fetched IS NOT NULL AND last_status IS NULL "
                "ORDER BY name",
            ).fetchall()
            if dead or mute:
                parts = []
                if dead:
                    parts.append(
                        "연속 실패: "
                        + ", ".join(f"{r['name']}({r['fail_count']}회/{r['last_status'] or '응답없음'})" for r in dead)
                    )
                if mute:
                    names = [r["name"] for r in mute]
                    shown = ", ".join(names[:3]) + (f" 외 {len(names) - 3}개" if len(names) > 3 else "")
                    parts.append(f"결과가 안 적히는 소스 {len(names)}개: {shown}")
                warn(
                    "수집 소스",
                    " · ".join(parts) + " — `lt collect --once` 로 재현하고,"
                    " 주소가 죽었으면 config/sources.yaml 에서 enabled: false",
                )
            else:
                total = conn.execute("SELECT COUNT(*) AS n FROM source WHERE enabled = 1").fetchone()["n"]
                ok("수집 소스", f"{total}개 모두 정상")
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

            from lifetrainer.llm.queue import health as queue_health

            qs = queue_stats(conn)
            detail = ", ".join(f"{k}={v}" for k, v in sorted(qs.items())) if qs else "비어 있음"

            # ★ 조회가 됐다고 OK 가 아니다 (2026-08-21). 요약 잡 277건이 503 으로
            #   죽어 있었는데 이 항목이 개수만 나열하고 OK 를 찍어 이틀을 못 봤다.
            #
            # ★ 그렇다고 **누적 실패율**도 아니다 (2026-08-28, issues/0016).
            #   `purge_done` 이 종료 잡을 14일 뒤에 걷어가므로 done 은 회전하는데
            #   failed 는 그 창이 지나야 사라진다. 그래서 원인을 고친 뒤에도 2주 동안
            #   노란불이 남고, **그 사이 새 실패가 들어와도 구분이 안 된다.**
            #   상시 켜진 신호는 신호가 아니다.
            #
            #   판정 축은 "얼마나 실패했나" 가 아니라 **"지금 실패하고 있나"** 다.
            h = queue_health(conn)
            RECENT_H, BACKLOG_H, MIN_SAMPLE, RATE = 48.0, 6.0, 10, 0.10
            since_h = h.hours_since_last_failure

            if h.stale_running:
                # 워커가 잡을 집다 죽으면 실패가 아니라 여기 남는다. 실패율엔 안 잡힌다.
                warn("큐", f"{detail} · running 인데 lease 만료 {h.stale_running}건"
                           " — 워커가 잡을 집은 채 죽었다. `lt queue stats` 확인")
            elif h.oldest_queued_age > BACKLOG_H * 3600:
                # backlog 도 실패율엔 안 잡힌다 — 아무것도 실패하지 않고 그냥 안 돈다.
                warn("큐", f"{detail} · 가장 오래된 대기 잡 {h.oldest_queued_age/3600:.1f}시간"
                           f" (기준 {BACKLOG_H:.0f}h) — 워커가 도는지 확인")
            elif (since_h is not None and since_h <= RECENT_H
                  and h.recent_finished >= MIN_SAMPLE and h.recent_rate >= RATE):
                worst = f" · 최악 {h.worst_kind[0]} {h.worst_kind[1]}/{h.worst_kind[2]}" if h.worst_kind else ""
                top = _top_job_error(conn)
                warn("큐", f"{detail} · 최근 {h.window_days}일 실패율 {h.recent_rate:.0%}"
                           f" ({h.recent_failed}/{h.recent_finished}){worst}"
                           f" · 마지막 실패 {since_h:.0f}시간 전"
                           + (f" — 대표 사유: {top}" if top else ""))
            elif since_h is not None and h.success_since_last_failure:
                # 무더기로 죽었어도 그 뒤로 계속 성공했으면 **고쳐진 것**이다.
                ok("큐", f"{detail} · 마지막 실패 {since_h/24:.1f}일 전, "
                         f"이후 {h.success_since_last_failure:,}건 연속 성공")
            else:
                ok("큐", detail)
        except Exception as exc:  # noqa: BLE001
            warn("큐", f"조회 실패: {exc}")

    # 8-B. 웹 검색 키 (없으면 대화가 URL 을 추측한다 — docs/issues/h-0006-the-model-cites-one-source-and-stays-there.md)
    # ── 임베딩 (RAG) ──
    if cfg.embed.enabled:
        try:
            import requests as _rq

            n_vec = conn.execute("SELECT COUNT(*) FROM doc_embedding").fetchone()[0] if conn else 0
            n_doc = conn.execute("SELECT COUNT(*) FROM doc WHERE dup_of IS NULL").fetchone()[0] if conn else 0
            _rq.get(cfg.embed.base_url.rstrip("/") + "/models", timeout=3).raise_for_status()

            # ★ 행 수 비교만으로는 "밀리는 중" 과 "밀린 채 멈춤" 을 구분 못 한다 (2026-08-28).
            #   전에는 부족하면 무조건 `lt embed --all` 을 권했는데, 그건 증상을 손으로
            #   지우는 것이지 원인을 고치는 게 아니다. 실제 원인은 둘이었다:
            #   야간 한도가 요약 한도에 묶여 유입의 1/6 이었고, 서버가 누수로 죽으면
            #   배치가 그 자리에서 통째로 끝났다. **가장 오래 밀린 문서의 나이**가
            #   그 둘을 구분한다 — 어제 것만 밀렸으면 따라잡는 중이고,
            #   열흘 된 것이 남아 있으면 구조적으로 못 따라가는 것이다.
            stale = conn.execute(
                "SELECT MIN(d.fetched_at) FROM doc d"
                " LEFT JOIN doc_embedding e ON e.doc_id = d.id"
                " WHERE d.dup_of IS NULL AND e.doc_id IS NULL"
            ).fetchone()[0] if conn else None
            # 설정과 다른 모델·차원으로 만들어진 벡터는 검색에서 조용히 틀린 이웃을 준다.
            mism = conn.execute(
                "SELECT COUNT(*) FROM doc_embedding WHERE model <> ? OR dim <> ?",
                (cfg.embed.model, cfg.embed.dim),
            ).fetchone()[0] if conn else 0

            age_d = (time.time() - stale) / 86400 if stale else 0.0
            if mism:
                warn("임베딩", f"벡터 {n_vec}/{n_doc}건 · **{mism}건이 다른 모델·차원**"
                               f" (설정 {cfg.embed.model}/{cfg.embed.dim}) — `lt embed --all --force`")
            elif stale and age_d > 2:
                warn("임베딩", f"벡터 {n_vec}/{n_doc}건 · 가장 오래 밀린 문서 {age_d:.0f}일"
                               f" — 야간 한도({cfg.nightly.embed_limit})가 유입을 못 따라가는지 본다")
            elif stale:
                ok("임베딩", f"{cfg.embed.model} · 벡터 {n_vec}/{n_doc}건 ·"
                             f" 밀린 것 {n_doc - n_vec}건 (가장 오래된 것 {age_d * 24:.0f}시간 — 따라잡는 중)")
            else:
                ok("임베딩", f"{cfg.embed.model} · 벡터 {n_vec}/{n_doc}건 · 밀린 것 없음")
        except Exception as exc:  # noqa: BLE001
            warn("임베딩", f"서버에 닿지 않습니다 ({cfg.embed.base_url}) — 검색이 키워드로만 간다: {exc}")

    try:
        from lifetrainer.llm.websearch import providers_available

        have = providers_available(cfg)
        if have["serper"]:
            # 공급자는 Serper 하나다(2026-08-25 에 네이버 경로를 지웠다). 하나뿐인
            # 것이 정상 구성이라 OK 를 준다 — 못 하는 일을 WARN 으로 권하지 않는다.
            ok("웹 검색", "Serper (구글 경유) — 한국어 질의는 gl=kr 로 간다")
        else:
            warn(
                "웹 검색",
                "키 없음 — 대화가 주소를 추측한다. "
                "config/lifetrainer.toml 의 [search] 에 serper_api_key 를 넣는다 "
                "(serper.dev, 2,500건 무료). 네이버는 API HUB 이관으로 신규 발급 불가 "
                "— HISTORY/2026-08-25-it-worked-because-the-key-was-empty.md",
            )
    except Exception as exc:  # noqa: BLE001
        warn("웹 검색", f"확인 실패: {exc}")

    # 12. OpenClaw 에이전트 배선
    #
    # ★ 이 배선은 **저장소 밖**(`~/.openclaw/openclaw.json`)에 있다. 기기를 다시
    #   세우거나 `openclaw` 를 재설치하면 조용히 사라지는데, 그때 증상은
    #   "에이전트가 툴을 하나도 안 부른다" 뿐이라 원인을 찾기 어렵다. 여기서 본다.
    try:
        _check_agent(cfg, ok, warn, fail)
    except Exception as exc:  # noqa: BLE001 - 진단이 진단 때문에 죽으면 안 된다
        warn("에이전트", f"확인 실패: {exc}")

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


def _check_agent(cfg: Config, ok, warn, fail) -> None:  # noqa: ANN001 - cmd_doctor 의 지역 함수
    """에이전트 쪽 점검 — 예산 · 워크스페이스 · OpenClaw 등록.

    **모델을 부르지 않는다.** `doctor` 는 빨라야 하고, 한 턴이 26초다.
    부르지 않고 알 수 있는 것만 본다.
    """
    import json as _json

    from lifetrainer.agent import catalog as catalog_mod
    from lifetrainer.agent import prompt as prompt_mod
    from lifetrainer.agent.config import build_sandbox, load_settings
    from lifetrainer.llm.client import estimate_tokens

    settings = load_settings(cfg)
    if not settings.enabled:
        warn("에이전트", "config 의 [agent] enabled=false — 꺼져 있다")
        return

    # ① 예산. 넘으면 첫 턴이 길어지고 ctx 20480 에서 압축이 걸린다.
    report = catalog_mod.budget_report()
    sandbox = build_sandbox(cfg, settings)
    prompt_tokens = int(round(estimate_tokens(prompt_mod.build_agents_md(sandbox))))
    over = []
    if report["total"] > report["budget"]:
        over.append(f"툴 {report['total']}/{report['budget']}")
    if prompt_tokens > prompt_mod.PROMPT_TOKEN_BUDGET:
        over.append(f"프롬프트 {prompt_tokens}/{prompt_mod.PROMPT_TOKEN_BUDGET}")
    total = report["total"] + prompt_tokens + catalog_mod.FRAMEWORK_TOKENS
    if over:
        warn("에이전트 예산", f"{', '.join(over)} 초과 — lt agent budget 로 무엇이 큰지 본다")
    else:
        ok("에이전트 예산", f"툴 {report['count']}개 {report['total']} + 프롬프트 {prompt_tokens} → 약 {total} 토큰")

    # ② 감옥이 실제로 서 있나. `Sandbox.build` 가 **없는 폴더를 버리므로**,
    #    오타 하나로 읽기가 통째로 비어도 조용하다.
    if not sandbox.read_roots:
        warn("에이전트 감옥", "허용 폴더가 하나도 없다 — [agent] read_roots 경로를 확인한다")
    elif not sandbox.write_roots:
        warn("에이전트 감옥", "쓰기 폴더가 없다 — 메모를 남길 수 없다")
    else:
        ok("에이전트 감옥", sandbox.describe().replace("\n", " · "))

    # ③ 워크스페이스 프롬프트가 코드와 같은가.
    workspace = Path(settings.workspace)
    if not workspace.is_absolute():
        workspace = cfg.root / workspace
    target = workspace / prompt_mod.WORKSPACE_FILE
    if not target.is_file():
        warn("에이전트 프롬프트", f"{target} 이 없다 — bash scripts/install-agent.sh")
    elif target.read_text(encoding="utf-8") != prompt_mod.build_agents_md(sandbox):
        # 게이트웨이가 워크스페이스를 다시 시드했거나 모델이 고쳐 썼을 수 있다.
        warn("에이전트 프롬프트", "코드가 만드는 내용과 다르다 — lt agent prompt 로 다시 쓴다")
    else:
        ok("에이전트 프롬프트", f"{target.name} 최신")

    # ④ Slack 위임이 실제로 닿는가.
    #
    # ★ 이 항목이 없어서 **위임이 조용히 강등된 채 몇 시간을 돌았다.** 개발 셸에는
    #   nvm PATH 가 있어 `openclaw` 가 찾아졌지만 systemd 서비스에는 없었다.
    #   로그에 WARNING 이 찍혔는데, 답이 빠르고 그럴듯해서 아무도 안 봤다.
    if settings.slack:
        from lifetrainer.agent import delegate

        binary = delegate.resolve_bin(settings.openclaw_bin)
        if not binary:
            fail(
                "에이전트 Slack 위임",
                "openclaw 실행 파일을 못 찾았다 — 자연어 DM 이 조용히 옛 경로로 간다. "
                "config 의 [agent] openclaw_bin 에 절대 경로를 넣는다",
            )
        elif not binary.startswith(("/usr/local/", "/usr/bin/")):
            # 서비스 PATH 밖(nvm 등)이면 찾아지긴 해도 버전 매니저에 묶여 있다.
            #
            # ★ 2026-08-28: 게이트웨이 유닛도 같이 본다. 유닛은 이미 시스템 Node 로
            #   **실행**되는데(`/usr/local/bin/node`), 실행하는 **스크립트**는 여전히
            #   nvm 안의 node_modules 다. 즉 nvm 을 지우면 CLI 뿐 아니라 게이트웨이도
            #   같이 죽는다. 여기를 안 보고 있어서 절반만 옮긴 상태가 안 드러났다.
            gw = ""
            try:
                gw = subprocess.run(
                    ["systemctl", "--user", "show", "openclaw-gateway", "-p", "ExecStart", "--value"],
                    capture_output=True, text=True, timeout=5,
                ).stdout
            except Exception as exc:  # noqa: BLE001
                gw = f"<확인 실패: {exc}>"
            if "/.nvm/" in gw:
                also = " · **게이트웨이 스크립트도 같은 곳에 있다**"
            elif gw.startswith("<확인 실패"):
                # ★ 못 봤으면 못 봤다고 말한다. 조용히 "괜찮음" 으로 넘어가면
                #   절반만 옮겨진 상태가 이번에도 안 드러난다.
                also = f" · 게이트웨이 경로 {gw}"
            else:
                also = ""
            warn(
                "에이전트 Slack 위임",
                f"{binary} — 버전 매니저 경로다. nvm 을 갈아엎으면 조용히 끊긴다{also}. "
                "옮기는 법: `bash life-trainer/deploy/install-openclaw-system.sh` (sudo 로 감싸지 말 것) "
                "(`operate/notes/agent-gateway.md §4-10`)",
            )
        else:
            ok("에이전트 Slack 위임", binary)
    else:
        ok("에이전트 Slack 위임", "꺼짐 ([agent] slack=false — 자연어는 빠른 경로로)")

    # ⑤ 압축 설정이 **지금의 ctx** 와 맞는가.
    #
    # ★ 이 검사가 없어서 대화가 한 시간쯤 이어지자 `Context overflow` 로 죽었다.
    #   08-15 에 ctx 40,960 기준으로 정한 값이 08-23 의 ctx 축소 뒤에도 남아 있었고,
    #   그때의 재계산이 **시스템 프롬프트를 빼먹었다** (HISTORY 2026-08-24).
    #   같은 누락이 `main` 에이전트를 이미 죽여 놨다 — 한 곳에서 본 원인을
    #   옆으로 밀어 보지 않은 것이 이 사고의 절반이다.
    _check_compaction(cfg, total, ok, warn, fail)

    # ⑥ OpenClaw 쪽 등록. 설정 파일만 읽는다 — 게이트웨이를 부르지 않는다.
    openclaw_path = Path(cfg.slack.openclaw_config)
    if not openclaw_path.is_file():
        warn("에이전트 등록", f"{openclaw_path} 가 없다 — OpenClaw 가 설치되지 않았다")
        return
    try:
        raw = _json.loads(openclaw_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        warn("에이전트 등록", f"openclaw.json 을 읽지 못했다: {exc}")
        return

    agents = [a.get("id") for a in (raw.get("agents") or {}).get("list") or []]
    server = ((raw.get("mcp") or {}).get("servers") or {}).get(catalog_mod.SERVER_NAME)
    missing = []
    if settings.agent_id not in agents:
        missing.append(f"에이전트 '{settings.agent_id}'")
    if not server:
        missing.append(f"MCP 서버 '{catalog_mod.SERVER_NAME}'")
    elif server.get("enabled") is False:
        missing.append(f"MCP 서버 '{catalog_mod.SERVER_NAME}' 가 꺼져 있다")
    if missing:
        warn("에이전트 등록", f"{', '.join(missing)} 없음 — bash scripts/install-agent.sh")
    else:
        ok("에이전트 등록", f"agent={settings.agent_id} · mcp={catalog_mod.SERVER_NAME}")


def _check_compaction(cfg: Config, prompt_tokens: int, ok, warn, fail) -> None:  # noqa: ANN001
    """`시스템 프롬프트 + keepRecent + reserve` 가 ctx 안에 드는가.

    OpenClaw 의 압축 설정은 **전역**이라 ctx 를 바꿔도 따라오지 않는다. 남는 자리가
    한 턴치도 안 되면 대화가 길어지는 순간 죽는다 — 그때 사용자가 보는 것은
    영어 오류 문장 하나뿐이다.
    """
    import json as _json

    path = Path(cfg.slack.openclaw_config)
    if not path.is_file():
        return
    try:
        raw = _json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return

    agents = raw.get("agents") or {}
    comp = (agents.get("defaults") or {}).get("compaction") or {}
    keep = int(comp.get("keepRecentTokens") or 0)
    reserve = int(comp.get("reserveTokens") or 0)
    models = ((raw.get("models") or {}).get("providers") or {}).get("llamacpp", {}).get("models") or []
    ctx = int(models[0].get("contextWindow") or 0) if models else 0
    if not (ctx and keep and reserve):
        return

    # 한 턴이 쓰는 최소치. 남는 자리가 이보다 작으면 곧 넘친다.
    headroom = ctx - (prompt_tokens + keep + reserve)
    detail = f"ctx {ctx} − (프롬프트 {prompt_tokens} + keepRecent {keep} + reserve {reserve}) = 여유 {headroom}"
    if headroom < 2000:
        fail(
            "에이전트 압축 여유",
            f"{detail} — 대화가 길어지면 Context overflow 로 죽는다. "
            "`openclaw config set agents.defaults.compaction.keepRecentTokens 3000` 등으로 낮춘다",
        )
    elif headroom < 5000:
        warn("에이전트 압축 여유", f"{detail} — 빠듯하다")
    else:
        ok("에이전트 압축 여유", detail)


def cmd_init_db(args: argparse.Namespace, cfg: Config) -> int:
    """스키마 + 마이그레이션 적용. `open_db` 가 둘 다 한다 (2026-09-01 부터).

    ★ 전에는 "schema v5 적용 완료" 라고 찍었는데 그건 **상수를 출력한 것**이지
      적용한 결과가 아니었다. 실제로는 마이그레이션이 하나도 안 돌고 있었다.
      이제 적용된 표식을 세서 말한다 — 숫자를 지어내지 않는다.
    """
    conn = db.open_db(cfg)
    applied = [
        r[0].split(":", 1)[1]
        for r in conn.execute("SELECT key FROM meta WHERE key LIKE 'migration:%' ORDER BY key")
    ]
    conn.close()
    print(f"스키마 적용 완료: {cfg.db_path}")
    print(f"  마이그레이션 {len(applied)}개 적용됨: {', '.join(applied) or '(없음)'}")
    return 0


# ── 프라이빗 모드 ────────────────────────────────────────────────────────


def _private_line(st, cfg: Config) -> str:
    if not st.active:
        return "프라이빗: 꺼짐"
    left = max(0.0, st.until_ts - st.server_ts)
    return f"프라이빗: 켜짐 — {left / 60:.0f}분 남음 (만료 {_fmt_local(st.until_ts, cfg)})"


def _fmt_local(ts: float, cfg: Config) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.fromtimestamp(ts, ZoneInfo(cfg.timezone)).strftime("%H:%M")


def cmd_private_on(args: argparse.Namespace, cfg: Config) -> int:
    from lifetrainer import privacy

    minutes = args.minutes if args.minutes is not None else cfg.private.default_minutes
    if not (0 < minutes <= cfg.private.max_minutes):
        print(f"minutes 는 0 보다 크고 {cfg.private.max_minutes} 이하여야 합니다", file=sys.stderr)
        return 2
    conn = db.open_db(cfg)
    try:
        st = privacy.begin(conn, minutes, source="cli")
    finally:
        conn.close()
    print(_private_line(st, cfg))
    print("  이 시간의 창 제목·앱 이름은 저장되지 않습니다.")
    return 0


def cmd_private_off(args: argparse.Namespace, cfg: Config) -> int:
    from lifetrainer import privacy

    conn = db.open_db(cfg)
    try:
        st = privacy.end_now(conn)
    finally:
        conn.close()
    print(_private_line(st, cfg))
    print("  끈 시각부터 다시 기록됩니다 — 구간 안의 기록은 돌아오지 않습니다.")
    return 0


def cmd_private_status(args: argparse.Namespace, cfg: Config) -> int:
    from lifetrainer import privacy

    conn = db.open_db(cfg)
    try:
        st = privacy.state(conn)
        rows = conn.execute(
            "SELECT start_ts, end_ts, kind, source FROM private_span "
            "WHERE revoked = 0 AND end_ts > ? ORDER BY start_ts",
            (st.server_ts - 86400,),
        ).fetchall()
        trash, oldest = privacy.trash_count(conn)
    finally:
        conn.close()
    print(_private_line(st, cfg))
    # ★ **안 보이면 잊힌다.** "지웠다" 고 생각한 것이 디스크에 남아 있는 상태를
    #   사람이 모르면 안 된다 — `forget` 은 자동으로 안 돈다.
    if trash:
        age = f", 가장 오래된 것 {(time.time() - oldest) / 86400:.1f}일 전" if oldest else ""
        print(f"  휴지통: 표시 삭제된 이벤트 {trash}건{age} — 완전 삭제는 lt private forget")
    if rows:
        print("  최근 24시간 구간:")
        for r in rows:
            print(
                f"    {_fmt_local(r['start_ts'], cfg)}–{_fmt_local(r['end_ts'], cfg)}"
                f"  {r['kind']}  ({r['source']})"
            )
    return 0


def cmd_private_undo(args: argparse.Namespace, cfg: Config) -> int:
    """표시 삭제를 되돌린다. 기본은 **가장 최근 삭제**다 — 오클릭 직후가 대부분이다."""
    from lifetrainer import privacy
    from lifetrainer.rollup.classify import Classifier
    from lifetrainer.rollup.rollup import rollup_day

    conn = db.open_db(cfg)
    try:
        row = conn.execute(
            "SELECT id, start_ts, end_ts FROM private_span WHERE kind = 'purge' AND revoked = 0 "
            + ("AND id = ? " if args.span_id else "")
            + "ORDER BY created_at DESC, id DESC LIMIT 1",
            (args.span_id,) if args.span_id else (),
        ).fetchone()
        if row is None:
            print("되돌릴 삭제가 없습니다.", file=sys.stderr)
            return 1

        n = privacy.undo(conn, int(row["id"]))
        if n == 0:
            # `forget` 으로 이미 완전 삭제된 구간이다. 구간 취소만 되고 기록은 안 온다.
            print(
                f"이벤트가 되살아나지 않았습니다 — 이 구간은 이미 `lt private forget` 으로 "
                f"완전 삭제됐습니다. 구간 표시만 해제했습니다.",
                file=sys.stderr,
            )
        days = privacy.affected_days(cfg, float(row["start_ts"]), float(row["end_ts"]))
        classifier = Classifier.from_yaml(cfg.rollup.rules_path)
        for day in days:
            rollup_day(conn, cfg, classifier, day)
        print(
            f"되돌림: 이벤트 {n}건 · 재롤업 {len(days)}일 "
            f"({_fmt_local(float(row['start_ts']), cfg)}–{_fmt_local(float(row['end_ts']), cfg)})"
        )
        return 0
    finally:
        conn.close()


def cmd_private_forget(args: argparse.Namespace, cfg: Config) -> int:
    """표시 삭제된 것을 진짜로 지운다. **여기부터는 되돌릴 수 없다.**"""
    from lifetrainer import privacy

    conn = db.open_db(cfg)
    try:
        n, oldest = privacy.trash_count(conn)
        if n == 0:
            print("휴지통이 비어 있습니다.")
            return 0
        age = f" (가장 오래된 것 {(time.time() - oldest) / 86400:.1f}일 전)" if oldest else ""
        print(f"표시 삭제된 이벤트 {n}건{age}")
        if not args.yes:
            print("  진짜로 지우려면 --yes 를 붙이세요. 이건 되돌릴 수 없습니다.")
            return 0
        print(f"완전 삭제: {privacy.forget(conn)}건")
        return 0
    finally:
        conn.close()


def cmd_private_purge(args: argparse.Namespace, cfg: Config) -> int:
    from lifetrainer import privacy
    from lifetrainer.rollup.classify import Classifier
    from lifetrainer.rollup.rollup import rollup_day

    now_ts = time.time()
    if args.day:
        # ★ 하루를 통째로 지운다. **논리적 하루**(06:00~다음날 06:00)를 쓴다 —
        #   자정 기준으로 지우면 새벽 활동이 어제 격자에 남아 "지웠는데 아직 있다" 가 된다.
        if not args.yes:
            print("--day 는 --yes 가 필요합니다 (하루치는 되돌릴 수 없습니다)", file=sys.stderr)
            return 2
        if args.range or args.minutes is not None:
            print("--day 는 --range·--minutes 와 같이 못 씁니다", file=sys.stderr)
            return 2
        from zoneinfo import ZoneInfo

        start_ts, end_ts = timeutil.day_bounds(
            args.day, ZoneInfo(cfg.timezone), boundary_hour=cfg.rollup.day_boundary_hour
        )
    elif args.range:
        if not args.yes:
            print("--range 는 --yes 가 필요합니다 (임의 구간은 되돌릴 수 없습니다)", file=sys.stderr)
            return 2
        start_ts, end_ts = (_parse_local_ts(v, cfg) for v in args.range)
    else:
        minutes = args.minutes if args.minutes is not None else cfg.private.default_minutes
        if not (0 < minutes <= cfg.private.max_purge_minutes):
            print(f"minutes 는 0 보다 크고 {cfg.private.max_purge_minutes} 이하여야 합니다", file=sys.stderr)
            return 2
        start_ts, end_ts = now_ts - minutes * 60.0, now_ts

    conn = db.open_db(cfg)
    try:
        preview = privacy.purge(conn, cfg, start_ts, end_ts, dry_run=True, now=now_ts)
        print(
            f"{_fmt_local(start_ts, cfg)}–{_fmt_local(end_ts, cfg)} 구간: "
            f"이벤트 {preview.events}건 · 활동 {preview.seconds / 60:.0f}분 · 날짜 {len(preview.days)}개"
        )
        if not args.yes:
            # ★ 미리보기만 하고 아무것도 안 바꾼 채로 끝난다. 정상 종료다.
            print("  실제로 지우려면 --yes 를 붙이세요.")
            return 0

        report = privacy.purge(conn, cfg, start_ts, end_ts, source="cli", now=now_ts)
        classifier = Classifier.from_yaml(cfg.rollup.rules_path)
        for day in report.days:
            rollup_day(conn, cfg, classifier, day)
        print(
            f"삭제 완료: 이벤트 {report.events}건, 재롤업 {len(report.days)}일\n"
            f"  되돌리려면: lt private undo   (표시만 지운 상태다 — "
            f"완전 삭제는 lt private forget)"
        )

        if args.aw or cfg.private.purge_aw:
            _purge_aw(cfg, start_ts, end_ts)
    finally:
        conn.close()
    return 0


def _purge_aw(cfg: Config, start_ts: float, end_ts: float) -> None:
    """엔드포인트 AW 로컬 DB 정리. **실패해도 전체를 실패시키지 않는다.**

    젯슨 DB 차단은 이미 성립했다. 남의 기기가 꺼져 있다고 이 명령이 실패로
    끝나면, 사람은 삭제가 안 된 줄 알고 다시 돌린다.
    """
    from lifetrainer import privacy
    from lifetrainer.collect.aw_client import AWClient

    try:
        client = AWClient(base_url=cfg.aw.base_url, api_key=cfg.aw.api_key)
        n = privacy.purge_aw_local(client, start_ts, end_ts)
        print(f"  AW 로컬 DB: {n}건 삭제")
    except Exception as exc:  # noqa: BLE001
        print(f"  AW 로컬 DB 정리 실패 (젯슨 DB 는 이미 정리됨): {exc}", file=sys.stderr)


def _parse_local_ts(value: str, cfg: Config) -> float:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(cfg.timezone)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=tz).timestamp()
        except ValueError:
            continue
    raise SystemExit(f"시각을 못 읽었습니다: {value!r} ('YYYY-MM-DD HH:MM')")


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
    """`collect/ingest.days_touched` 로 옮겼다 — 웹 수신 경로와 같은 계산을 써야 한다."""
    from lifetrainer.collect.ingest import days_touched

    return days_touched(cfg, result)


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


def cmd_backfill_abstracts(args: argparse.Namespace, cfg: Config) -> int:
    """제목뿐인 문서에 초록을 채운다.

    ★ 채우고 나면 **임베딩을 다시 돌려야 값어치가 난다.** `abstract` 가 임베딩 원문의
    일부라 `source_hash` 가 달라지고, 다음 `lt embed` 가 알아서 다시 만든다.
    """
    from lifetrainer.collect import abstracts as A

    conn = db.open_db(cfg)
    try:
        todo = len(A.pending(conn, 10_000, source=args.source))
        if not todo:
            print("근거가 없는 문서가 없습니다.")
            return 0
        print(f"근거 없는 문서 {todo}건 · 이번에 {min(todo, args.limit)}건 처리"
              f"{' (dry-run)' if args.dry_run else ''}")

        def show(i, n, title):
            print(f"  [{i}/{n}] {title[:56]}", flush=True)

        r = A.backfill(
            conn, cfg, limit=args.limit, source=args.source, dry_run=args.dry_run, progress=show
        )
        print(
            f"초록 {r.filled}건 채움 · {r.too_short}건 너무 짧음 · "
            f"{r.boilerplate}건 보일러플레이트 · {r.failed}건 실패 · 남은 것 {todo - r.filled}건"
        )
        if r.stopped_early:
            print("상대 서버가 429 를 보내 물러났습니다. 잠시 뒤 같은 명령을 다시 돌리면 이어 합니다.")
        if r.filled and not args.dry_run:
            print("→ `lt embed --all` 로 벡터를 다시 만드세요 (원문이 바뀌었습니다).")
        return 0
    finally:
        conn.close()


def cmd_embed(args: argparse.Namespace, cfg: Config) -> int:
    """수집해 둔 문서를 벡터로 만든다. 야간 배치가 부르는 것과 같은 경로다."""
    from lifetrainer.llm import embed as E

    if not cfg.embed.enabled:
        print("임베딩이 꺼져 있습니다 (config 의 [embed] enabled = true).", file=sys.stderr)
        return 1

    conn = db.open_db(cfg)
    try:
        total = E.EmbedResult()
        while True:
            try:
                r = E.embed_pending(conn, cfg, limit=args.limit)
            except E.EmbedUnavailable as exc:
                print(f"임베딩 서버에 연결할 수 없습니다: {exc}", file=sys.stderr)
                return 1
            total.embedded += r.embedded
            total.skipped += r.skipped
            total.failed += r.failed
            print(f"  +{r.embedded}건 (누적 {total.embedded})")
            if not args.all or r.embedded == 0:
                break
        left = len(E.pending_docs(conn, cfg, 10_000))
        print(f"임베딩 {total.embedded}건 생성 · {total.skipped}건 건너뜀 · "
              f"{total.failed}건 실패 · 남은 것 {left}건")
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


def cmd_bodies(args: argparse.Namespace, cfg: Config) -> int:
    """초록이 짧은 문서의 전문을 받는다.

    ★ **타이머가 없다. 손으로 부른다.** 받은 본문이 실제로 답을 낫게 하는지 보기 전에
      자동으로 돌리면 *만들었다 ≠ 그게 값을 한다* 를 확인할 기회가 사라진다
      (`collect/bodies.py` 머리말).
    """
    from lifetrainer.collect.bodies import collect_bodies

    conn = db.open_db(cfg)
    try:
        with db.transaction(conn):
            r = collect_bodies(conn, cfg, limit=args.limit)
        print(
            f"본문 수집: 저장 {r.fetched}건 · 내용 부족 {r.too_thin}건 · "
            f"robots 차단 {r.skipped_robots}건 · 실패 {r.failed}건"
        )
        return 0
    finally:
        conn.close()


def cmd_score(args: argparse.Namespace, cfg: Config) -> int:
    from lifetrainer.collect.score import rescore_all, score_pending

    conn = db.open_db(cfg)
    try:
        if getattr(args, "rescore", False):
            # 2026-09-04 이전에 매겨진 점수에는 최신성 보정이 곱해져 굳어 있다.
            # 씻어내는 일회성 작업이지만, 관심사 가중치를 고친 뒤에도 쓸 수 있다.
            with db.transaction(conn):
                n = rescore_all(conn, cfg)
            print(f"재채점 완료: {n}건 (기준점수 — 최신성은 읽을 때 곱한다)")
            return 0
        n = score_pending(conn, cfg)
        print(f"스코어링 완료: {n}건")
        return 0
    finally:
        conn.close()


_DIGEST_TAG_RE = __import__("re").compile(r"<[^>]+>")
_DIGEST_WS_RE = __import__("re").compile(r"\s+")
_DIGEST_ENTITIES = {"&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'"}


def _plain_text(text: str) -> str:
    """수집한 글의 요약·초록에 섞인 마크업을 걷어낸다.

    ★ `llm/tools.py._plain` 과 같은 일을 한다. 그쪽은 툴 결과(프롬프트)용이고 여기는
    Slack 카드용이라 호출 경로가 겹치지 않지만, **규칙이 갈라지면 같은 글이 두 화면에서
    다르게 보인다.** 하나로 합칠 때가 오면 `text` 유틸로 빼는 게 맞다.
    """
    if not text:
        return ""
    out = _DIGEST_TAG_RE.sub(" ", text)
    for k, v in _DIGEST_ENTITIES.items():
        out = out.replace(k, v)
    return _DIGEST_WS_RE.sub(" ", out).strip()


def cmd_digest(args: argparse.Namespace, cfg: Config) -> int:
    """아침 다이제스트 — 키워드 랭킹 상위 문서만 나열한다 (LLM 없음)."""
    from lifetrainer.collect.score import mark_digested, top_docs
    from lifetrainer.slackio import blocks as blocks_mod

    conn = db.open_db(cfg)
    try:
        today = _today(cfg)
        docs = top_docs(conn, limit=5)

        if not docs:
            # ★ 문구를 "새로 수집된" 에서 "아직 안 보낸" 으로 바꿨다 (2026-09-04).
            #   전에는 전체 코퍼스를 훑으면서 **"새로 수집된"** 이라고 말했다 —
            #   말과 코드가 달랐다. 지금은 실제로 "안 보낸 것" 을 고른다.
            text = f"{today} 아침 다이제스트: 아직 안 보낸 관심 문서가 없습니다."
            digest_blocks = [
                blocks_mod.header(f"아침 다이제스트 — {today}"),
                blocks_mod.section("아직 안 보낸 관심 문서가 없습니다."),
            ]
        else:
            # ★ 한 덩어리 문단이 아니라 **문서당 한 블록**으로 나눈다.
            #
            # 전에는 5건을 불릿으로 이어 붙여 하나의 section 에 넣었다. 요약이 3~4문장
            # 이라 문단 다섯 개가 붙어 버려서 어디서 끊기는지 눈으로 못 찾았다.
            # (게다가 Slack 링크 미리보기가 각 링크를 본문 카드로 펼쳐 화면 절반을 먹었다
            #  — notify.post 에서 unfurl 을 껐다.)
            #
            # 지금 모양: 굵은 제목 링크 → 요약 2줄 → 출처·점수 한 줄.
            digest_blocks = [blocks_mod.header(f"아침 다이제스트 — {today}")]
            lines = []
            for i, d in enumerate(docs, 1):
                gist = _plain_text(d["summary"] or d["abstract"] or "")
                title = f"*{i}. <{d['url']}|{blocks_mod._truncate(d['title'] or '제목 없음', 110)}>*"
                body = title + (f"\n{blocks_mod._truncate(gist, 220)}" if gist else "")
                digest_blocks.append(blocks_mod.section(body))
                # ★ 보정 **후** 점수를 보여준다. 저장된 `score` 는 시간이 안 들어간
                #   기준점수라 화면에 쓰면 순위와 숫자가 어긋난다.
                meta = [f"점수 {float(d['ranked_score'] or 0):.1f}"]
                src = conn.execute("SELECT name FROM source WHERE id = ?", (d["source_id"],)).fetchone()
                if src:
                    meta.append(src["name"])
                if not d["summary"]:
                    meta.append("요약 없음 — 초록에서 발췌")
                digest_blocks.append(blocks_mod.context([" · ".join(meta)]))
                lines.append(f"• <{d['url']}|{d['title']}>" + (f" — {gist[:120]}" if gist else ""))
            text = f"{today} 아침 다이제스트\n" + "\n".join(lines)

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
                        # ★ **발송에 성공한 뒤에만** 표식을 단다. 만들기만 한 것까지
                        #   표시하면 미리보기 한 번에 그 문서가 영영 안 나간다.
                        n = mark_digested(conn, [d["id"] for d in docs])
                    print(f"Slack 발송 완료 (ts={ts}) · 문서 {n}건을 발송 완료로 표시")
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

            # ★ 임베딩은 **요약이 끝난 뒤** 여기서 돈다.
            #
            # 왜 여기인가: 임베딩 원문이 `title + abstract + summary` 라, 요약이 붙으면
            # 원문이 바뀌고 벡터를 다시 만들어야 한다. 배치 시작(02:00)에 돌리면 그날
            # 새로 붙은 요약을 못 본다. 창이 닫히는 05:50 이 요약이 다 앉은 시점이다.
            #
            # 실패해도 종료 자체는 성공으로 둔다 — 임베딩이 없으면 검색이 키워드로
            # 떨어질 뿐이고, 다음 밤이나 `lt embed` 로 언제든 따라잡을 수 있다.
            if cfg.embed.enabled:
                from lifetrainer.llm import embed as E

                try:
                    r = E.embed_pending(conn, cfg, limit=cfg.nightly.embed_limit)
                    print(f"임베딩 {r.embedded}건 생성 (실패 {r.failed}건).")
                except E.EmbedUnavailable as exc:
                    print(f"임베딩 서버 없음 — 건너뜁니다: {exc}")

            # ★ 종료된 잡 걷어내기. **여기가 `purge_done` 의 첫 호출자다 (2026-09-01).**
            #
            #   그전까지 이 함수는 만들어만 두고 아무도 안 불렀다. 그런데 `lt doctor` 의
            #   큐 판정 주석은 *"purge_done 이 14일 뒤에 걷어가므로 done 은 회전한다"* 를
            #   전제로 쓰여 있었다 — 실측하니 가장 오래된 done 이 17일 전이었다.
            #   **주석이 코드보다 낙관적이었다.**
            #
            #   왜 여기인가: `--stop` 은 새벽 창이 닫히는 05:50 에 하루 한 번 돈다.
            #   워커가 잡을 집고 있지 않은 유일하게 조용한 시점이라 지우기 안전하다.
            #
            #   지운 건수를 **찍는다.** 조용히 지우면 다음 사람이 또 "도는지 안 도는지"를
            #   실측해야 한다.
            days = cfg.nightly.job_retention_days
            if days > 0:
                from lifetrainer.llm.queue import purge_done

                purged = purge_done(conn, older_than_days=days)
                print(f"종료 잡 정리: {purged}건 삭제 ({days}일 이상 지난 done/failed/cancelled).")
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
        today = timeutil.day_str(
            timeutil.now_ts(), cfg.tz, boundary_hour=cfg.rollup.day_boundary_hour
        )
        archived = delete_plan(conn, args.plan_id, from_day=today)
        print(f"계획 삭제됨: id={args.plan_id} ({existing.title!r}) · 오늘 이후 인스턴스 {archived}건 보관")
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
    """반복 계획을 그 날짜만 건너뛴다. `--off` 로 되돌린다.

    ★ 되돌리기가 **없었다** (2026-09-01 까지). `unskip_plan` 은 만들어져 있었는데
      부르는 곳이 하나도 없었고, 웹은 건너뛴 계획을 목록에서 아예 빼기 때문에
      잘못 누르면 DB 를 직접 손대는 것 말고는 길이 없었다.
      `lt plan check --off` 와 같은 관례를 따른다.

    ★ 한계: `plan_skip` 은 **전개(materialize) 전에만** 효과가 있다. 이미 그날의
      인스턴스가 만들어진 뒤면 건너뛰기도 되돌리기도 화면을 안 바꾼다 —
      `docs/issues/0026`. 이 명령은 그 경계를 고치지 않는다.
    """
    from lifetrainer.plan.models import get_plan, skip_plan, unskip_plan

    conn = db.open_db(cfg)
    try:
        existing = get_plan(conn, args.plan_id)
        if existing is None:
            raise CliError(f"계획을 찾을 수 없습니다: id={args.plan_id}")
        if args.off:
            unskip_plan(conn, args.plan_id, args.day)
            print(f"{args.day} {existing.title!r} 건너뛰기 해제")
        else:
            skip_plan(conn, args.plan_id, args.day)
            print(f"{args.day} {existing.title!r} 건너뜀 (되돌리려면 --off)")
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


# ── agent ────────────────────────────────────────────────────────────


def cmd_agent_mcp(args: argparse.Namespace, cfg: Config) -> int:
    """MCP stdio 서버. **stdout 은 전송로다** — 여기서 print 하면 프로토콜이 깨진다."""
    from lifetrainer.agent import mcp_server
    from lifetrainer.agent.catalog import AgentContext
    from lifetrainer.agent.config import build_sandbox

    ctx = AgentContext(cfg=cfg, sandbox=build_sandbox(cfg), actor="agent")
    logger.info("MCP 서버 시작 — %s", ctx.sandbox.describe().replace("\n", " · "))
    mcp_server.serve(ctx)
    return 0


def cmd_agent_budget(args: argparse.Namespace, cfg: Config) -> int:
    """툴 스키마와 워크스페이스 프롬프트가 각각 몇 토큰인지 센다.

    `operate/notes/agent-gateway.md §3` 의 12,541 토큰과 같은 자리에서 비교할 수 있게
    **한 화면에** 낸다. 눈대중으로 "이 정도면 되겠지" 하지 않기 위한 명령이다.
    """
    from lifetrainer.agent import catalog as catalog_mod
    from lifetrainer.agent import prompt as prompt_mod
    from lifetrainer.agent.config import build_sandbox
    from lifetrainer.llm.client import estimate_tokens

    report = catalog_mod.budget_report()
    agents_md = prompt_mod.build_agents_md(build_sandbox(cfg))
    prompt_tokens = int(round(estimate_tokens(agents_md)))

    print(f"툴 {report['count']}개 — {report['total']} 토큰 (예산 {report['budget']})")
    for name, tokens in report["per_tool"].items():
        print(f"  {tokens:5d}  {name}")
    print(f"워크스페이스 AGENTS.md — {prompt_tokens} 토큰 (예산 {prompt_mod.PROMPT_TOKEN_BUDGET})")

    # ★ 우리 몫만 세면 안 된다. OpenClaw 골격이 프롬프트의 절반 가까이를 차지하고,
    #   그것까지 합친 값이 실제 첫 턴 지연을 정한다. 게이트웨이 트레이스에서 잰
    #   실측 첫 호출이 5,212 토큰이었고 우리 몫 추정이 3,826 이라, 차액이 골격이다.
    #   `estimate_tokens` 가 실제보다 크게 잡으므로 이 골격 값은 보수적인 하한이다.
    ours = report["total"] + prompt_tokens
    total = ours + catalog_mod.FRAMEWORK_TOKENS
    print(f"우리 몫 {ours} 토큰 + OpenClaw 골격 약 {catalog_mod.FRAMEWORK_TOKENS} = 약 {total}")
    # 295 tok/s 는 이 기기의 프롬프트 처리 실측이다 (`operate/notes/agent-gateway.md §3`).
    print(f"→ 첫 호출 프롬프트 처리 약 {total / 295:.1f}초 (295 tok/s 실측 기준)")
    print("참고: OpenClaw 기본 구성은 12,541 토큰 = 42.5초, 첫 턴부터 압축이 걸렸다")

    over = []
    if report["total"] > report["budget"]:
        over.append("툴")
    if prompt_tokens > prompt_mod.PROMPT_TOKEN_BUDGET:
        over.append("프롬프트")
    if over:
        print(f"예산 초과: {', '.join(over)}", file=sys.stderr)
        return 1
    return 0


def cmd_agent_prompt(args: argparse.Namespace, cfg: Config) -> int:
    """`AGENTS.md` 를 만들어 워크스페이스에 쓴다. 게이트웨이가 그걸 읽는다."""
    from lifetrainer.agent import prompt as prompt_mod
    from lifetrainer.agent.config import build_sandbox, load_settings

    settings = load_settings(cfg)
    body = prompt_mod.build_agents_md(build_sandbox(cfg, settings))
    if getattr(args, "print_only", False):
        print(body)
        return 0

    workspace = Path(settings.workspace)
    if not workspace.is_absolute():
        workspace = cfg.root / workspace
    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / prompt_mod.WORKSPACE_FILE
    target.write_text(body, encoding="utf-8")
    print(f"{target} 에 썼습니다 ({len(body)}자)")

    if not getattr(args, "keep_seeded", False):
        stubbed = []
        for name in prompt_mod.SEEDED_FILES:
            path = workspace / name
            # 없어도 만든다 — 있으면 게이트웨이가 시드를 안 돌린다 (prompt.py 주석).
            if not path.is_file() or path.read_text(encoding="utf-8") != prompt_mod.STUB_BODY:
                path.write_text(prompt_mod.STUB_BODY, encoding="utf-8")
                stubbed.append(name)
        boot = workspace / prompt_mod.BOOTSTRAP_FILE
        if boot.is_file():
            boot.unlink()
            stubbed.append(prompt_mod.BOOTSTRAP_FILE + "(삭제)")
        if stubbed:
            print(f"범용 인격 파일 {len(stubbed)}개 비움: {', '.join(stubbed)}")
    return 0


def cmd_agent_call(args: argparse.Namespace, cfg: Config) -> int:
    """툴 하나를 직접 부른다. 게이트웨이·모델 없이 몸통만 확인할 때 쓴다."""
    from lifetrainer.agent import catalog as catalog_mod
    from lifetrainer.agent.config import build_sandbox

    tools = {t.name: t for t in catalog_mod.build_tools()}
    tool = tools.get(args.tool)
    if tool is None:
        raise CliError(f"'{args.tool}' 이라는 툴은 없습니다. 있는 것: {', '.join(sorted(tools))}")

    call_args: dict = {}
    for item in args.args:
        key, sep, value = item.partition("=")
        if not sep:
            raise CliError(f"인자는 key=value 모양이어야 합니다: {item!r}")
        call_args[key] = value

    ctx = catalog_mod.AgentContext(cfg=cfg, sandbox=build_sandbox(cfg), actor="cli")
    print(tool.handler(ctx, call_args))
    return 0


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
