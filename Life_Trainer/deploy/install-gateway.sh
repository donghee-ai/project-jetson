#!/usr/bin/env bash
# OpenClaw 게이트웨이 드롭인 2개를 건다. **에이전트 경로에만 필요하다.**
#
# ★ 둘을 한 곳에서 거는 이유: 전에는 10-depends-llama 만 install.sh 가 걸고
#   20-system-node 는 use-system-node.sh 만 걸어서, install.sh 만 다시 돌리면
#   node PATH 드롭인이 조용히 빠졌다.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
DROPIN="$UNIT_DIR/openclaw-gateway.service.d"

mkdir -p "$DROPIN"
for conf in 10-depends-llama.conf 20-system-node.conf; do
  ln -sf "$DEPLOY_DIR/systemd/openclaw-gateway.service.d/$conf" "$DROPIN/$conf"
done

systemctl --user daemon-reload
systemctl --user restart openclaw-gateway

echo
systemctl --user is-active openclaw-gateway
ls -l "$DROPIN" | sed 's/^/  /'
