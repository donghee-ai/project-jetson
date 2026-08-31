#!/usr/bin/env bash
# OpenClaw 를 **시스템 Node** 아래로 옮긴다.
#
# ★ 이 스크립트는 **일반 사용자로** 실행한다. sudo 로 감싸면 안 된다.
#      bash Life_Trainer/deploy/install-openclaw-system.sh      ← 이렇게
#      sudo bash ...                                            ← 이러면 실패한다
#   npm 설치만 내부에서 sudo 를 쓴다.
#
#   처음에 `sudo bash` 를 안내했다가 실패했다 (2026-08-28). sudo 아래에서는
#   root 의 PATH 라 `openclaw` 가 안 보이고, `systemctl --user` 는 사용자 버스가
#   없어 못 붙는다. **읽어야 하는 상태는 사용자 것인데 설치만 root 가 필요하다** —
#   그 둘을 한 컨텍스트로 묶은 것이 잘못이었다.
#
# ★ 왜 옮기나
#   게이트웨이는 시스템 Node 로 **실행**되는데 실행하는 **스크립트**는 nvm 안이다.
#   `openclaw` CLI 도 같다. nvm 을 갈아엎으면 둘 다 죽고, 증상은 "에이전트가 툴을
#   안 부른다" 하나뿐이다 (`runtime/agent-gateway.md §10-B`).
set -euo pipefail

[ "$(id -u)" -ne 0 ] || {
  echo "❌ root 로 실행하지 말 것 — 사용자 상태(openclaw 버전·게이트웨이 유닛)를 읽어야 한다." >&2
  echo "   그냥: bash $0" >&2; exit 1; }

NODE=/usr/local/bin/node
[ -x "$NODE" ] || { echo "❌ $NODE 없음. 먼저: sudo bash Life_Trainer/deploy/install-system-node.sh" >&2; exit 1; }

cur_bin=$(command -v openclaw || true)
cur_ver=$(openclaw --version 2>/dev/null | head -1 || true)
gw=$(systemctl --user show openclaw-gateway -p ExecStart --value 2>/dev/null | grep -o '/[^ ;]*index.js' || echo 불명)
sys_node=$("$NODE" --version)
nvm_node=$(node --version 2>/dev/null || echo 불명)

echo "▶ 옮기기 전 상태 — **되돌릴 때 필요하다. 복사해 둘 것**"
printf '   %-22s %s\n' "openclaw"        "${cur_bin:-없음}"
printf '   %-22s %s\n' "버전"            "${cur_ver:-불명}"
printf '   %-22s %s\n' "게이트웨이 스크립트" "$gw"
printf '   %-22s %s\n' "시스템 Node"     "$sys_node"
printf '   %-22s %s\n' "nvm Node"        "$nvm_node"
echo

VER=$(printf '%s' "$cur_ver" | grep -oE '[0-9]{4}\.[0-9]+\.[0-9]+(-[0-9]+)?' | head -1)
[ -n "$VER" ] || { echo "❌ 현재 버전을 못 읽었다. 버전을 고정하지 않고 설치하지 않는다." >&2; exit 1; }

# ★ 변수를 하나만 바꾸려 했는데 Node 메이저가 같이 바뀐다면 그 사실을 먼저 말한다.
if [ "${sys_node%%.*}" != "${nvm_node%%.*}" ]; then
  cat <<WARN
⚠️  Node 메이저가 같이 바뀐다: $nvm_node → $sys_node
    openclaw 버전은 $VER 로 고정하지만 **런타임이 바뀌는 것은 두 번째 변수**다.
    다만 근거가 하나 있다 — 게이트웨이는 **이미 시스템 Node 로 돌고 있다**:
        $gw
    즉 데몬 경로는 $sys_node 에서 동작이 확인됐고, 안 해 본 것은 CLI 경로다.
    아래 smoke test 의 3번이 정확히 그것을 본다.

WARN
  read -r -p "계속할까? [y/N] " a; [ "$a" = y ] || { echo "중단."; exit 0; }
fi

echo "▶ 같은 버전($VER)을 시스템 Node 아래로 설치한다"
sudo /usr/local/bin/npm install -g "openclaw@$VER"

hash -r
echo
echo "▶ 설치 확인"
printf '   %-22s %s\n' "openclaw" "$(command -v openclaw)"
printf '   %-22s %s\n' "버전"     "$(openclaw --version 2>&1 | head -1)"

cat <<'NEXT'

▶ 남은 일 (사람이 확인하며 한다)

  1. 게이트웨이 유닛을 다시 만든다 (이제 시스템 경로를 잡는다):
       openclaw gateway install --force
       bash Life_Trainer/deploy/use-system-node.sh
       systemctl --user restart openclaw-gateway

  2. smoke test — 셋 다 돼야 옮긴 것이다:
       systemctl --user show openclaw-gateway -p ExecStart --value | grep -c '/.nvm/'   # 0 이어야 한다
       Life_Trainer/.venv/bin/lt doctor | grep 에이전트                                  # WARN 없어야 한다
       openclaw agent --agent lifetrainer --message "오늘 계획이 뭐야?"                   # ★ CLI 경로 (새 Node)

  3. Slack DM 으로 자연어 한 번 — 위임이 실제로 도는지

▶ §rollback — 어느 단계든 실패하면

     sudo /usr/local/bin/npm uninstall -g openclaw
     # nvm 쪽은 안 지웠으므로 그대로 있다
     openclaw gateway install --force
     systemctl --user restart openclaw-gateway

  ★ nvm 설치본을 먼저 안 지우는 이유가 이것이다. 둘이 공존하는 동안에만 되돌릴 수
    있다. 시스템 쪽이 며칠 멀쩡히 돈 뒤에 nvm 쪽을 지운다.
NEXT
