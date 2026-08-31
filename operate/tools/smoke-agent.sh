#!/usr/bin/env bash
# 업그레이드 **뒤에** 에이전트 경로가 아직 성립하는지 본다.
#
# ★ 왜 doctor·verify-boot 로 부족한가
#   둘 다 *"지금 되나"* 를 묻는다. 이 스크립트는 *"올린 뒤에도 되나"* 를 묻는다 —
#   그 둘이 갈리는 자리가 **npm 전역 설치**다. `npm install -g openclaw@latest` 는
#   유닛도 안 건드리고 헬스체크도 안 깨뜨리면서 배선을 바꿀 수 있다:
#     · 사용자 셸의 `npm -g` 는 **nvm 쪽**을 가리킨다 (nvm 이 prefix 를 깐다).
#       거기 깔면 시스템 쪽 openclaw 는 그대로라 **두 판이 공존**하고,
#       무엇이 불리는지는 PATH 가 정한다 (CLAUDE.md §2 가 적은 그 함정)
#     · `openclaw gateway install` 이 유닛 본체를 다시 만든다. 드롭인만 살아남는다
#
# ★ 이 검사는 언제 우나 (CLAUDE.md §1)
#   **평소에는 안 돈다.** 알림도 타이머도 아니고, 사람이 업그레이드한 뒤 부르는 관문이다.
#   그래서 "언제 꺼지나" 가 아니라 "언제 부르나" 가 답이다 — `make smoke`.
#   고정본과 실물이 같아지면 조용하다.
#
# 쓰기:  make smoke          기기만 본다 (모델 호출 없음 · 몇 초)
#        make smoke-live     + 실제로 한 턴 돌려 툴이 불리는지 본다 (30~100초)
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

LIVE=0; [ "${1:-}" = "--live" ] && LIVE=1
MANIFEST=operate/npm-globals.txt
AGENT_ID=lifetrainer
fail=0
ok()  { echo -e "  \033[32m✅\033[0m $*"; }
bad() { echo -e "  \033[31m❌\033[0m $*"; fail=1; }
wr()  { echo -e "  \033[33m⚠️\033[0m  $*"; }

echo "▶ 1. 실물 — 경로가 아니라 **무엇이 실행되는가**"
oc=$(command -v openclaw 2>/dev/null || true)
[ -n "$oc" ] || { bad "openclaw 이 PATH 에 없다"; exit 1; }
ocreal=$(readlink -f "$oc")
case "$ocreal" in
  *nvm*) bad "PATH 의 openclaw 이 **nvm 쪽**이다 — $ocreal" ;;
  *)     ok "openclaw  $ocreal" ;;
esac

pid=$(systemctl --user show openclaw-gateway -p MainPID --value 2>/dev/null)
if [ -n "$pid" ] && [ "$pid" != 0 ]; then
  exe=$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)
  case "$exe" in
    *nvm*) bad "게이트웨이가 **nvm node** 로 돌고 있다 — $exe" ;;
    "")    wr  "게이트웨이 실행 파일을 못 읽었다 (권한)" ;;
    *)     ok  "게이트웨이 런타임  $exe" ;;
  esac
else bad "게이트웨이가 안 떠 있다"; fi

echo
echo "▶ 2. 고정본 대조 ($MANIFEST)"
if [ ! -f "$MANIFEST" ]; then
  bad "$MANIFEST 이 없다 — make lock"
else
  # 시스템 prefix 절만 읽는다. nvm 절의 판을 비교하면 늘 어긋난다.
  want=$(awk '/^\[\/usr\/local\]/{f=1;next} /^\[/{f=0} f && /^openclaw@/{print;exit}' "$MANIFEST")
  have=$(openclaw --version 2>/dev/null | grep -oE '[0-9]{4}\.[0-9]+\.[0-9]+(-[0-9]+)?' | head -1)
  if [ -z "$want" ]; then bad "고정본에 시스템 openclaw 항목이 없다"
  elif [ "openclaw@$have" = "$want" ]; then ok "openclaw@$have — 고정본과 같다"
  else bad "설치본 openclaw@$have ≠ 고정본 $want — 올렸으면 \`make lock\` 으로 기록할 것"; fi
fi

echo
echo "▶ 3. 유닛·드롭인·백엔드"
echo "   (verify-boot 에게 맡긴다 — 같은 것을 두 곳에서 판정하지 않는다)"
if bash operate/tools/verify-boot.sh >/tmp/smoke-vb.$$ 2>&1; then
  ok "verify-boot 통과"
else
  bad "verify-boot 실패 — 아래"; sed 's/^/     /' /tmp/smoke-vb.$$ | grep -E '❌|⚠️' || true
fi
rm -f /tmp/smoke-vb.$$

if [ "$LIVE" = 1 ]; then
  echo
  echo "▶ 4. ★ 실제로 한 턴 — **툴이 불리는가**"
  echo "   (읽기 전용 질문이다. 계획을 쓰지 않는다)"
  key="agent:$AGENT_ID:smoke-$$"
  out=$(PATH=/usr/local/bin:$PATH timeout 180 openclaw agent --agent "$AGENT_ID" \
          --session-key "$key" --message "오늘 계획이 뭐야?" --json 2>/dev/null || true)
  tools=$(printf '%s' "$out" | life-trainer/.venv/bin/python -c '
import json,sys
try:
    d=json.load(sys.stdin)["result"]["meta"]["toolSummary"]
    print(" ".join(d.get("tools") or []))
except Exception: pass' 2>/dev/null)
  if [ -n "$tools" ]; then ok "툴 호출됨 — $tools"
  elif [ -z "$out" ]; then bad "응답이 없다 (시간 초과이거나 게이트웨이가 안 받았다)"
  else
    # ★ 이건 모델의 실패 모드지 배선 고장이 아닐 수 있다 (agent-gateway.md §4-6).
    #   그래서 실패가 아니라 경고다 — 한 번 더 돌려 보고도 같으면 배선을 의심한다.
    wr "툴을 안 불렀다. 배선이 아니라 **모델이 그냥 답한** 경우일 수 있다 (§4-6)"
    printf '%s' "$out" | head -c 200 | sed 's/^/     /'; echo
  fi
fi

echo
[ "$fail" -eq 0 ] && echo "  ★ 에이전트 경로 성립" || echo "  ★ 실패 있음 — 위를 볼 것"
exit "$fail"
