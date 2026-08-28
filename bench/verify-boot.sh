#!/usr/bin/env bash
# 재부팅 뒤 서비스가 실제로 올라왔는지 확인한다.
#
# ★ 왜 필요한가: 심링크는 **프로세스가 살아 있는 동안엔 안 깨진 것처럼 보인다.**
#   `systemctl is-active` 가 active 여도 그건 지금 도는 것이지, 다음 부팅에
#   뜬다는 뜻이 아니다. 이 저장소가 이미 겪은 부류다 —
#   "살아 있다 ≠ 서빙 가능하다" (runtime/agent-gateway.md §4-8).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

fail=0
ok()   { printf "  \033[32m✅\033[0m %s\n" "$1"; }
bad()  { printf "  \033[31m❌\033[0m %s\n" "$1"; fail=1; }

echo "▶ 부팅 후 경과: $(uptime -p)"
echo

echo "▶ 1. 서비스"
for u in llama-server llama-embed openclaw-gateway lifetrainer-web lifetrainer-worker lifetrainer-slack; do
  s=$(systemctl --user is-active "$u" 2>&1)
  [ "$s" = active ] && ok "$u" || bad "$u — $s"
done

echo
echo "▶ 2. ★ ctx.conf 드롭인 (끊기면 조용히 KV 가 두 배가 된다)"
ctx=$(systemctl --user show llama-server -p Environment --value | tr ' ' '\n' | grep '^LLAMA_CTX=' || true)
[ "$ctx" = "LLAMA_CTX=20480" ] && ok "$ctx" || bad "${ctx:-LLAMA_CTX 없음} — 기본값 40960 으로 돌아갔다"

echo
echo "▶ 3. 끊긴 심링크"
d=$(find ~/.config/systemd/user -xtype l 2>/dev/null | wc -l)
[ "$d" -eq 0 ] && ok "없음" || { find ~/.config/systemd/user -xtype l -printf '     %p -> %l\n'; bad "$d 개"; }

echo
echo "▶ 4. 헬스 엔드포인트"
curl -sf --max-time 10 127.0.0.1:8080/health >/dev/null && ok ":8080 llama-server" || bad ":8080"
curl -sf --max-time 10 127.0.0.1:8081/health >/dev/null && ok ":8081 llama-embed"  || bad ":8081"

echo
echo "▶ 5. 타이머"
n=$(systemctl --user list-timers --no-legend 2>/dev/null | grep -c lifetrainer || true)
[ "$n" -ge 7 ] && ok "lifetrainer 타이머 $n 개" || bad "타이머 $n 개 (7개여야 한다)"

echo
[ "$fail" -eq 0 ] && echo "  ★ 재부팅 검증 통과 — Phase 6 닫힘" || echo "  ★ 실패 있음 — runtime/README.md 의 복구 절차 참조"
exit $fail
