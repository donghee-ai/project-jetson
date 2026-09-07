"""리포트용 타임라인 PNG — 144칸 가로 띠 하나 = 하루, 7줄 = 한 주.

`docs/life-trainer-design.md` §8.2 를 그대로 따른다. 모바일 Slack 에서
스크롤 없이 한눈에 읽히는 것이 목표라 여백을 최소화하고 가로 폭을 1200px
안팎으로 고정한다. 헤드리스 서버라 GUI 백엔드를 시도하면 그 자체로 실패하므로
matplotlib import 직후 Agg 백엔드를 강제한다.

숫자(슬롯별 카테고리·활동 시간)는 전부 `report.stats.compute_daily` 가 SQL 로
계산한 값을 그대로 그린다 — 이 모듈은 다시 집계하지 않고 그리기만 한다.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import matplotlib

matplotlib.use("Agg")  # 헤드리스 서버 — pyplot import 전에 반드시 먼저 고정한다.

import matplotlib.pyplot as plt  # noqa: E402 - Agg 고정 뒤에 import 해야 한다.
from matplotlib import font_manager  # noqa: E402
from matplotlib.axes import Axes  # noqa: E402
from matplotlib.patches import Patch, Rectangle  # noqa: E402

from lifetrainer import timeutil  # noqa: E402
from lifetrainer.report.stats import DailyStats, compute_daily, format_hm  # noqa: E402

if TYPE_CHECKING:
    import sqlite3

    from lifetrainer.config import Config
    from lifetrainer.rollup.classify import Classifier

logger = logging.getLogger(__name__)

# 한글 폰트 폴백 순서. cfg.report.font_family 를 가장 먼저 시도한 뒤 이 순서로 내려간다.
_FONT_FALLBACKS: tuple[str, ...] = ("NanumGothic", "Noto Sans CJK KR", "DejaVu Sans")

_FIG_WIDTH_IN = 8.0  # 150dpi 기준 1200px
_DPI = 150
_WEEKDAY_KR = ("월", "화", "수", "목", "금", "토", "일")


# ── 폰트 ──────────────────────────────────────────────────────────────


def _available_font_names() -> set[str]:
    """matplotlib 이 실제로 스캔해 알고 있는 폰트 패밀리 이름 집합."""
    return {f.name for f in font_manager.fontManager.ttflist}


def _resolve_font(cfg: "Config") -> str:
    """cfg.report.font_family -> NanumGothic -> Noto Sans CJK KR -> DejaVu Sans 순 폴백.

    실제로 시스템에 설치되어 matplotlib font_manager 가 찾을 수 있는 폰트만 고른다.
    아무것도 못 찾으면 DejaVu Sans 로 떨어지며 경고를 남긴다 — 이 경우 한글은
    두부(□)로 깨진다.
    """
    available = _available_font_names()
    candidates = [cfg.report.font_family, *_FONT_FALLBACKS]
    for name in candidates:
        if name in available:
            return name
    logger.warning(
        "한글 폰트를 찾지 못했습니다 (시도: %s). DejaVu Sans 로 대체하지만 "
        "한글이 두부(□)로 깨질 수 있습니다.",
        candidates,
    )
    return "DejaVu Sans"


def _apply_font(cfg: "Config") -> None:
    family = _resolve_font(cfg)
    plt.rcParams["font.family"] = family
    plt.rcParams["axes.unicode_minus"] = False  # 마이너스 기호(−)가 두부로 깨지는 것 방지


# ── 그리기 헬퍼 ────────────────────────────────────────────────────────


def _hour_ticks(cfg: "Config") -> tuple[list[float], list[str]]:
    """3시간 간격 눈금의 슬롯 위치와 라벨.

    슬롯 0 은 이제 자정이 아니라 `cfg.rollup.day_boundary_hour`(기본 06:00)다 —
    `rollup_day` 가 그 경계로 슬롯을 채우므로(V1), 눈금 라벨도 같이 회전시켜야
    "슬롯 0 = 00" 처럼 실제와 어긋난 라벨을 그리지 않는다. 기본값 기준
    06,09,12,15,18,21,00,03,06 순서가 된다.
    """
    slots_per_hour = 60 / cfg.rollup.slot_minutes
    boundary = cfg.rollup.day_boundary_hour
    positions = [h * slots_per_hour for h in range(0, 25, 3)]
    labels = [f"{(boundary + h) % 24:02d}" for h in range(0, 25, 3)]
    return positions, labels


def _draw_band(ax: Axes, categories: list[str], classifier: "Classifier", y0: float, height: float) -> None:
    """카테고리 리스트(길이 = 슬롯 수)를 폭 1짜리 칸으로 이어붙여 가로 띠로 그린다."""
    for i, cat in enumerate(categories):
        color = classifier.color(cat)
        ax.add_patch(Rectangle((i, y0), 1.0, height, facecolor=color, edgecolor="none"))


def _legend_handles(categories_present: list[str], classifier: "Classifier") -> list[Patch]:
    return [Patch(facecolor=classifier.color(c), label=classifier.label(c)) for c in categories_present]


def _ordered_present(all_categories: set[str], classifier: "Classifier") -> list[str]:
    """실제로 등장한 카테고리만, classifier.order() 의 안정적인 순서로."""
    try:
        order = classifier.order()
    except Exception:  # noqa: BLE001 - 순서 조회가 실패해도 범례는 나와야 한다
        order = sorted(all_categories)
    present = [c for c in order if c in all_categories]
    # order() 에 없는 카테고리(예: 방어적으로 들어온 미분류 값)도 놓치지 않는다.
    present += sorted(all_categories - set(present))
    return present


def _default_out_path(cfg: "Config", filename: str) -> Path:
    return cfg.report.png_dir / filename


def _save(fig: "plt.Figure", out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=_DPI, bbox_inches="tight")
    plt.close(fig)
    return out


# ── 공개 API ──────────────────────────────────────────────────────────


def render_day(
    conn: "sqlite3.Connection",
    cfg: "Config",
    classifier: "Classifier",
    day: str,
    *,
    out_path: Path | None = None,
) -> Path:
    """하루를 144칸(기본) 가로 띠 1줄로 그린 PNG 를 만들고 경로를 반환한다."""
    _apply_font(cfg)
    stats: DailyStats = compute_daily(conn, cfg, day)
    n_slots = len(stats.slot_categories)

    fig, ax = plt.subplots(figsize=(_FIG_WIDTH_IN, 2.2), dpi=_DPI)

    _draw_band(ax, stats.slot_categories, classifier, y0=0.0, height=1.0)

    ax.set_xlim(0, max(n_slots, 1))
    ax.set_ylim(0, 1)
    ax.set_yticks([])
    ticks, labels = _hour_ticks(cfg)
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels, fontsize=9)
    ax.tick_params(axis="x", length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)

    present = _ordered_present(set(stats.slot_categories), classifier)
    handles = _legend_handles(present, classifier)
    if handles:
        # fig.legend (axes 가 아니라 figure 기준) 을 쓰면 축 좌표계 스케일과 무관하게
        # 항상 그림 하단에 고정된 간격으로 붙는다 — bbox_to_anchor 를 축 fraction 으로
        # 주면 줄 수가 늘어날 때 간격이 같이 벌어져 PNG 하단에 빈 여백이 커지는 문제가 있었다.
        fig.legend(
            handles=handles,
            loc="lower center",
            ncol=min(len(handles), 6),
            frameon=False,
            fontsize=9,
            handlelength=1.2,
            columnspacing=1.0,
        )

    ax.set_title(f"{day} · 활동 {format_hm(stats.active_sec)}", fontsize=13, pad=10)
    fig.subplots_adjust(bottom=0.28)

    out = out_path or _default_out_path(cfg, f"{day}-timeline.png")
    return _save(fig, out)


def render_week(
    conn: "sqlite3.Connection",
    cfg: "Config",
    classifier: "Classifier",
    end_day: str,
    *,
    out_path: Path | None = None,
) -> Path:
    """end_day 를 포함한 최근 7일을 7줄(위=오래된 날, 아래=end_day)로 그린 PNG."""
    _apply_font(cfg)

    end_date = date.fromisoformat(end_day)
    start_date = end_date - timedelta(days=6)
    days = timeutil.day_range(start_date.isoformat(), end_day)  # 오래된 날 -> 최근 날 순

    day_stats = [compute_daily(conn, cfg, d) for d in days]
    n_slots = cfg.slots_per_day
    n_rows = len(days)

    row_h = 0.8
    row_gap = 0.35
    row_stride = row_h + row_gap
    fig_h = max(n_rows, 1) * row_stride + 1.2
    fig, ax = plt.subplots(figsize=(_FIG_WIDTH_IN, fig_h), dpi=_DPI)

    present_all: set[str] = set()
    # strict: 날짜와 그날 통계가 어긋나면 그림 전체가 하루씩 밀린다.
    for row_idx, (d, st) in enumerate(zip(days, day_stats, strict=True)):
        # 위가 오래된 날이 되도록: row_idx 0(가장 오래됨)이 가장 큰 y0(=맨 위)을 갖는다.
        y0 = (n_rows - 1 - row_idx) * row_stride
        _draw_band(ax, st.slot_categories, classifier, y0=y0, height=row_h)
        present_all.update(st.slot_categories)

        dt = date.fromisoformat(d)
        weekday = _WEEKDAY_KR[dt.weekday()]
        marker = " (오늘)" if d == end_day else ""
        label = f"{d[5:]} ({weekday}){marker}  {format_hm(st.active_sec)}"
        ax.text(-1.5, y0 + row_h / 2, label, ha="right", va="center", fontsize=9)

    ax.set_xlim(0, max(n_slots, 1))
    ax.set_ylim(0, max(n_rows, 1) * row_stride)
    ax.set_yticks([])
    ticks, labels = _hour_ticks(cfg)
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels, fontsize=9)
    ax.tick_params(axis="x", length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)

    # 3시간 눈금마다 옅은 세로선을 그어 요일 간 시각 정렬(=지난주 비교)이 눈으로 되게 한다.
    for x in ticks:
        ax.axvline(x, color="white", linewidth=0.6, alpha=0.5, zorder=3)

    present = _ordered_present(present_all, classifier)
    handles = _legend_handles(present, classifier)
    if handles:
        # render_day 와 마찬가지로 fig.legend 를 써서 줄 수(n_rows)와 무관하게
        # 하단 여백이 일정하게 유지되도록 한다.
        fig.legend(
            handles=handles,
            loc="lower center",
            ncol=min(len(handles), 6),
            frameon=False,
            fontsize=9,
            handlelength=1.2,
            columnspacing=1.0,
        )

    ax.set_title(f"{start_date.isoformat()} ~ {end_day} 주간 타임라인", fontsize=13, pad=10)
    # 위/아래 고정 여백(제목·범례)을 절대 인치 기준으로 확보한다 — 축 fraction 으로
    # 잡으면 fig_h 가 줄 수에 비례해 커질 때 여백도 같이 커져 하단이 텅 비어 보인다.
    top_margin_in = 0.55
    bottom_margin_in = 0.55
    fig.subplots_adjust(
        top=1 - top_margin_in / fig_h,
        bottom=bottom_margin_in / fig_h,
    )

    out = out_path or _default_out_path(cfg, f"{end_day}-week-timeline.png")
    return _save(fig, out)
