#!/usr/bin/env bash
# Emit the measurement environment as JSON. No root required.
set -uo pipefail
GPUDEV=/sys/devices/platform/bus@0/17000000.gpu/devfreq/17000000.gpu
r() { cat "$1" 2>/dev/null | tr -d '\0' | head -1; }

python3 - "$(r /proc/device-tree/model)" \
         "$(cat /proc/device-tree/compatible 2>/dev/null | tr '\0' '\n' | paste -sd'|')" \
         "$(r /var/lib/nvpmodel/status)" \
         "$(r $GPUDEV/max_freq)" "$(r $GPUDEV/cur_freq)" \
         "$(r /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq)" \
         "$(nproc)" \
         "$(head -1 /etc/nv_tegra_release 2>/dev/null | sed 's/^# //')" \
         "$(dpkg-query -W -f='${Version}' nvidia-jetpack 2>/dev/null)" \
         "$(cd /home/user/llama.cpp 2>/dev/null && git log --oneline -1 2>/dev/null)" <<'PY'
import json, subprocess, sys, datetime

model, compat, pmode, gmax, gcur, cmax, ncpu, l4t, jetpack, llama = sys.argv[1:11]

def temps():
    import glob, os
    out = {}
    for z in glob.glob('/sys/devices/virtual/thermal/thermal_zone*'):
        try:
            t = open(os.path.join(z, 'type')).read().strip()
            v = int(open(os.path.join(z, 'temp')).read().strip()) / 1000.0
            out[t] = v
        except Exception:
            pass
    return out

def nvml():
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        d = {"name": pynvml.nvmlDeviceGetName(h), "count": pynvml.nvmlDeviceGetCount()}
        try:
            d["memory_total"] = pynvml.nvmlDeviceGetMemoryInfo(h).total
        except Exception as e:
            d["memory_total_error"] = type(e).__name__
        try:
            d["compute_capability"] = list(pynvml.nvmlDeviceGetCudaComputeCapability(h))
        except Exception as e:
            d["compute_capability_error"] = type(e).__name__
        try:
            v = pynvml.nvmlSystemGetCudaDriverVersion_v2()
            d["cuda_driver"] = f"{v//1000}.{(v%1000)//10}"
        except Exception:
            pass
        return d
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

def smi():
    try:
        return subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,clocks.max.memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception as e:
        return f"error: {e}"

def meminfo():
    d = {}
    for line in open('/proc/meminfo'):
        k, v = line.split(':', 1)
        if k in ('MemTotal', 'MemAvailable', 'MemFree', 'SwapTotal', 'SwapFree'):
            d[k] = int(v.strip().split()[0]) * 1024
    return d

print(json.dumps({
    "collected_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "board": {
        "device_tree_model": model,
        "device_tree_compatible": [c for c in compat.split('|') if c],
        "soc": next((c.split(',')[1] for c in compat.split('|') if c.startswith('nvidia,tegra')), None),
        "module_part_number": next(
            (c.split(',')[1] for c in compat.split('|')
             if ',' in c and c.split(',')[1].startswith('p') and '+' not in c.split(',')[1]
             and not c.split(',')[1].startswith('tegra')), None),
    },
    "power": {
        "nvpmodel_status": pmode,
        "gpu_max_freq_hz": int(gmax) if gmax.isdigit() else None,
        "gpu_cur_freq_hz": int(gcur) if gcur.isdigit() else None,
        "cpu_max_freq_khz": int(cmax) if cmax.isdigit() else None,
        "cpu_count": int(ncpu) if ncpu.isdigit() else None,
    },
    "software": {"l4t": l4t, "jetpack": jetpack, "llama_cpp": llama},
    "nvml": nvml(),
    "nvidia_smi_whichllm_query": smi(),
    "memory": meminfo(),
    "thermal_c": temps(),
}, indent=2))
PY
