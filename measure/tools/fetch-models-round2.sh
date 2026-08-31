#!/usr/bin/env bash
# Round 2: controls that pin down the two load-bearing claims.
#
#   Qwen3-32B IQ2_M  dense, 10.58 GiB, same quant and near-identical size to the
#                    Qwen3-30B-A3B MoE (9.71 GiB). Measuring a dense model in the
#                    same format gives the MoE read ratio without assuming any
#                    _QUANT_EFFICIENCY value.
#   Qwen3-4B Q8_0    replicates the Q8_0 result at a second size. The 86.4 GB/s
#                    figure from Qwen3-8B Q8_0 is what refutes the "60 GB/s
#                    ceiling" claim, and it is currently n=1.
set -uo pipefail
DEST="${1:-/home/user/project/opensource/models}"
MODELS=(
"Qwen3-32B-IQ2_M.gguf|https://huggingface.co/bartowski/Qwen_Qwen3-32B-GGUF/resolve/main/Qwen_Qwen3-32B-IQ2_M.gguf"
"Qwen3-4B-Q8_0.gguf|https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q8_0.gguf"
)
for entry in "${MODELS[@]}"; do
  name="${entry%%|*}"; url="${entry#*|}"; out="$DEST/$name"
  want=$(curl -sIL "$url" | grep -i '^x-linked-size:' | tail -1 | tr -d '\r' | awk '{print $2}')
  [ -z "$want" ] && { echo "[SKIP] $name"; continue; }
  if [ -f "$out" ] && [ "$(stat -c%s "$out")" = "$want" ]; then echo "[HAVE] $name"; continue; fi
  avail=$(df --output=avail -B1 "$DEST" | tail -1)
  if [ "$avail" -lt $((want + 2*1024*1024*1024)) ]; then
    echo "[STOP] $name needs $(echo "scale=1;$want/1073741824"|bc) GiB, only $(echo "scale=1;$avail/1073741824"|bc) GiB free"; continue
  fi
  echo "[GET ] $name ($(echo "scale=2;$want/1073741824"|bc) GiB)"
  curl -L --fail --retry 5 --retry-delay 5 -C - --no-progress-meter -o "$out" "$url"
  got=$(stat -c%s "$out" 2>/dev/null || echo 0); magic=$(head -c4 "$out" 2>/dev/null)
  [ "$got" = "$want" ] && [ "$magic" = "GGUF" ] && echo "[OK  ] $name" || echo "[BAD ] $name got=$got want=$want"
done
echo "=== DONE ==="; df -h /home | tail -1
