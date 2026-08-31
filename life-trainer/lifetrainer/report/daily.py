"""일일/주간 리포트 조립 — stats(C) 와 blocks(D) 를 엮는 배선 계층 (담당 H).

`report/stats.py` 가 SQL 로 계산한 숫자를 `slackio.blocks` 의 빌더로 Block Kit
카드로 만들고, `render_*_text` 로 평문 폴백을 함께 만든 뒤 `report` 테이블에
`(kind, day)` UNIQUE 로 upsert 한다. PNG 는 `report.timeline` 이 그린다.

LLM 은 이 단계에 전혀 없다 (Phase 1 원칙). PNG 생성이 실패해도(폰트/디스크 등
일시적 문제) 텍스트/Block Kit 리포트 자체는 살아남아야 하므로, 여기서만 예외를
잡아 로그로 남기고 `png_path=None` 으로 계속 진행한다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from lifetrainer import db, timeutil
from lifetrainer.report import stats as stats_mod
from lifetrainer.report import timeline as timeline_mod
from lifetrainer.slackio import blocks as blocks_mod

if TYPE_CHECKING:  # pragma: no cover - 타입 힌트 전용
    import sqlite3

    from lifetrainer.config import Config
    from lifetrainer.rollup.classify import Classifier

logger = logging.getLogger(__name__)


@dataclass
class BuiltReport:
    """조립이 끝난 리포트 하나. `report` 테이블의 한 행에 대응한다."""

    kind: str
    day: str
    text: str
    blocks: list[dict]
    png_path: Path | None
    report_id: int


def _upsert_report(
    conn: "sqlite3.Connection",
    kind: str,
    day: str,
    text: str,
    blocks: list[dict],
    png_path: Path | None,
) -> int:
    """`report` 테이블에 `(kind, day)` 로 upsert 하고 해당 행의 id 를 반환한다."""
    now = timeutil.now_ts()
    blocks_json = json.dumps(blocks, ensure_ascii=False)
    png_str = str(png_path) if png_path is not None else None
    with db.transaction(conn):
        conn.execute(
            """
            INSERT INTO report(kind, day, text, blocks_json, png_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(kind, day) DO UPDATE SET
                text = excluded.text,
                blocks_json = excluded.blocks_json,
                png_path = excluded.png_path,
                created_at = excluded.created_at
            """,
            (kind, day, text, blocks_json, png_str, now),
        )
    row = conn.execute(
        "SELECT id FROM report WHERE kind = ? AND day = ?", (kind, day)
    ).fetchone()
    return int(row["id"])


def build_daily(
    conn: "sqlite3.Connection", cfg: "Config", classifier: "Classifier", day: str
) -> BuiltReport:
    """하루치 리포트를 조립한다: `stats.compute_daily` -> 텍스트/Block Kit -> PNG -> upsert."""
    day_stats = stats_mod.compute_daily(conn, cfg, day)
    text = stats_mod.render_daily_text(day_stats, classifier)
    blocks = blocks_mod.daily_report_blocks(day_stats, classifier)

    png_path: Path | None
    try:
        png_path = timeline_mod.render_day(conn, cfg, classifier, day)
    except Exception:  # noqa: BLE001 - PNG 실패해도 텍스트 리포트는 살아남아야 한다
        logger.exception("일일 타임라인 PNG 생성 실패 (day=%s)", day)
        png_path = None

    report_id = _upsert_report(conn, "daily", day, text, blocks, png_path)
    return BuiltReport(
        kind="daily", day=day, text=text, blocks=blocks, png_path=png_path, report_id=report_id
    )


def build_weekly(
    conn: "sqlite3.Connection", cfg: "Config", classifier: "Classifier", end_day: str
) -> BuiltReport:
    """주간 리포트를 조립한다: `stats.compute_weekly` -> 텍스트/Block Kit -> PNG -> upsert."""
    week_stats = stats_mod.compute_weekly(conn, cfg, end_day)
    text = stats_mod.render_weekly_text(week_stats, classifier)
    blocks = blocks_mod.weekly_report_blocks(week_stats, classifier)

    png_path: Path | None
    try:
        png_path = timeline_mod.render_week(conn, cfg, classifier, end_day)
    except Exception:  # noqa: BLE001
        logger.exception("주간 타임라인 PNG 생성 실패 (end_day=%s)", end_day)
        png_path = None

    report_id = _upsert_report(conn, "weekly", end_day, text, blocks, png_path)
    return BuiltReport(
        kind="weekly", day=end_day, text=text, blocks=blocks, png_path=png_path, report_id=report_id
    )
