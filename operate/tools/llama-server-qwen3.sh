#!/usr/bin/env bash
# OpenClaw 백엔드용 llama.cpp 서버 — Qwen3-8B-Q4_K_M
#
# systemd(llama-server.service)가 이 스크립트를 실행한다.
# 래퍼로 감싼 이유: --chat-template-kwargs 의 JSON 인자를 systemd ExecStart 에
# 직접 쓰면 systemd 가 따옴표를 벗겨내 파싱이 깨진다.
set -euo pipefail

BIN="${LLAMA_BIN:-$HOME/llama.cpp/build/bin/llama-server}"
MODEL="${LLAMA_MODEL:-$HOME/models/Qwen3-8B-Q4_K_M.gguf}"
PORT="${LLAMA_PORT:-8080}"

# 운영 기본값은 실측에 맞춘 20480. Qwen3-8B 학습 상한 40960 이 필요하면
# LLAMA_CTX 로 명시한다 (systemd drop-in 도 같은 운영 기본값을 고정한다).
CTX="${LLAMA_CTX:-20480}"

exec "$BIN" \
  -m "$MODEL" \
  --alias qwen3-8b \
  -ngl 99 \
  -c "$CTX" \
  --parallel 1 \
  -fa on -ctk q8_0 -ctv q8_0 \
  --jinja \
  --chat-template-kwargs '{"enable_thinking":false}' \
  --temp 0.7 --top-p 0.8 --top-k 20 \
  --host 127.0.0.1 --port "$PORT"
