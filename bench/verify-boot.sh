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

# ★ 목록을 여기 안 적는다. Life_Trainer/systemd/desired-state.txt 하나가 정본이고
#   install-units.sh · uninstall-units.sh 도 같은 파일을 읽는다. 전에는 세 곳이
#   각자 목록을 들고 있어 서로 달랐다 — 그래서 설치 스크립트로 세운 기기가
#   지금 도는 기기와 달랐고, 이 스크립트는 그 차이를 못 봤다 (2026-08-28).
# shellcheck source=../Life_Trainer/scripts/_desired-state.sh
. Life_Trainer/scripts/_desired-state.sh

echo "▶ 1. 서비스 — Life Trainer 소유 (desired-state.txt 와 대조)"
mapfile -t WANT < <(lt_desired_units Life_Trainer)
for u in "${WANT[@]}"; do
  case "$u" in *.timer) continue;; esac        # 타이머는 §5 에서 따로 본다
  s=$(systemctl --user is-active "$u" 2>&1)
  [ "$s" = active ] && ok "$u" || bad "$u — $s"
done
# desired 에 없는데 도는 것도 본다 — 손으로 켜 두고 잊은 것이 다음 사람에게 유령이 된다
while read -r u; do
  printf '%s\n' "${WANT[@]}" | grep -qxF "$u" || bad "$u — 돌고 있는데 desired-state 에 없다"
done < <(systemctl --user list-units --state=active --no-legend 'lifetrainer-*' 'llama-embed*' \
         2>/dev/null | awk '{print $1}')

echo
echo "▶ 1b. 서비스 — 공유 자산 (runtime/ 소유)"
for u in llama-server openclaw-gateway; do
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
echo "▶ 5. 타이머 (desired-state.txt 와 대조)"
# ★ 전에는 "7개 이상" 이라는 **숫자**를 봤다. 백업 타이머를 더하면 그 숫자가 틀리고,
#   타이머 하나가 조용히 빠져도 다른 게 늘면 통과한다. 이름으로 대조한다.
for u in "${WANT[@]}"; do
  case "$u" in *.timer) ;; *) continue;; esac
  systemctl --user is-active "$u" >/dev/null 2>&1 && ok "$u" || bad "$u — 안 떠 있다"
done

echo
echo "▶ 6. journal 이 이전 부팅을 갖고 있나"
# ★ 로그가 재부팅을 못 넘기면 사고를 사후에 조사할 수 없다.
#   HANDOFF §10 이 안내하는 journalctl 명령이 빈손이 된다.
if [ -d /var/log/journal ]; then
  journalctl --user -b -1 -n1 >/dev/null 2>&1 \
    && ok "이전 부팅 로그 있음" || bad "영속 저장은 켜져 있는데 이전 부팅 기록이 없다"
else
  bad "/var/log/journal 없음 — journald 가 메모리에만 쓴다. 재부팅하면 전부 사라진다"
fi

echo
[ "$fail" -eq 0 ] && echo "  ★ 재부팅 검증 통과 — Phase 6 닫힘" || echo "  ★ 실패 있음 — runtime/README.md 의 복구 절차 참조"
exit $fail
