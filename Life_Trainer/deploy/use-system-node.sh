#!/usr/bin/env bash
# 게이트웨이 유닛을 시스템 Node 로 전환한다. (일반 사용자 권한)
# 선행: sudo bash openclaw-setup/install-system-node.sh
set -euo pipefail

SETUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DROPIN_DIR="$HOME/.config/systemd/user/openclaw-gateway.service.d"

if [ ! -x /usr/local/bin/node ]; then
  echo "/usr/local/bin/node 가 없다. 먼저 실행: sudo bash $SETUP_DIR/install-system-node.sh" >&2
  exit 1
fi

echo "==> 시스템 Node: $(/usr/local/bin/node --version)"

# 유닛 본체를 다시 생성한다. 이제 /usr/local/bin/node 가 존재하므로 openclaw 가
# ExecStart 를 시스템 Node 로 잡는다 (resolveSystemNodeInfo 가 version-managed
# 아닌 지원 버전을 우선한다). --force 없이는 "already enabled" 로 그냥 넘어간다.
openclaw gateway install --force

# 본체 PATH 에는 여전히 nvm 경로가 섞이므로 드롭인으로 덮는다.
mkdir -p "$DROPIN_DIR"
ln -sf "$SETUP_DIR/systemd/openclaw-gateway.service.d/20-system-node.conf" \
       "$DROPIN_DIR/20-system-node.conf"

systemctl --user daemon-reload
systemctl --user restart openclaw-gateway

echo
echo "==> 실제 실행 중인 커맨드"
systemctl --user show openclaw-gateway -p MainPID --value \
  | xargs -I{} sh -c 'tr "\0" " " < /proc/{}/cmdline; echo'
echo "==> 실제 PATH"
systemctl --user show openclaw-gateway -p Environment --value | tr ' ' '\n' | grep -i '^PATH='
