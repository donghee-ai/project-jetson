#!/usr/bin/env bash
# OpenClaw 를 **시스템 Node** 아래로 옮긴다. sudo 필요.
#
# ★ 왜 (2026-08-28)
#   지금 게이트웨이는 시스템 Node 로 **실행**되는데(`/usr/local/bin/node`),
#   실행하는 **스크립트**는 nvm 안에 있다:
#     /home/<user>/.nvm/versions/node/vXX/lib/node_modules/openclaw/dist/index.js
#   `openclaw` CLI 도 마찬가지다. **nvm 을 갈아엎으면 둘 다 죽고**, 증상은
#   "에이전트가 툴을 안 부른다" 하나뿐이라 원인을 못 찾는다
#   (`runtime/agent-gateway.md §4-10`).
#
#   절반만 옮겨져 있던 것이라, 이 스크립트가 나머지 절반을 옮긴다.
#
# ★ 되돌리는 법은 아래 §rollback 에 있다. 먼저 읽을 것.
set -euo pipefail

NODE=/usr/local/bin/node
NPM=/usr/local/bin/npm
[ -x "$NODE" ] || { echo "❌ $NODE 가 없다. 먼저: sudo bash deploy/install-system-node.sh" >&2; exit 1; }

echo "▶ 옮기기 전 상태 (되돌릴 때 필요하다 — 어딘가에 적어 둘 것)"
echo "   현재 openclaw   : $(command -v openclaw || echo 없음)"
echo "   현재 버전       : $(openclaw --version 2>/dev/null | head -1 || echo 불명)"
echo "   게이트웨이 스크립트: $(systemctl --user show openclaw-gateway -p ExecStart --value | grep -o '/[^ ;]*index.js' || echo 불명)"
echo "   시스템 Node     : $("$NODE" --version)"
echo

VER=$(openclaw --version 2>/dev/null | grep -oE '[0-9]{4}\.[0-9]+\.[0-9]+(-[0-9]+)?' | head -1)
[ -n "$VER" ] || { echo "❌ 현재 버전을 못 읽었다. 버전을 고정하지 않고 설치하지 않는다." >&2; exit 1; }

echo "▶ 같은 버전($VER)을 시스템 Node 아래로 설치한다"
echo "   ★ 버전을 고정한다 — 옮기는 김에 올리면 무엇이 원인인지 못 가른다"
"$NPM" install -g "openclaw@$VER"

echo
echo "▶ 확인"
hash -r
command -v openclaw
openclaw --version | head -1

cat <<'NEXT'

▶ 남은 일 (이 스크립트가 대신 안 한다 — 사람이 확인하며 해야 한다)

  1. 게이트웨이 유닛을 다시 만든다 (이제 시스템 경로를 잡는다):
       openclaw gateway install --force
       bash Life_Trainer/deploy/use-system-node.sh
       systemctl --user restart openclaw-gateway

  2. smoke test — 셋 다 돼야 옮긴 것이다:
       systemctl --user show openclaw-gateway -p ExecStart --value | grep -c '/.nvm/'   # 0
       Life_Trainer/.venv/bin/lt doctor | grep 에이전트                                  # WARN 없음
       openclaw agent --agent lifetrainer --message "오늘 계획이 뭐야?"                   # 답이 온다

  3. Slack DM 으로 자연어 한 번 — 위임 경로가 실제로 도는지

▶ §rollback — 위 어느 단계든 실패하면

     sudo /usr/local/bin/npm uninstall -g openclaw
     # nvm 쪽은 지우지 않았으므로 그대로 남아 있다
     openclaw gateway install --force
     systemctl --user restart openclaw-gateway

  ★ nvm 설치본을 **먼저 지우지 않는 이유가 이것이다.** 둘이 공존하는 동안에만
    되돌릴 수 있다. 시스템 쪽이 며칠 멀쩡히 돈 뒤에 nvm 쪽을 지운다.
NEXT
