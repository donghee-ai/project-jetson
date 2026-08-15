#!/usr/bin/env bash
# systemd 유닛을 사용자 서비스로 등록한다. (일반 사용자 권한)
#
#   llama-server.service            모델 백엔드 (Qwen3-8B)
#   openclaw-gateway.service.d/     게이트웨이가 백엔드를 기다리게 하는 드롭인
#
# 이 저장소의 파일을 심링크로 건다 — 저장소를 고치면 유닛도 같이 바뀐다.
# openclaw-gateway.service 본체는 `openclaw daemon install` 이 만든 것을 쓴다.
set -euo pipefail

SETUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"

chmod +x "$SETUP_DIR/bin/llama-server-qwen3.sh"

mkdir -p "$UNIT_DIR/openclaw-gateway.service.d"
ln -sf "$SETUP_DIR/systemd/llama-server.service" "$UNIT_DIR/llama-server.service"
ln -sf "$SETUP_DIR/systemd/openclaw-gateway.service.d/10-depends-llama.conf" \
       "$UNIT_DIR/openclaw-gateway.service.d/10-depends-llama.conf"

systemctl --user daemon-reload
systemctl --user enable --now llama-server
systemctl --user restart openclaw-gateway

# 헤드리스 부팅에서 사용자 서비스가 뜨려면 linger 가 필요하다.
# 없으면 SSH 로 로그인할 때까지 아무것도 안 뜬다.
loginctl enable-linger "$USER"

echo
systemctl --user is-active llama-server openclaw-gateway
