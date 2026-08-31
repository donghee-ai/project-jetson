#!/usr/bin/env python3
"""Summarise tegrastats samples captured during each llama-bench run.

Used to show the throughput numbers are not thermally or power limited: on
Orin the GPU throttles well above these temperatures, and a run pinned at max
GPU frequency with headroom on tj is bandwidth-bound, not thermally bound.
"""

from __future__ import annotations

import glob
import os
import re
import sys

RE_GR3D = re.compile(r"GR3D_FREQ (\d+)%")
RE_TEMP = re.compile(r"(\w+)@([\d.]+)C")
RE_VDD = re.compile(r"VDD_IN (\d+)mW")
RE_RAM = re.compile(r"RAM (\d+)/(\d+)MB")


def summarise(path: str) -> dict | None:
    gr3d, tj, vdd, ram = [], [], [], []
    with open(path) as fh:
        for line in fh:
            if m := RE_GR3D.search(line):
                gr3d.append(int(m.group(1)))
            temps = dict(RE_TEMP.findall(line))
            if "tj" in temps:
                tj.append(float(temps["tj"]))
            if m := RE_VDD.search(line):
                vdd.append(int(m.group(1)))
            if m := RE_RAM.search(line):
                ram.append(int(m.group(1)))
    if not gr3d:
        return None
    return {
        "samples": len(gr3d),
        "gpu_busy_max": max(gr3d),
        "gpu_busy_mean": sum(gr3d) / len(gr3d),
        "tj_max_c": max(tj) if tj else None,
        "vdd_in_max_mw": max(vdd) if vdd else None,
        "ram_peak_mb": max(ram) if ram else None,
    }


def main() -> int:
    run_dir = sys.argv[1]
    rows = []
    for path in sorted(glob.glob(os.path.join(run_dir, "*.tegrastats.txt"))):
        base = os.path.basename(path)[: -len(".tegrastats.txt")]
        s = summarise(path)
        if s:
            rows.append((base, s))
    if not rows:
        print("no telemetry found", file=sys.stderr)
        return 1
    hdr = (f"{'model':<40}{'n':>5}{'GPU%max':>9}{'GPU%avg':>9}"
           f"{'tj max':>9}{'VDD_IN max':>12}{'RAM peak':>10}")
    print(hdr); print("-" * len(hdr))
    for base, s in rows:
        print(f"{base[:39]:<40}{s['samples']:>5}{s['gpu_busy_max']:>8}%"
              f"{s['gpu_busy_mean']:>8.0f}%{s['tj_max_c']:>8.1f}C"
              f"{s['vdd_in_max_mw']/1000:>10.1f}W{s['ram_peak_mb']:>9}M")
    print()
    print(f"Orin NX software thermal throttle starts at tj 99C; "
          f"hottest run here was {max(s['tj_max_c'] for _, s in rows):.1f}C.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
