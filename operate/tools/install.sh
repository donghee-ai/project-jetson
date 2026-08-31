#!/usr/bin/env bash
# 추론 런타임(llama-server)을 사용자 systemd 에 건다.
#
# ★ 이건 **공유 자산**이다 — 벤치마크(bench/*.py) · Life Trainer · OpenClaw 게이트웨이가
#   전부 :8080 의 이 서버를 쓴다. 그래서 앱이 아니라 runtime/ 에 있다.
#
# 게이트웨이 드롭인은 여기서 안 건다 → Life_Trainer/deploy/install-gateway.sh
set -euo pipefail

RUNTIME_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"

chmod +x "$RUNTIME_DIR/llama-server-qwen3.sh"
mkdir -p "$UNIT_DIR/llama-server.service.d"

ln -sf "$RUNTIME_DIR/systemd/llama-server.service" "$UNIT_DIR/llama-server.service"

# ★ 이 한 줄이 없어서 조용히 망가진 적이 있다.
#   ctx.conf 링크가 끊기면 LLAMA_CTX 가 기본값 40960 으로 돌아가고
#   KV 캐시가 1.50 → 2.99 GB 로 뛰어, 8B 가 llama-embed 옆에서 CUDA 버퍼를 못 잡는다.
#   에러 메시지가 원인을 안 가리킨다.
ln -sf "$RUNTIME_DIR/systemd/llama-server.service.d/ctx.conf" \
       "$UNIT_DIR/llama-server.service.d/ctx.conf"

chmod +x "$RUNTIME_DIR/restart-llama-server.sh"
ln -sf "$RUNTIME_DIR/systemd/llama-server-restart.service" "$UNIT_DIR/llama-server-restart.service"
ln -sf "$RUNTIME_DIR/systemd/llama-server-restart.timer"   "$UNIT_DIR/llama-server-restart.timer"

systemctl --user daemon-reload
systemctl --user enable --now llama-server
# ★ 8B 는 자란다 (2026-08-30 전역 OOM). 매일 06:10 에 재기동해 회수한다.
systemctl --user enable --now llama-server-restart.timer

# 헤드리스 부팅에서 사용자 서비스가 뜨려면 linger 가 필요하다.
# 없으면 SSH 로 로그인할 때까지 아무것도 안 뜬다.
loginctl enable-linger "$USER"

echo
systemctl --user is-active llama-server
echo "  LLAMA_CTX = $(systemctl --user show llama-server -p Environment --value | tr ' ' '\n' | grep LLAMA_CTX || echo '★ 안 걸림 — ctx.conf 링크를 확인할 것')"
