#!/usr/bin/env bash
# operate/ 가 소유한 유닛을 사용자 systemd 에 건다.
#
# ★ 이건 **공유 자산**이다 — 벤치마크(measure/tools/*.py) · Life Trainer · OpenClaw 게이트웨이가
#   전부 :8080 의 이 서버를 쓴다. 그래서 앱이 아니라 operate/ 에 있다.
#
# ★ **무엇을 켜는지 여기 안 적는다** — `operate/systemd/desired-state.txt` 하나가 정본이다.
#   전에는 이 스크립트가 llama-server 둘만 걸고 `jetson-daily-check`·`jetson-heartbeat` 는
#   손으로 걸어 뒀다. 그래서 2026-08-31 재구조화에서 심링크를 지웠다 다시 걸 때
#   **그 둘이 빠진 채로 돌아왔다.** Life Trainer 쪽(`install-units.sh`)이 이미 목록 파일을
#   읽는 방식으로 고쳐져 있었는데 이쪽만 안 고쳤던 것이다 — 같은 실패의 반대편이다.
#
# 게이트웨이 드롭인은 여기서 안 건다 → life-trainer/deploy/install-gateway.sh
set -euo pipefail

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# ★ 유닛은 형제 폴더에 있다 (operate/systemd/). 스크립트와 같은 칸이 아니다 —
#   2026-08-31 재구조화에서 갈렸고, 한 변수로 두면 다음 이동 때 또 어긋난다.
OPERATE_DIR="$(cd "$TOOLS_DIR/.." && pwd)"
UNIT_SRC="$OPERATE_DIR/systemd"
UNIT_DIR="$HOME/.config/systemd/user"
STATE="$UNIT_SRC/desired-state.txt"

echo "== operate systemd 유닛 설치 =="
echo "유닛: $UNIT_SRC"
echo

chmod +x "$TOOLS_DIR/llama-server-qwen3.sh" "$TOOLS_DIR/restart-llama-server.sh" \
         "$TOOLS_DIR/daily-check.sh" "$TOOLS_DIR/heartbeat.sh"

mkdir -p "$UNIT_DIR" "$UNIT_DIR/llama-server.service.d"
for unit_path in "$UNIT_SRC"/*.service "$UNIT_SRC"/*.timer; do
    ln -sf "$unit_path" "$UNIT_DIR/$(basename "$unit_path")"
done
echo "심링크: $(ls -1 "$UNIT_SRC"/*.service "$UNIT_SRC"/*.timer | wc -l)개"

# ★ 이 한 줄이 없어서 조용히 망가진 적이 있다.
#   ctx.conf 링크가 끊기면 LLAMA_CTX 가 기본값 40960 으로 돌아가고
#   KV 캐시가 1.50 → 2.99 GB 로 뛰어, 8B 가 llama-embed 옆에서 CUDA 버퍼를 못 잡는다.
#   에러 메시지가 원인을 안 가리킨다.
ln -sf "$UNIT_SRC/llama-server.service.d/ctx.conf" "$UNIT_DIR/llama-server.service.d/ctx.conf"

systemctl --user daemon-reload

# desired-state.txt 의 조건: always | optional=<환경변수>
# optional 은 그 변수가 **환경 파일에** 비어 있지 않을 때만 켠다.
echo
echo "-- 기동 (desired-state.txt) --"
skipped=""
while read -r unit cond; do
    case "$unit" in ''|\#*) continue;; esac
    if [ "$cond" != "always" ]; then
        var=${cond#optional=}
        # 유닛이 읽는 환경 파일에서 찾는다. 셸 환경이 아니라 **유닛이 보는 것**이 기준이다.
        if ! grep -qE "^${var}=.+" "$HOME/.config/jetson/heartbeat.env" 2>/dev/null; then
            skipped="$skipped  ⏭  $unit  ($var 미설정)\n"; continue
        fi
    fi
    if systemctl --user enable --now "$unit" >/dev/null 2>&1; then echo "  ✅ $unit"
    else echo "  ❌ $unit"; fi
done < "$STATE"
[ -n "$skipped" ] && { echo; echo "-- 조건이 안 맞아 안 켠 것 --"; printf '%b' "$skipped"; }

# 헤드리스 부팅에서 사용자 서비스가 뜨려면 linger 가 필요하다.
# 없으면 SSH 로 로그인할 때까지 아무것도 안 뜬다.
loginctl enable-linger "$USER"

echo
echo "  LLAMA_CTX = $(systemctl --user show llama-server -p Environment --value | tr ' ' '\n' | grep LLAMA_CTX || echo '★ 안 걸림 — ctx.conf 링크를 확인할 것')"
