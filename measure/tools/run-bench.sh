#!/usr/bin/env bash
# llama-bench sweep for the whichllm Jetson contribution.
#
# Measures prompt-processing (pp512) and token-generation (tg128) throughput for
# every GGUF in MODEL_DIR, with power/thermal telemetry sampled per model so the
# numbers can be shown not to be thermally throttled.
#
# tg128 is the quantity whichllm's estimate_tok_per_sec() models, so that is the
# column the contribution is validated against.
set -uo pipefail

ROOT=/home/user/project/opensource
MODEL_DIR="${MODEL_DIR:-$ROOT/models}"
OUT="${OUT:-$ROOT/results/raw}"
LB="${LB:-/home/user/llama.cpp/build/bin/llama-bench}"
REPS="${REPS:-3}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN="$OUT/run-$STAMP"
mkdir -p "$RUN"

SERVICE=llama-server.service
# Units that declare Requires=llama-server.service are stopped by systemd along
# with it, and are NOT brought back by starting llama-server again. Record
# everything that was running so cleanup can restore the original state.
RESTORE_UNITS=()

cleanup() {
  local unit
  for unit in "${RESTORE_UNITS[@]}"; do
    echo "[env] restarting $unit"
    systemctl --user start "$unit" || echo "[env] WARN: failed to restart $unit"
  done
}
trap cleanup EXIT INT TERM

if systemctl --user is-active --quiet "$SERVICE"; then
  # Collect reverse dependencies that are currently active, before stopping.
  while read -r unit; do
    [ -n "$unit" ] || continue
    if [ "$unit" != "$SERVICE" ] && systemctl --user is-active --quiet "$unit"; then
      RESTORE_UNITS+=("$unit")
    fi
  done < <(systemctl --user list-dependencies --reverse --plain --no-legend              "$SERVICE" 2>/dev/null | awk '{print $1}' | grep -E '\.service$' || true)

  echo "[env] stopping $SERVICE to free unified memory"
  if systemctl --user stop "$SERVICE"; then
    # Restart the backend first, then its dependents.
    RESTORE_UNITS=("$SERVICE" "${RESTORE_UNITS[@]}")
  fi
  if [ ${#RESTORE_UNITS[@]} -gt 1 ]; then
    echo "[env] will also restore: ${RESTORE_UNITS[*]:1}"
  fi
  sleep 3
fi

echo "[env] collecting environment"
"$ROOT/bench/collect-env.sh" > "$RUN/environment.json"

mapfile -t MODELS < <(find -L "$MODEL_DIR" -maxdepth 1 -name '*.gguf' -printf '%f\n' | sort)
echo "[env] ${#MODELS[@]} models in $MODEL_DIR"

for f in "${MODELS[@]}"; do
  path="$MODEL_DIR/$f"
  base="${f%.gguf}"
  size=$(stat -Lc%s "$path")
  echo "[bench] $base ($(echo "scale=2;$size/1073741824"|bc) GiB)"

  # telemetry during this model's run
  tegrastats --interval 1000 > "$RUN/$base.tegrastats.txt" 2>/dev/null &
  TSPID=$!

  timeout 1800 "$LB" -m "$path" -ngl 99 -p 512 -n 128 -fa 1 -r "$REPS" -o json \
      > "$RUN/$base.bench.json" 2> "$RUN/$base.bench.err"
  rc=$?

  kill "$TSPID" 2>/dev/null; wait "$TSPID" 2>/dev/null

  if [ $rc -ne 0 ]; then
    echo "[bench] FAILED rc=$rc — see $base.bench.err"
    echo "{\"model\":\"$base\",\"error\":\"rc=$rc\",\"file_size_bytes\":$size}" > "$RUN/$base.failed.json"
  else
    echo "[bench] ok"
  fi
  echo "$size" > "$RUN/$base.filesize"
  sleep 5
done

echo "[env] sweep complete -> $RUN"
echo "$RUN" > "$OUT/latest-run"
