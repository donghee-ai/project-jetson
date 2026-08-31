#!/usr/bin/env python3
"""measure/results/ 에서 그림을 재생성한다.

★ 수동으로 고치지 않는다. `make figures` 가 이 스크립트를 부르고,
  숫자가 바뀌면 그림도 같이 바뀌어야 한다.

라벨은 영문이다 — 이 기기에 한글 폰트가 없어 matplotlib 이 두부(□)를 그린다.
그림은 저장소 밖에서도 읽히는 물건이므로 영문이 맞다.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANALYSIS = os.path.join(ROOT, "measure/results/processed/analysis.json")
RAW = os.path.join(ROOT, "measure/results/raw")

INK, GRID = "#0b0b0b", "#e1e0d9"
BLUE, RED, GREEN, MUTED = "#2a78d6", "#e34948", "#1baf7a", "#898781"
CAPTION = "single run, 2026-08-18 · Jetson Orin NX 16GB · MAXN"


def _style(ax, title, xlabel, ylabel):
    ax.set_title(title, fontsize=12, fontweight="bold", color=INK, pad=12)
    ax.set_xlabel(xlabel, fontsize=9, color=MUTED)
    ax.set_ylabel(ylabel, fontsize=9, color=MUTED)
    ax.grid(True, color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=8)


def _save(fig, out, name):
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, name)
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  ✅ {os.path.relpath(path, ROOT)}")


# ── 파서 ────────────────────────────────────────────────────────────────
# measure/results/*.depth.txt 는 고정폭 한글 ASCII 표다. 기계가 읽을 형식이 따로 없어
# 여기서 뜯는다. 행 모양: "  1      463      316.3/s     10.69/s    3.5s  ..."
_DEPTH_ROW = re.compile(r"^\s*\d+\s+([\d,]+)\s+([\d.]+)/s\s+([\d.]+)/s")


def parse_depth(path):
    """→ [(depth, prompt_tok_s, gen_tok_s), ...]"""
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = _DEPTH_ROW.match(line)
            if m:
                rows.append((int(m.group(1).replace(",", "")), float(m.group(2)), float(m.group(3))))
    return rows


_VDD = re.compile(r"VDD_IN (\d+)mW")


def mean_vdd_mw(path):
    vals = [int(m.group(1)) for m in (_VDD.search(l) for l in open(path, encoding="utf-8", errors="replace")) if m]
    return sum(vals) / len(vals) if vals else None


def load_analysis():
    with open(ANALYSIS, encoding="utf-8") as fh:
        return json.load(fh)


# ── 그림 ────────────────────────────────────────────────────────────────
def fig_depth_crossover(out):
    """② 어떤 모델이 적합한가 — 깊이에서 8B 가 30B 를 추월한다."""
    series = [
        ("Qwen3-8B Q4_K_M", "measure/results/Qwen3-8B-Q4KM.depth.txt", BLUE, "-"),
        ("Qwen3-30B-A3B IQ2_M", "measure/results/Qwen3-30B-A3B-IQ2M.depth.txt", RED, "-"),
        ("EXAONE-3.5-7.8B Q4_K_M", "measure/results/EXAONE-3.5-7.8B-Q4KM.depth.txt", GREEN, "--"),
    ]
    fig, ax = plt.subplots(figsize=(7.6, 4.6))
    data = {}
    for label, rel, color, ls in series:
        rows = parse_depth(os.path.join(ROOT, rel))
        if not rows:
            print(f"  ⚠ 비었음: {rel}", file=sys.stderr)
            continue
        data[label] = rows
        ax.plot([r[0] for r in rows], [r[2] for r in rows], ls,
                color=color, linewidth=2, marker="o", markersize=4, label=label, zorder=3)

    a = data.get("Qwen3-8B Q4_K_M", [])
    b = data.get("Qwen3-30B-A3B IQ2_M", [])
    for (d1, _, g1), (d2, _, g2) in zip(a, b):
        if d1 == d2 and g1 >= g2:                      # 첫 추월 지점 = 실측점
            ax.axvline(d1, color=MUTED, linewidth=1, linestyle=":", zorder=1)
            ax.annotate(f"crossover measured here\n{d1:,} tok — 8B {g1:.2f} vs 30B {g2:.2f}",
                        xy=(d1, g1), xytext=(d1 * 1.25, g1 + 2.6), fontsize=8.5, color=INK,
                        arrowprops=dict(arrowstyle="->", color=MUTED, linewidth=1))
            break

    ax.set_xscale("log")
    _style(ax, "Generation speed vs context depth", "context depth (tokens, log)", "generation tok/s")
    ax.legend(frameon=False, fontsize=8.5)
    fig.text(0.5, -0.02, "single run · Jetson Orin NX 16GB · MAXN · llama.cpp CUDA",
             ha="center", fontsize=7.5, color=MUTED)
    _save(fig, out, "depth-crossover.png")


def fig_achieved_bandwidth(out):
    """참고 — 60 GB/s 는 마이크로벤치의 한계이지 보드의 천장이 아니다."""
    rows = sorted(load_analysis()["quant_efficiency"], key=lambda r: r["traffic_gbps"])
    labels = [r["model"].replace("-Instruct", "") for r in rows]
    vals = [r["traffic_gbps"] for r in rows]
    colors = [BLUE if r["quant"] == "Q8_0" else MUTED for r in rows]

    fig, ax = plt.subplots(figsize=(8.2, 4.8))
    ax.barh(labels, vals, color=colors, height=0.62, zorder=3)
    for y, v in enumerate(vals):
        ax.text(v + 1.4, y, f"{v:.1f}", va="center", fontsize=8, color=INK)

    top = len(rows) - 0.35
    ax.axvline(102.4, color=RED, linewidth=1.4, linestyle="--", zorder=4)
    ax.text(102.4, top, "datasheet peak\n102.4", color=RED, fontsize=8, va="bottom", ha="center")
    ax.axvline(60.0, color=GREEN, linewidth=1.4, linestyle=":", zorder=4)
    ax.text(60.0, top, "membw.cu read kernel\n60.0", color=GREEN, fontsize=8, va="bottom", ha="center")
    ax.set_ylim(-0.7, len(rows) + 0.9)

    ax.set_xlim(0, 112)
    _style(ax, "Achieved weight-traffic bandwidth (tok/s x file size)", "GB/s", "")
    fig.text(0.5, -0.05, "Q8_0 reaches 86.4 GB/s — 84% of peak, 44% above the read kernel.  " + CAPTION,
             ha="center", fontsize=7.5, color=MUTED)
    _save(fig, out, "achieved-bandwidth.png")


def fig_tokens_per_watt(out):
    """① 올라가는가 — 같은 보드에서 모델마다 와트당 성능이 다르다."""
    d = load_analysis()
    tg = {r["model"]: r["tg128_tok_s"] for r in d["measured"] if r.get("tg128_tok_s")}
    # ★ run 디렉토리가 둘이다 (2차는 MoE 대조 실험이라 30B 를 다시 돌렸다).
    #   analysis.json 의 tg128 은 1차 기준이므로 **모델당 1차 것만** 쓴다 —
    #   안 그러면 Qwen3-30B 가 두 번 나오고 와트가 서로 다른 유령 항목이 생긴다.
    by_model = {}
    for path in sorted(glob.glob(os.path.join(RAW, "run-*", "*.tegrastats.txt"))):
        model = os.path.basename(path)[: -len(".tegrastats.txt")]
        by_model.setdefault(model, path)          # sorted 라 앞선 run 이 남는다

    pts = []
    for model, path in by_model.items():
        mw = mean_vdd_mw(path)
        if model in tg and mw:
            pts.append((model, tg[model] / (mw / 1000.0), tg[model], mw / 1000.0))
    assert len(pts) == len(tg), f"모델 {len(tg)}개인데 점이 {len(pts)}개다"
    pts.sort(key=lambda p: p[1])

    fig, ax = plt.subplots(figsize=(8.2, 4.8))
    ax.barh([p[0].replace("-Instruct", "") for p in pts], [p[1] for p in pts],
            color=BLUE, height=0.62, zorder=3)
    for y, p in enumerate(pts):
        ax.text(p[1] + 0.012, y, f"{p[1]:.2f}   ({p[2]:.1f} tok/s @ {p[3]:.1f} W)",
                va="center", fontsize=7.5, color=INK)
    ax.set_xlim(0, max(p[1] for p in pts) * 1.55)
    _style(ax, "Tokens per watt (tg128 / mean VDD_IN)", "tok/s per watt", "")
    fig.text(0.5, -0.05, "MAXN only — 15W and 25W were never benchmarked.  " + CAPTION,
             ha="center", fontsize=7.5, color=MUTED)
    _save(fig, out, "tokens-per-watt.png")


def fig_memory_budget(out):
    """③ 메모리를 얼마나 둘지 — 13.4 GB 를 무엇이 먹나."""
    parts = [
        ("llama-server 8B Q4_K_M\n(weights + activations)", 6.7 - 1.50, BLUE),
        ("KV cache @ ctx 20480", 1.50, "#7fb3ec"),
        ("embedding 0.6B (CPU, -ngl 0)", 1.8, GREEN),
        ("OpenClaw gateway (node)", 0.3, "#f0b429"),
        ("headroom (python, OS slack)", 13.4 - 6.7 - 1.8 - 0.3, GRID),
    ]
    fig, ax = plt.subplots(figsize=(8.2, 2.5))
    left = 0.0
    for label, gb, color in parts:
        ax.barh([0], [gb], left=left, color=color, height=0.5, zorder=3,
                edgecolor="white", linewidth=1.5)
        if gb > 0.6:
            ax.text(left + gb / 2, 0, f"{gb:.2f} GB", ha="center", va="center",
                    fontsize=8.5, color=INK if color == GRID else "white", fontweight="bold")
        left += gb

    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for _, _, c in parts]
    ax.legend(handles, [p[0] for p in parts], frameon=False, fontsize=7.5,
              loc="upper center", bbox_to_anchor=(0.5, -0.28), ncol=3)
    ax.set_xlim(0, 13.4)
    _style(ax, "What fills the 13.4 GB available to the LLM stack", "GB", "")
    ax.set_yticks([])
    fig.text(0.5, -0.62, "ctx 40960 -> 20480 cut the KV cache from 2.99 to 1.50 GB. "
                         "That is the decision that made this budget fit.",
             ha="center", fontsize=7.5, color=MUTED)
    _save(fig, out, "memory-budget.png")


def main():
    ap = argparse.ArgumentParser(description="측정 결과에서 그림을 재생성한다")
    ap.add_argument("--out", default=os.path.join(ROOT, "measure/figures"))
    args = ap.parse_args()
    print(f"→ {os.path.relpath(args.out, ROOT)}/")
    fig_depth_crossover(args.out)
    fig_achieved_bandwidth(args.out)
    fig_tokens_per_watt(args.out)
    fig_memory_budget(args.out)


if __name__ == "__main__":
    main()
