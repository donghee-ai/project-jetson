#!/usr/bin/env bash
# Download the GGUF benchmark set. Idempotent: verifies size + magic, skips complete files.
set -uo pipefail
DEST="${1:-/home/user/project/opensource/models}"
mkdir -p "$DEST"

# name|url
MODELS=(
"Qwen3-1.7B-Q4_K_M.gguf|https://huggingface.co/unsloth/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q4_K_M.gguf"
"gemma-3-4b-it-Q4_K_M.gguf|https://huggingface.co/ggml-org/gemma-3-4b-it-GGUF/resolve/main/gemma-3-4b-it-Q4_K_M.gguf"
"Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf|https://huggingface.co/bartowski/Meta-Llama-3.1-8B-Instruct-GGUF/resolve/main/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
"Qwen3-8B-Q6_K.gguf|https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q6_K.gguf"
"Qwen3-8B-Q8_0.gguf|https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q8_0.gguf"
"Qwen3-14B-Q4_K_M.gguf|https://huggingface.co/Qwen/Qwen3-14B-GGUF/resolve/main/Qwen3-14B-Q4_K_M.gguf"
"gpt-oss-20b-MXFP4.gguf|https://huggingface.co/ggml-org/gpt-oss-20b-GGUF/resolve/main/gpt-oss-20b-MXFP4.gguf"
)

for entry in "${MODELS[@]}"; do
  name="${entry%%|*}"; url="${entry#*|}"; out="$DEST/$name"
  want=$(curl -sIL "$url" | grep -i '^x-linked-size:' | tail -1 | tr -d '\r' | awk '{print $2}')
  if [ -z "$want" ]; then echo "[SKIP] $name — could not resolve size"; continue; fi
  if [ -f "$out" ] && [ "$(stat -c%s "$out")" = "$want" ]; then echo "[HAVE] $name"; continue; fi
  echo "[GET ] $name ($(echo "scale=2;$want/1073741824"|bc) GiB)"
  curl -L --fail --retry 5 --retry-delay 5 -C - -o "$out" "$url" 2>&1 | tail -1
  got=$(stat -c%s "$out" 2>/dev/null || echo 0)
  magic=$(head -c4 "$out" 2>/dev/null)
  if [ "$got" = "$want" ] && [ "$magic" = "GGUF" ]; then echo "[OK  ] $name"
  else echo "[BAD ] $name got=$got want=$want magic=$magic"; fi
done
echo "=== DONE ==="
df -h /home | tail -1
