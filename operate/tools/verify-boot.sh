#!/usr/bin/env bash
# 재부팅 뒤 서비스가 실제로 올라왔는지 확인한다.
#
# ★ 왜 필요한가: 심링크는 **프로세스가 살아 있는 동안엔 안 깨진 것처럼 보인다.**
#   `systemctl is-active` 가 active 여도 그건 지금 도는 것이지, 다음 부팅에
#   뜬다는 뜻이 아니다. 이 저장소가 이미 겪은 부류다 —
#   "살아 있다 ≠ 서빙 가능하다" (operate/notes/agent-gateway.md §4-8).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

fail=0
ok()   { printf "  \033[32m✅\033[0m %s\n" "$1"; }
bad()  { printf "  \033[31m❌\033[0m %s\n" "$1"; fail=1; }
# ★ 경고는 실패로 세지 않는다. 아직 증명할 수 없을 뿐 잘못되지 않은 상태가 있고,
#   그걸 빨간불로 만들면 신호가 죽는다 — issues/0016 이 정확히 그 부류다.
wr()   { printf "  \033[33m⚠️\033[0m  %s\n" "$1"; }

echo "▶ 부팅 후 경과: $(uptime -p)"
echo

# ★ 목록을 여기 안 적는다. life-trainer/systemd/desired-state.txt 하나가 정본이고
#   install-units.sh · uninstall-units.sh 도 같은 파일을 읽는다. 전에는 세 곳이
#   각자 목록을 들고 있어 서로 달랐다 — 그래서 설치 스크립트로 세운 기기가
#   지금 도는 기기와 달랐고, 이 스크립트는 그 차이를 못 봤다 (2026-08-28).
# shellcheck source=../life-trainer/scripts/_desired-state.sh
. life-trainer/scripts/_desired-state.sh

echo "▶ 1. 서비스 — Life Trainer 소유 (desired-state.txt 와 대조)"
mapfile -t WANT < <(lt_desired_units life-trainer)
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
echo "▶ 1b. 서비스 — 공유 자산 (operate/systemd/desired-state.txt)"
while read -r u cond; do
  case "$u" in ''|\#*) continue;; esac
  case "$cond" in
    optional=*)
      var=${cond#optional=}
      # ★ 환경변수는 **서비스**에 있다. 타이머에서 찾으면 항상 비어 있어서
      #   설정을 마쳐도 계속 "미설정" 노란불이 남는다 (실제로 그랬다).
      # ★ 그리고 `EnvironmentFile=` 은 **실행 시점에 읽히므로 `show -p Environment`
      #   에 안 나온다.** 비밀값은 저장소 밖 파일에 두는 게 맞으니(유닛은 git 에
      #   추적된다) 파일 쪽도 같이 봐야 한다. 두 번째 함정이었다.
      svc=${u%.timer}.service
      val=$(systemctl --user show "$svc" -p Environment --value 2>/dev/null | tr ' ' '\n' | grep "^$var=" | cut -d= -f2-)
      if [ -z "$val" ]; then
        while read -r ef; do
          ef=${ef%% *}; ef=${ef#-}
          [ -r "$ef" ] && val=$(grep -h "^$var=" "$ef" 2>/dev/null | tail -1 | cut -d= -f2-)
          [ -n "$val" ] && break
        done < <(systemctl --user show "$svc" -p EnvironmentFiles --value 2>/dev/null | tr ' ' '\n' | grep '^/')
      fi
      if [ -z "$val" ]; then
        # ★ 미설정을 실패로 세지 않는다. 다만 **조용히 넘어가지도 않는다** —
        #   heartbeat 가 안 도는 것과 "설정을 안 한 것" 은 다른 사실이다.
        wr "$u — $var 미설정 (바깥에서 이 기기의 죽음을 못 알아챈다)"; continue
      fi ;;
  esac
  case "$u" in
    *.timer) systemctl --user is-active "$u" >/dev/null 2>&1 && ok "$u" || bad "$u — 안 떠 있다" ;;
    *) st=$(systemctl --user is-active "$u" 2>&1); [ "$st" = active ] && ok "$u" || bad "$u — $st" ;;
  esac
done < operate/systemd/desired-state.txt

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
if [ ! -d /var/log/journal ]; then
  bad "/var/log/journal 없음 — journald 가 메모리에만 쓴다. 재부팅하면 전부 사라진다"
  echo "     고치는 법: sudo bash operate/tools/enable-persistent-journal.sh"
elif journalctl --user -b -1 -n1 >/dev/null 2>&1; then
  ok "이전 부팅 로그 있음 ($(journalctl --list-boots --no-pager 2>/dev/null | wc -l) 부팅 보관)"
else
  # ★ 영속화를 **이번 부팅 도중에** 켜면 이전 부팅이 디스크에 있을 수가 없다.
  #   설정은 옳고 증명만 아직 안 되는 상태다. 이걸 실패로 세면 다음 재부팅까지
  #   빨간불이 상수가 되고, 그러면 사람이 이 스크립트를 안 본다.
  wr "영속 저장은 켜져 있으나 이전 부팅 기록이 아직 없다 — 다음 재부팅 뒤 다시 볼 것"
fi

echo
[ "$fail" -eq 0 ] && echo "  ★ 재부팅 검증 통과 — Phase 6 닫힘" || echo "  ★ 실패 있음 — operate/README.md 의 복구 절차 참조"
exit $fail
