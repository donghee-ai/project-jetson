"""플래너 PNG — 하루치 종이 플래너를 재현한다 (계약서 §5).

`docs/reference/planner_example.png` 이 재현 대상 레이아웃이다.
좌측에는 사람이 선언한 계획(체크박스·시간·달성률), 우측에는 기계가 계측한
실제 하루가 24행(시간) × 6칸(10분) 격자로 앉는다. 계획은 격자 위에 **테두리**
로만 겹친다 — 채우면 실제와 계획이 구분되지 않는다.

`report/timeline.py` 는 건드리지 않는다 (주간 가로 띠는 그대로 유지). 이 모듈은
하루 단위 새 레이아웃이고, 숫자는 전부 `report.stats.compute_daily` 와
`plan.achieve` 가 SQL 로 이미 계산해 둔 값을 그대로 가져다 그리기만 한다.

색은 전부 `report.palette.load_palette` 경유 — 여기서도 색을 하드코딩하지 않는다.
이 팔레트는 `adjacent` 검증만 통과하고 `all-pairs` 는 실패하므로(research↔ops,
research↔entertainment 가 색각 이상에서 겹친다), 색만으로 카테고리 정체를
전달하면 안 된다. 그래서 이 모듈은 반드시:
  1. 그날 등장한 카테고리의 범례를 그린다
  2. 3칸(30분) 이상 연속된 블록에는 카테고리 이름을 격자 위에 직접 적는다
이 두 가지를 뺀 채로는 절대 "완료"가 아니다.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import matplotlib

matplotlib.use("Agg")  # 헤드리스 서버 — pyplot import 전에 반드시 먼저 고정한다.

import matplotlib.pyplot as plt  # noqa: E402 - Agg 고정 뒤에 import 해야 한다.
from matplotlib import font_manager, patheffects  # noqa: E402
from matplotlib.axes import Axes  # noqa: E402
from matplotlib.patches import Patch, Rectangle  # noqa: E402

from lifetrainer.plan.achieve import day_achievement, plans_for_day  # noqa: E402
from lifetrainer.report.palette import Palette, load_palette  # noqa: E402
from lifetrainer.report.stats import compute_daily, format_hm  # noqa: E402

if TYPE_CHECKING:
    import sqlite3

    from lifetrainer.config import Config
    from lifetrainer.plan.models import PlanInstance

logger = logging.getLogger(__name__)

# 한글 폰트 폴백 순서 — timeline.py 와 동일한 방식을 그대로 따른다.
_FONT_FALLBACKS: tuple[str, ...] = ("NanumGothic", "Noto Sans CJK KR", "DejaVu Sans")

_WEEKDAY_KR = ("월", "화", "수", "목", "금", "토", "일")  # date.weekday() 0=월 순서

_DPI = 150
_FIG_WIDTH_IN = 6.8  # 150dpi 기준 1020px — 계약서 요구 폭(900~1100px) 안

# ── 캔버스 지오메트리(인치) — 모두 add_axes 로 직접 배치하기 위한 값들 ──────
_MARGIN_LEFT_IN = 0.35
_MARGIN_RIGHT_IN = 0.25
_MARGIN_TOP_IN = 0.25
_MARGIN_BOTTOM_IN = 0.30
_HEADER_H_IN = 1.15
_GAP_HEADER_BODY_IN = 0.24  # 헤더 구분선과 "계획" 제목이 겹치지 않도록 넉넉히 둔다
_GRID_ROW_H_IN = 0.30  # 24행 * 0.30 = 7.2in
_GAP_BODY_LEGEND_IN = 0.15
_LEGEND_H_IN = 0.55
_GAP_COLUMNS_IN = 0.22  # 계획 열 <-> 시각 라벨 사이
_ROW_LABEL_W_IN = 0.34  # 시각 라벨("06" 등) 열 폭
_LEFT_COL_FRAC = 0.42  # 본문 폭 중 계획 열이 차지하는 비율 (나머지는 시각 라벨 + 격자)

_GRID_ROWS = 24
_MIN_RUN_LEN_FOR_LABEL = 3  # 3칸(30분) 이상 연속되면 이름을 직접 적는다 (계약서 §2)
_MAX_PLAN_ROWS = 12  # 이보다 많으면 나머지는 "+N개 더"로 접는다
_TOP_UNCLASSIFIED = 3

# ── 계획 열 내부 지오메트리(인치, grid_h 대비 분수로 환산해서 쓴다) ────────
#
# 계획 카드 높이는 칸 수에 맞춰 늘어나면 안 된다 — 항목이 2개든 8개든
# 위에서부터 고정 높이로 차곡차곡 쌓고, 남는 아래 공간은 그냥 비워 둔다
# (레퍼런스 플래너의 TASKS 열과 같은 방식). 예전엔 남은 영역을 항목 수로
# 나눠 채웠는데, 항목이 적을 때 카드 사이에 거대한 빈 칸이 생기는 문제가 있었다.
_PLAN_TITLE_H_IN = 0.26
_PLAN_ITEM_H_IN = 0.58
_PLAN_BAR_H_IN = 0.045  # 달성률 막대 — 격자가 주인공이니 아주 얇게 고정한다
_UNCLASS_TITLE_H_IN = 0.20
_UNCLASS_ITEM_H_IN = 0.16
_PLAN_REGION_BOTTOM = 0.24  # 이 아래는 미분류 섹션 몫으로 남겨둔다 (좌측 열 y-fraction)
_UNCLASS_REGION_TOP = 0.20


# ── 폰트 (timeline.py 와 동일한 폴백 방식) ────────────────────────────────


def _available_font_names() -> set[str]:
    return {f.name for f in font_manager.fontManager.ttflist}


def _resolve_font(cfg: "Config") -> str:
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
    plt.rcParams["axes.unicode_minus"] = False


# ── 팔레트 ────────────────────────────────────────────────────────────


def _palette_path(cfg: "Config") -> Path:
    return cfg.root / "config" / "palette.yaml"


def _cell_color(category: str, pal: Palette) -> str:
    """슬롯 카테고리 -> 채울 색. 활동 카테고리가 아니면 구조색, 그마저 없으면 방어적 폴백."""
    if category in pal.categories:
        return pal.categories[category]
    if category in pal.structural:
        return pal.structural[category]
    return pal.ink["muted"]


# ── 격자 좌표 변환 ────────────────────────────────────────────────────


def _slot_to_rowcol(slot_idx: int, cols_per_row: int, grid_start_hour: int) -> tuple[int, int]:
    """논리적 하루 슬롯 인덱스(0..143) -> 격자 표시상의 (행, 열).

    ★ **슬롯 0 은 자정이 아니라 `grid_start_hour`(기본 06:00)다.** 그래서 행 번호는
    슬롯을 나눈 몫 그 자체이며, `grid_start_hour` 를 빼면 안 된다 — 빼면 경계가
    두 번 적용돼 6시간(36슬롯)만큼 통째로 밀린다. `grid_start_hour` 는 행 **라벨**
    을 만들 때만 쓴다(`_draw_row_labels`).

    이 함수는 한때 슬롯을 자정 기준으로 오해했고, 그 탓에 밤 활동이 낮 칸에
    그려졌다 (`HISTORY/2026-08-18-planner-row-offset.md`). 같은 실수를 웹은
    `web/app.py` 주석으로 이미 막아두고 있었다.
    """
    col = slot_idx % cols_per_row
    row = (slot_idx // cols_per_row) % _GRID_ROWS
    return row, col


def _row_segments(
    start_slot: int, end_slot: int, cols_per_row: int, grid_start_hour: int
) -> list[tuple[int, int, int]]:
    """[start_slot, end_slot) 구간을 격자의 (행, 시작열, 끝열) 조각들로 나눈다.

    계획은 자정을 넘지 않지만 여러 시간(=여러 행)에 걸칠 수 있으므로, 행 경계마다
    끊어서 각 행에 그릴 사각형 하나씩을 만든다.
    """
    segments: list[tuple[int, int, int]] = []
    i = start_slot
    while i < end_slot:
        # `_slot_to_rowcol` 과 같은 규칙 — 슬롯 0 이 곧 grid_start_hour 라 빼지 않는다.
        row_index = i // cols_per_row
        row_start_slot = row_index * cols_per_row
        row_end_slot = row_start_slot + cols_per_row
        seg_end = min(end_slot, row_end_slot)
        row = row_index % _GRID_ROWS
        segments.append((row, i - row_start_slot, seg_end - row_start_slot))
        i = seg_end
    return segments


def _runs(categories: list[str]) -> list[tuple[int, int, str]]:
    """카테고리 리스트를 (시작 인덱스, 길이, 카테고리)의 연속 구간 목록으로."""
    if not categories:
        return []
    runs: list[tuple[int, int, str]] = []
    start = 0
    for i in range(1, len(categories) + 1):
        if i == len(categories) or categories[i] != categories[start]:
            runs.append((start, i - start, categories[start]))
            start = i
    return runs


# ── 텍스트 헬퍼 ───────────────────────────────────────────────────────


def _fmt_minutes(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _top_unclassified(conn: "sqlite3.Connection", day: str, limit: int) -> list[tuple[str, float]]:
    """그날 미분류로 떨어진 지문 중 시간이 큰 순 상위 N개. (app 없으면 지문 자체를 라벨로)."""
    rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(u.app, ''), ud.fingerprint) AS label,
               SUM(ud.seconds) AS sec
        FROM unclassified_day ud
        LEFT JOIN unclassified u ON u.fingerprint = ud.fingerprint
        WHERE ud.day = ?
        GROUP BY ud.fingerprint
        ORDER BY sec DESC
        LIMIT ?
        """,
        (day, limit),
    ).fetchall()
    return [(str(r["label"]), float(r["sec"])) for r in rows]


# ── 헤더 ─────────────────────────────────────────────────────────────


def _draw_header(
    ax: Axes, day: str, active_sec: float, overall: float, achieved: int, total: int, coverage: float, pal: Palette
) -> None:
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    dt = date.fromisoformat(day)
    weekday = _WEEKDAY_KR[dt.weekday()]
    ax.text(
        0.0, 0.62, f"{day} ({weekday})", ha="left", va="center", fontsize=17, fontweight="bold", color=pal.ink["primary"]
    )

    achieve_text = f"달성 {achieved}/{total}" if total > 0 else "계획 없음"
    stat_line = f"활동 {format_hm(active_sec)}  ·  달성률 {overall * 100:.0f}% ({achieve_text})  ·  커버리지 {coverage * 100:.0f}%"
    ax.text(0.0, 0.18, stat_line, ha="left", va="center", fontsize=9.5, color=pal.ink["secondary"])

    ax.axhline(0.0, color=pal.ink["baseline"], linewidth=1.0)


# ── 계획 목록(좌측) ──────────────────────────────────────────────────


def _draw_checkbox(ax: Axes, x: float, y: float, checked: bool, pal: Palette) -> None:
    if checked:
        ax.plot(
            x, y, marker="s", markersize=7, markerfacecolor=pal.plan["achieved"],
            markeredgecolor=pal.plan["achieved"], markeredgewidth=1.0, clip_on=False,
        )
    else:
        ax.plot(
            x, y, marker="s", markersize=7, markerfacecolor="none",
            markeredgecolor=pal.ink["secondary"], markeredgewidth=1.0, clip_on=False,
        )


def _draw_plan_item(ax: Axes, y_top: float, item_h: float, bar_h: float, inst: "PlanInstance", pal: Palette) -> None:
    """카드 하나를 `y_top` 에서 시작해 고정 높이 `item_h` 안에 그린다.

    `item_h` 는 카드 간 간격을 포함한 "몫"이고, 실제 내용(체크박스·제목·시간·막대)은
    그 위쪽 일부만 차지한다 — 아래쪽 여백이 곧 다음 카드와의 간격이다.
    막대 두께(`bar_h`)는 `item_h` 와 무관하게 고정값을 받는다: 항목이 적어 `item_h`
    가 커져도 막대만 따라 굵어지면 안 된다(격자가 주인공, 계획 열은 참조 정보).
    """
    y_center = y_top - item_h * 0.18
    _draw_checkbox(ax, 0.025, y_center, inst.checked, pal)

    title = _truncate(inst.plan.title, 12)
    time_range = f"{_fmt_minutes(inst.plan.start_min)}-{_fmt_minutes(inst.plan.end_min)}"
    pct = inst.achievement * 100
    # 달성(100%)이면 achieved 색, 아니면 missed 색 — 둘 다 palette.yaml 의 plan_overlay 값.
    pct_color = pal.plan["achieved"] if inst.achievement >= 1.0 else pal.plan["missed"]

    ax.text(0.09, y_center, title, ha="left", va="center", fontsize=8.3, color=pal.ink["primary"])
    time_pct_y = y_top - item_h * 0.42
    ax.text(0.09, time_pct_y, time_range, ha="left", va="center", fontsize=6.8, color=pal.ink["secondary"])
    ax.text(0.99, time_pct_y, f"{pct:.0f}%", ha="right", va="center", fontsize=6.8, color=pct_color)

    # 달성률 막대 — 얇은 트랙(ink.gridline) + 채움(achieved/missed). 고정 두께.
    bar_y = y_top - item_h * 0.56
    bar_x0, bar_w = 0.09, 0.72
    ax.add_patch(Rectangle((bar_x0, bar_y), bar_w, bar_h, facecolor=pal.ink["gridline"], edgecolor="none"))
    fill_w = bar_w * min(1.0, inst.achievement)
    if fill_w > 0:
        ax.add_patch(Rectangle((bar_x0, bar_y), fill_w, bar_h, facecolor=pct_color, edgecolor="none"))


def _draw_plan_list(
    ax: Axes,
    instances: list["PlanInstance"],
    top_unclassified: list[tuple[str, float]],
    pal: Palette,
    grid_h_in: float,
) -> None:
    """좌측 계획 열. 카드는 항목 수와 무관하게 고정 높이로 위에서부터 쌓는다.

    `grid_h_in` 은 이 축(axes)의 실제 물리 높이(인치) — 인치 단위 상수를
    이 축의 0..1 좌표계 분수로 환산하는 데 쓴다. 그래야 카드 높이가
    "항목이 몇 개인가" 가 아니라 "인쇄됐을 때 몇 mm인가" 로 고정된다.
    """
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    def frac(inches: float) -> float:
        return inches / grid_h_in

    title_h = frac(_PLAN_TITLE_H_IN)
    item_h = frac(_PLAN_ITEM_H_IN)
    bar_h = frac(_PLAN_BAR_H_IN)
    unclass_title_h = frac(_UNCLASS_TITLE_H_IN)
    unclass_item_h = frac(_UNCLASS_ITEM_H_IN)

    # "계획" 제목은 축 안쪽(y<=1.0)에서 아래로 그린다 — 예전엔 y=1.02 에 위로
    # 자라는 텍스트를 그려서 헤더 구분선과 겹쳤다.
    ax.text(0.0, 1.0, "계획", ha="left", va="top", fontsize=10, fontweight="bold", color=pal.ink["primary"])
    cursor = 1.0 - title_h

    if not instances:
        ax.text(
            0.0, cursor - item_h * 0.5, "계획 없음 — lt plan add 로 추가",
            ha="left", va="center", fontsize=8.5, color=pal.ink["muted"],
        )
    else:
        avail = max(0.0, cursor - _PLAN_REGION_BOTTOM)
        max_slots = max(1, int(avail // item_h))
        max_slots = min(max_slots, _MAX_PLAN_ROWS)
        if len(instances) <= max_slots:
            shown, overflow = instances, 0
        else:
            # 한 줄은 "…외 N개" 안내용으로 남겨둔다.
            shown, overflow = instances[: max(0, max_slots - 1)], 0
            overflow = len(instances) - len(shown)
        for i, inst in enumerate(shown):
            y_top = cursor - i * item_h
            _draw_plan_item(ax, y_top, item_h, bar_h, inst, pal)
        if overflow > 0:
            y_top = cursor - len(shown) * item_h
            ax.text(
                0.0, y_top - item_h * 0.35, f"…외 {overflow}개 더", ha="left", va="center",
                fontsize=7.5, color=pal.ink["muted"],
            )

    if top_unclassified:
        ax.text(
            0.0, _UNCLASS_REGION_TOP, "미분류 상위", ha="left", va="top", fontsize=8, fontweight="bold",
            color=pal.ink["secondary"],
        )
        unclass_cursor = _UNCLASS_REGION_TOP - unclass_title_h
        for i, (label, sec) in enumerate(top_unclassified):
            y_top = unclass_cursor - i * unclass_item_h
            ax.text(
                0.0, y_top - unclass_item_h * 0.5, f"{_truncate(label, 16)}  {format_hm(sec)}",
                ha="left", va="center", fontsize=7.2, color=pal.ink["muted"],
            )


# ── 격자(우측) ────────────────────────────────────────────────────────


def _draw_row_labels(ax: Axes, grid_start_hour: int, pal: Palette) -> None:
    ax.set_xlim(0, 1)
    ax.set_ylim(0, _GRID_ROWS)
    ax.invert_yaxis()
    ax.axis("off")
    for row in range(_GRID_ROWS):
        hour = (grid_start_hour + row) % 24
        ax.text(0.85, row + 0.5, f"{hour:02d}", ha="right", va="center", fontsize=7.5, color=pal.ink["secondary"])


def _draw_grid(
    ax: Axes,
    slot_categories: list[str],
    plan_instances: list["PlanInstance"],
    cols_per_row: int,
    grid_start_hour: int,
    pal: Palette,
) -> None:
    ax.set_xlim(0, cols_per_row)
    ax.set_ylim(0, _GRID_ROWS)
    ax.invert_yaxis()
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    # 슬롯(자정 기준 0..143)을 격자 행렬로 재배치한다.
    rows_categories: list[list[str]] = [["off"] * cols_per_row for _ in range(_GRID_ROWS)]
    for slot_idx, cat in enumerate(slot_categories):
        row, col = _slot_to_rowcol(slot_idx, cols_per_row, grid_start_hour)
        if 0 <= row < _GRID_ROWS and 0 <= col < cols_per_row:
            rows_categories[row][col] = cat

    # 1) 실제(채움) — 칸마다 얇은 테두리
    for row in range(_GRID_ROWS):
        for col in range(cols_per_row):
            color = _cell_color(rows_categories[row][col], pal)
            ax.add_patch(
                Rectangle((col, row), 1.0, 1.0, facecolor=color, edgecolor=pal.ink["gridline"], linewidth=0.4)
            )

    # 2) 정시 경계 — 진한 가로선 (매 행 = 매 시간 경계)
    for row in range(_GRID_ROWS + 1):
        ax.axhline(row, color=pal.ink["baseline"], linewidth=1.1, zorder=2)
    ax.axvline(0, color=pal.ink["baseline"], linewidth=1.1, zorder=2)
    ax.axvline(cols_per_row, color=pal.ink["baseline"], linewidth=1.1, zorder=2)

    # 3) 계획(테두리만) — 실제 위에 옅은 wash + 테두리로 겹친다. 채우지 않는다.
    #    반드시 정시 경계선(2번)보다 나중에, 위 zorder 로 그려야 한다 — 계획이 정각에
    #    딱 맞춰 시작/끝나는 경우(흔함)가 많아서, 먼저 그리면 테두리가 정시 경계선에
    #    완전히 덮여 안 보이는 문제가 있었다.
    stroke = pal.plan["stroke"]
    stroke_alpha = float(pal.plan["stroke_alpha"])
    wash_alpha = float(pal.plan["wash_alpha"])
    for inst in plan_instances:
        for row, col_start, col_end in _row_segments(inst.start_slot, inst.end_slot, cols_per_row, grid_start_hour):
            width = col_end - col_start
            ax.add_patch(
                Rectangle((col_start, row), width, 1.0, facecolor=stroke, alpha=wash_alpha, edgecolor="none", zorder=3)
            )
            ax.add_patch(
                Rectangle(
                    (col_start, row), width, 1.0, facecolor="none", edgecolor=stroke, alpha=stroke_alpha,
                    linewidth=1.5, zorder=4,
                )
            )

    # 4) 2차 인코딩: 3칸(30분) 이상 연속 블록에 카테고리 이름을 직접 적는다.
    #    구조 카테고리(off/away/unknown)는 색각 혼동 대상이 아니므로 라벨을 붙이지 않는다.
    stroke_for_text = pal.surface
    for row in range(_GRID_ROWS):
        for start, length, cat in _runs(rows_categories[row]):
            if length < _MIN_RUN_LEN_FOR_LABEL or cat not in pal.categories:
                continue
            label = pal.labels.get(cat, cat)
            ax.text(
                start + length / 2, row + 0.5, label, ha="center", va="center", fontsize=6.6,
                color=pal.ink["primary"], zorder=6,
                path_effects=[patheffects.withStroke(linewidth=2.2, foreground=stroke_for_text)],
            )


# ── 범례 ─────────────────────────────────────────────────────────────


def _legend_handles(slot_categories: list[str], pal: Palette) -> list[Patch]:
    present = set(slot_categories) & set(pal.categories)
    ordered = [c for c in pal.order if c in present]
    return [Patch(facecolor=pal.categories[c], label=pal.labels.get(c, c)) for c in ordered]


# ── 저장 ─────────────────────────────────────────────────────────────


def _default_out_path(cfg: "Config", day: str, theme: str) -> Path:
    suffix = "-planner-dark.png" if theme == "dark" else "-planner.png"
    return cfg.report.png_dir / f"{day}{suffix}"


def _save(fig: "plt.Figure", out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=_DPI, bbox_inches="tight")
    plt.close(fig)
    return out


# ── 공개 API ──────────────────────────────────────────────────────────


def render_planner_day(
    conn: "sqlite3.Connection",
    cfg: "Config",
    day: str,
    *,
    out_path: Path | None = None,
    theme: str = "light",
) -> Path:
    """레퍼런스 플래너 레이아웃으로 하루치 PNG 를 그리고 경로를 반환한다.

    좌측에는 계획(체크박스·시간·달성률 막대), 우측에는 24행×6칸 격자(실제 =
    채움, 계획 = 테두리)를 그린다. 데이터가 전혀 없는 날에도 빈 격자를 그리고
    죽지 않는다 — `compute_daily`/`plans_for_day` 둘 다 빈 결과에 대해 방어적이다.
    """
    _apply_font(cfg)
    pal = load_palette(_palette_path(cfg), theme)

    stats = compute_daily(conn, cfg, day)
    instances = plans_for_day(conn, cfg, day)
    overall, achieved, total = day_achievement(conn, cfg, day)
    top_unclassified = _top_unclassified(conn, day, _TOP_UNCLASSIFIED)

    cols_per_row = max(1, 60 // cfg.rollup.slot_minutes)
    grid_start_hour = cfg.rollup.day_boundary_hour

    fig_w = _FIG_WIDTH_IN
    grid_h = _GRID_ROWS * _GRID_ROW_H_IN
    fig_h = (
        _MARGIN_TOP_IN
        + _HEADER_H_IN
        + _GAP_HEADER_BODY_IN
        + grid_h
        + _GAP_BODY_LEGEND_IN
        + _LEGEND_H_IN
        + _MARGIN_BOTTOM_IN
    )

    fig = plt.figure(figsize=(fig_w, fig_h), dpi=_DPI)
    fig.patch.set_facecolor(pal.surface)

    content_w = fig_w - _MARGIN_LEFT_IN - _MARGIN_RIGHT_IN
    left_col_w = content_w * _LEFT_COL_FRAC
    grid_w = content_w - left_col_w - _GAP_COLUMNS_IN - _ROW_LABEL_W_IN

    def frac_x(left_in: float, width_in: float) -> tuple[float, float]:
        return left_in / fig_w, width_in / fig_w

    def frac_y(bottom_in: float, height_in: float) -> tuple[float, float]:
        return bottom_in / fig_h, height_in / fig_h

    top_cursor = fig_h - _MARGIN_TOP_IN

    # 헤더
    header_bottom = top_cursor - _HEADER_H_IN
    hx, hw = frac_x(_MARGIN_LEFT_IN, content_w)
    hy, hh = frac_y(header_bottom, _HEADER_H_IN)
    header_ax = fig.add_axes([hx, hy, hw, hh])
    header_ax.set_facecolor(pal.surface)
    _draw_header(header_ax, day, stats.active_sec, overall, achieved, total, stats.coverage, pal)

    top_cursor = header_bottom - _GAP_HEADER_BODY_IN

    # 본문(계획 열 + 시각 라벨 열 + 격자 열) — 셋 다 같은 높이를 공유한다
    body_bottom = top_cursor - grid_h
    by, bh = frac_y(body_bottom, grid_h)

    lx, lw = frac_x(_MARGIN_LEFT_IN, left_col_w)
    left_ax = fig.add_axes([lx, by, lw, bh])
    left_ax.set_facecolor(pal.surface)
    _draw_plan_list(left_ax, instances, top_unclassified, pal, grid_h)

    rlx, rlw = frac_x(_MARGIN_LEFT_IN + left_col_w + _GAP_COLUMNS_IN, _ROW_LABEL_W_IN)
    row_label_ax = fig.add_axes([rlx, by, rlw, bh])
    row_label_ax.set_facecolor(pal.surface)
    _draw_row_labels(row_label_ax, grid_start_hour, pal)

    gx, gw = frac_x(_MARGIN_LEFT_IN + left_col_w + _GAP_COLUMNS_IN + _ROW_LABEL_W_IN, grid_w)
    grid_ax = fig.add_axes([gx, by, gw, bh])
    grid_ax.set_facecolor(pal.surface)
    _draw_grid(grid_ax, stats.slot_categories, instances, cols_per_row, grid_start_hour, pal)

    top_cursor = body_bottom - _GAP_BODY_LEGEND_IN

    # 범례 — 그날 등장한 카테고리만 (2차 인코딩 규칙 1번, 계약서 §2)
    legend_bottom = top_cursor - _LEGEND_H_IN
    legx, legw = frac_x(_MARGIN_LEFT_IN, content_w)
    legy, legh = frac_y(legend_bottom, _LEGEND_H_IN)
    legend_ax = fig.add_axes([legx, legy, legw, legh])
    legend_ax.set_facecolor(pal.surface)
    legend_ax.axis("off")
    handles = _legend_handles(stats.slot_categories, pal)
    if handles:
        legend_ax.legend(
            handles=handles, loc="center", ncol=min(len(handles), 8), frameon=False, fontsize=8,
            handlelength=1.1, columnspacing=1.0, labelcolor=pal.ink["secondary"],
        )

    out = out_path or _default_out_path(cfg, day, theme)
    return _save(fig, out)
