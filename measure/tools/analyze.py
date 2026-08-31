#!/usr/bin/env python3
"""Compare measured llama-bench throughput against whichllm's own estimator.

Runs whichllm's real `estimate_tok_per_sec()` (not a reimplementation) against
the measured GGUF file sizes, sweeping candidate memory-bandwidth values. The
bandwidth that minimises dense-model error is the value the GPU registry entry
should carry.

Usage:
    uv run --locked python analyze.py <run-dir> [--json out.json]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys

from whichllm.engine.performance import estimate_tok_per_sec
from whichllm.hardware.types import GPUInfo
from whichllm.models.types import GGUFVariant, ModelInfo

# MoE topology is not present in llama-bench output, so it is declared here.
# active/total parameter counts come from each model's published config.
MOE = {
    "Qwen3-30B-A3B": (3_300_000_000, True),
    "gpt-oss-20b": (3_600_000_000, True),
}

# Candidate bandwidths (GB/s) to sweep for the registry entry.
CANDIDATES = [34.0, 51.2, 60.0, 68.0, 69.5, 102.4, 136.5, 204.8]


def quant_from_name(name: str) -> str:
    upper = name.upper().replace("-", "_")
    for q in ("IQ4_XS", "IQ4_NL", "IQ3_XXS", "IQ3_XS", "IQ3_M", "IQ3_S",
              "IQ2_XXS", "IQ2_M", "IQ2_S", "IQ1_M", "IQ1_S",
              "Q4_K_M", "Q4_K_S", "Q5_K_M", "Q5_K_S", "Q3_K_M", "Q3_K_S",
              "Q3_K_L", "Q2_K", "Q8_0", "Q6_K", "Q5_0", "Q4_0",
              "MXFP4", "NVFP4", "BF16", "F16", "F32"):
        if q in upper:
            return q
    return "Q4_K_M"


def load_run(run_dir: str) -> list[dict]:
    rows = []
    for path in sorted(glob.glob(os.path.join(run_dir, "*.bench.json"))):
        base = os.path.basename(path)[: -len(".bench.json")]
        try:
            with open(path) as fh:
                entries = json.load(fh)
        except Exception as exc:
            print(f"  ! skip {base}: {exc}", file=sys.stderr)
            continue
        if not entries:
            continue

        pp = [e for e in entries if e.get("n_prompt", 0) > 0]
        tg = [e for e in entries if e.get("n_gen", 0) > 0]
        if not tg:
            print(f"  ! skip {base}: no token-generation rows", file=sys.stderr)
            continue

        e0 = entries[0]
        active, is_moe = None, False
        for key, (act, moe) in MOE.items():
            if key.lower() in base.lower():
                active, is_moe = act, moe

        rows.append({
            "model": base,
            "quant": quant_from_name(base),
            "file_size_bytes": int(e0.get("model_size", 0)),
            "n_params": int(e0.get("model_n_params", 0)),
            "model_type": e0.get("model_type", ""),
            "is_moe": is_moe,
            "params_active": active,
            "pp512_tok_s": pp[0].get("avg_ts") if pp else None,
            "pp512_sd": pp[0].get("stddev_ts") if pp else None,
            "tg128_tok_s": tg[0].get("avg_ts"),
            "tg128_sd": tg[0].get("stddev_ts"),
            "backends": e0.get("backends", ""),
            "flash_attn": e0.get("flash_attn"),
            "n_gpu_layers": e0.get("n_gpu_layers"),
            "type_k": e0.get("type_k"),
            "type_v": e0.get("type_v"),
            "build": e0.get("build_commit", ""),
        })
    return rows


def predict(row: dict, bandwidth: float) -> float:
    """Call whichllm's own estimator for this model at a given bandwidth."""
    variant = GGUFVariant(
        filename=row["model"] + ".gguf",
        quant_type=row["quant"],
        file_size_bytes=row["file_size_bytes"],
    )
    model = ModelInfo(
        id=row["model"],
        family_id=row["model"],
        name=row["model"],
        parameter_count=row["n_params"],
        parameter_count_active=row["params_active"],
        is_moe=row["is_moe"],
        gguf_variants=[variant],
    )
    gpu = GPUInfo(
        name="Orin (nvgpu)",
        vendor="nvidia",
        vram_bytes=15_642 * 1024**2,
        compute_capability=(8, 7),
        memory_bandwidth_gbps=bandwidth,
        shared_memory=True,
    )
    return estimate_tok_per_sec(model, variant, gpu, fit_type="full_gpu")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()

    rows = load_run(args.run_dir)
    if not rows:
        print("no benchmark rows found", file=sys.stderr)
        return 1

    env_path = os.path.join(args.run_dir, "environment.json")
    env = json.load(open(env_path)) if os.path.exists(env_path) else {}

    dense = [r for r in rows if not r["is_moe"]]
    moe = [r for r in rows if r["is_moe"]]

    print(f"\n=== MEASURED (llama-bench, -p 512 -n 128 -fa 1) — {len(rows)} models ===\n")
    hdr = f"{'model':<38}{'quant':<8}{'GiB':>6}{'params':>9}{'pp512':>10}{'tg128':>10}"
    print(hdr); print("-" * len(hdr))
    for r in sorted(rows, key=lambda x: x["file_size_bytes"]):
        pp = f"{r['pp512_tok_s']:.1f}" if r["pp512_tok_s"] else "-"
        print(f"{r['model'][:37]:<38}{r['quant']:<8}"
              f"{r['file_size_bytes']/1024**3:>6.2f}{r['n_params']/1e9:>8.1f}B"
              f"{pp:>10}{r['tg128_tok_s']:>10.2f}")

    # Implied peak bandwidth: invert whichllm's own formula per model. In the
    # bandwidth-bound regime these converge on the true peak; below it they fall
    # short, because the formula has no term for fixed per-token overhead.
    from whichllm.engine.performance import (
        _DEFAULT_QUANT_EFFICIENCY,
        _QUANT_EFFICIENCY,
    )

    print("\n=== IMPLIED PEAK BANDWIDTH per model (inverting the formula) ===")
    print("    implied = tg128 x weight_bytes / (quant_eff x backend_factor)\n")
    h = f"{'model':<38}{'quant':<8}{'GiB':>6}{'tg128':>9}{'implied GB/s':>14}"
    print(h); print("-" * len(h))
    for r in sorted(dense, key=lambda x: x["file_size_bytes"]):
        qe = _QUANT_EFFICIENCY.get(r["quant"], _DEFAULT_QUANT_EFFICIENCY)
        implied = r["tg128_tok_s"] * r["file_size_bytes"] / qe / 1e9
        r["implied_bandwidth_gbps"] = implied
        print(f"{r['model'][:37]:<38}{r['quant']:<8}{r['file_size_bytes']/1024**3:>6.2f}"
              f"{r['tg128_tok_s']:>9.2f}{implied:>14.1f}")

    print(f"\n=== BANDWIDTH SWEEP — whichllm estimate_tok_per_sec() vs measured tg128 ===")
    print(f"    dense models only (n={len(dense)}); error = pred/measured - 1\n")
    head = f"{'BW GB/s':>9}" + "".join(f"{r['model'][:11]:>13}" for r in dense) + f"{'MAPE':>9}{'bias':>9}"
    print(head); print("-" * len(head))
    best = None
    sweep = {}
    for bw in CANDIDATES:
        errs = []
        cells = ""
        for r in dense:
            p = predict(r, bw)
            e = p / r["tg128_tok_s"] - 1.0
            errs.append(e)
            cells += f"{e*100:>+12.1f}%"
        mape = statistics.mean(abs(e) for e in errs)
        bias = statistics.mean(errs)
        sweep[bw] = {"mape": mape, "bias": bias,
                     "per_model": {r["model"]: predict(r, bw) for r in dense}}
        print(f"{bw:>9.1f}{cells}{mape*100:>8.1f}%{bias*100:>+8.1f}%")
        if best is None or mape < best[1]:
            best = (bw, mape, bias)
    print(f"\n  -> best fit over all dense models: {best[0]:.1f} GB/s   "
          f"MAPE {best[1]*100:.1f}%   bias {best[2]*100:+.1f}%")

    # Restrict to the bandwidth-bound regime. Small models are dominated by
    # fixed per-token cost (kernel launches, attention, sampling) that the
    # formula does not model, so they drag the fit toward a too-low bandwidth.
    LARGE_GIB = 3.0
    FIT_QUANT = "Q4_K_M"
    large = [
        r for r in dense
        if r["file_size_bytes"] / 1024**3 >= LARGE_GIB and r["quant"] == FIT_QUANT
    ]
    best_large = None
    if len(large) >= 2:
        print(f"\n  restricted to {FIT_QUANT} weights >= {LARGE_GIB:.0f} GiB "
              f"(n={len(large)}) — one quant, so the fit isolates bandwidth:")
        for bw in CANDIDATES:
            errs = [predict(r, bw) / r["tg128_tok_s"] - 1.0 for r in large]
            mape = statistics.mean(abs(e) for e in errs)
            bias = statistics.mean(errs)
            marker = ""
            if best_large is None or mape < best_large[1]:
                best_large = (bw, mape, bias)
            print(f"    {bw:>7.1f} GB/s   MAPE {mape*100:>6.1f}%   bias {bias*100:>+6.1f}%{marker}")
        print(f"\n  -> best fit in the bandwidth-bound regime: {best_large[0]:.1f} GB/s  "
              f" MAPE {best_large[1]*100:.1f}%   bias {best_large[2]*100:+.1f}%")

    if moe:
        print(f"\n=== MoE MODELS — whichllm read-ratio model vs measured ===\n")
        h2 = f"{'model':<28}{'GiB':>6}{'act/total':>11}{'pred':>9}{'measured':>10}{'err':>9}{'implied rr':>12}"
        print(h2); print("-" * len(h2))
        for r in moe:
            p = predict(r, best[0])
            m = r["tg128_tok_s"]
            ratio = r["params_active"] / r["n_params"] if r["n_params"] else 0
            from whichllm.engine.performance import _QUANT_EFFICIENCY, _DEFAULT_QUANT_EFFICIENCY
            qe = _QUANT_EFFICIENCY.get(r["quant"], _DEFAULT_QUANT_EFFICIENCY)
            implied = (best[0] * 1e9 * qe) / (m * r["file_size_bytes"])
            print(f"{r['model'][:27]:<28}{r['file_size_bytes']/1024**3:>6.2f}{ratio:>11.3f}"
                  f"{p:>9.2f}{m:>10.2f}{(p/m-1)*100:>+8.1f}%{implied:>12.3f}")

    print("\n=== QUANT EFFICIENCY — measured achieved fraction of peak ===")
    print("    traffic = tg128 x weight_bytes ; achieved = traffic / peak_bandwidth\n")
    PEAK = best_large[0] if best_large else best[0]
    h3 = (f"{'model':<38}{'quant':<8}{'traffic GB/s':>14}"
          f"{'achieved':>10}{'whichllm':>10}{'delta':>9}")
    print(h3); print("-" * len(h3))
    quant_rows = []
    for r in sorted(dense, key=lambda x: x["file_size_bytes"]):
        qe = _QUANT_EFFICIENCY.get(r["quant"], _DEFAULT_QUANT_EFFICIENCY)
        traffic = r["tg128_tok_s"] * r["file_size_bytes"] / 1e9
        achieved = traffic / PEAK
        quant_rows.append({"model": r["model"], "quant": r["quant"],
                           "traffic_gbps": traffic, "achieved": achieved,
                           "whichllm_quant_efficiency": qe})
        print(f"{r['model'][:37]:<38}{r['quant']:<8}{traffic:>14.1f}"
              f"{achieved:>10.3f}{qe:>10.2f}{(achieved/qe-1)*100:>+8.0f}%")

    out = {
        "environment": env,
        "quant_efficiency": quant_rows,
        "peak_bandwidth_used_gbps": PEAK,
        "measured": rows,
        "bandwidth_sweep": sweep,
        "best_bandwidth_gbps": best[0],
        "best_mape": best[1],
        "best_bias": best[2],
        "bandwidth_bound_subset_min_gib": LARGE_GIB,
        "best_bandwidth_gbps_bandwidth_bound": best_large[0] if best_large else None,
        "best_mape_bandwidth_bound": best_large[1] if best_large else None,
    }
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(out, fh, indent=2)
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
