#!/usr/bin/env python3
"""Measure the MoE per-token read ratio against a dense control.

whichllm assumes an MoE model reads its active-parameter fraction of the weights
per token. Inverting its speed formula to check that would require trusting
_QUANT_EFFICIENCY for the quant in question, which measurements on this board
show is unreliable (Q8_0 is off by ~88%).

This avoids the issue entirely by measuring a *dense* model in the same
quantization. A dense model reads all of its weights every token, so its
throughput fixes the achieved bandwidth for that format:

    achieved_bw = tg_dense * size_dense

Applying that to the MoE model gives the ratio with no coefficient involved:

    read_ratio  = (tg_dense * size_dense) / (tg_moe * size_moe)

The one assumption left is that the MoE model achieves the same bandwidth as the
dense one. Scattered expert reads make that an upper bound in practice, so the
resulting ratio is an upper bound too — and it still lands far above the
active-parameter fraction, which is the point.

    uv run --locked python moe-control.py <run-dir> [<run-dir> ...]
"""

from __future__ import annotations

import glob
import json
import os
import sys

# name fragment -> (active params, total params)
MOE = {
    "Qwen3-30B-A3B": (3_300_000_000, 30_500_000_000),
    "gpt-oss-20b": (3_600_000_000, 20_900_000_000),
}
# MoE name fragment -> dense control measured in the same quantization
CONTROLS = {"Qwen3-30B-A3B": "Qwen3-32B-IQ2_M"}


def load(run_dirs: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for run_dir in run_dirs:
        for path in sorted(glob.glob(os.path.join(run_dir, "*.bench.json"))):
            base = os.path.basename(path)[: -len(".bench.json")]
            try:
                entries = json.load(open(path))
            except Exception:
                continue
            tg = [e for e in entries if e.get("n_gen", 0) > 0]
            if not tg:
                continue
            rec = {
                "tg": tg[0]["avg_ts"],
                "sd": tg[0].get("stddev_ts", 0.0),
                "size": entries[0]["model_size"],
                "run": os.path.basename(run_dir),
            }
            # Keep every session so repeats can be shown.
            out.setdefault(base, {"sessions": []})["sessions"].append(rec)
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    data = load(sys.argv[1:])

    print("\n=== SESSIONS ===\n")
    for name, rec in sorted(data.items()):
        for s in rec["sessions"]:
            print(f"  {name:<30}{s['size']/1024**3:>7.2f} GiB"
                  f"{s['tg']:>8.2f} ±{s['sd']:<6.2f}  {s['run']}")

    print("\n=== DENSE CONTROL -> MoE READ RATIO ===\n")
    for moe_key, control in CONTROLS.items():
        moe_name = next((n for n in data if moe_key.lower() in n.lower()), None)
        if moe_name is None or control not in data:
            print(f"  missing data for {moe_key} / {control}")
            continue
        active, total = MOE[moe_key]
        c = data[control]["sessions"][0]
        bw = c["tg"] * c["size"] / 1e9
        print(f"  dense control  {control}")
        print(f"    {c['size']/1024**3:.2f} GiB x {c['tg']:.2f} tok/s "
              f"= {bw:.1f} GB/s achieved for this quantization\n")
        for s in data[moe_name]["sessions"]:
            ratio = (c["tg"] * c["size"]) / (s["tg"] * s["size"])
            gb = ratio * s["size"] / 1024**3
            print(f"  MoE  {moe_name}  ({s['run']})")
            print(f"    measured {s['tg']:.2f} tok/s -> read ratio "
                  f"{ratio:.3f}  ({gb:.2f} GiB/token)")
            print(f"    whichllm assumes {active/total:.3f}  "
                  f"-> understated {ratio/(active/total):.1f}x\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
