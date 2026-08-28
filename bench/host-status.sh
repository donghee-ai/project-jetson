#!/usr/bin/env bash
# 기기 자체의 상태. **`lt doctor` 는 애플리케이션 진단기고 이건 호스트 진단기다.**
#
# ★ 왜 나눴나 (2026-08-28)
#   `lt doctor` 17항목은 DB·AW·LLM·Slack·큐·임베딩·에이전트를 본다 — 전부 앱이다.
#   기기가 죽어가는 것(디스크 수명·OOM·발열·BSP 어긋남·서비스 재시작 반복)은
#   **아무도 안 보고 있었다.** 앱이 초록불인 채로 기기가 나빠질 수 있다.
#
#   sudo 없이 읽히는 것만 본다. 못 읽는 것은 **못 읽는다고 말한다** —
#   조용히 건너뛰면 "확인했다" 로 읽힌다.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

warn=0
ok()  { printf "  \033[32m✅\033[0m %-22s %s\n" "$1" "$2"; }
wr()  { printf "  \033[33m⚠️\033[0m  %-22s %s\n" "$1" "$2"; warn=$((warn+1)); }
bad() { printf "  \033[31m❌\033[0m %-22s %s\n" "$1" "$2"; warn=$((warn+1)); }
na()  { printf "  \033[90m—\033[0m  %-22s %s\n" "$1" "$2"; }

echo "▶ $(date '+%Y-%m-%d %H:%M %Z') · $(uptime -p)"
echo
echo "▶ 저장장치 — 단일 NVMe 에 rootfs·DB·백업·journal 이 전부 있다"
use=$(df --output=pcent / | tail -1 | tr -dc '0-9')
free_gb=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
if   [ "$use" -ge 90 ]; then bad "디스크"  "${use}% 사용 · ${free_gb}GB 여유"
elif [ "$use" -ge 80 ]; then wr  "디스크"  "${use}% 사용 · ${free_gb}GB 여유 — 백업·journal 이 같은 파티션이다"
else ok "디스크" "${use}% 사용 · ${free_gb}GB 여유"; fi
inode=$(df --output=ipcent / | tail -1 | tr -dc '0-9')   # -i 와 --output 은 같이 못 쓴다
[ "${inode:-0}" -ge 80 ] && wr "inode" "${inode}%" || ok "inode" "${inode}%"

e=$(cat /sys/fs/ext4/nvme0n1p1/errors_count 2>/dev/null)
if [ -n "$e" ]; then
  [ "$e" -eq 0 ] && ok "ext4 오류" "0" || bad "ext4 오류" "$e 건 — dmesg 확인"
else na "ext4 오류" "읽을 수 없음"; fi
# ★ SMART 는 root 가 필요하다. "확인 못 했다" 를 "괜찮다" 로 읽히게 두지 않는다.
if nvme smart-log /dev/nvme0 >/dev/null 2>&1; then
  nvme smart-log /dev/nvme0 2>/dev/null \
    | awk -F: '/percentage_used|media_errors|unsafe_shutdowns/{gsub(/^ +| +$/,"",$2); printf "  \033[32m✅\033[0m %-22s %s\n", $1, $2}'
else
  na "NVMe SMART" "root 필요 — sudo nvme smart-log /dev/nvme0 (수명·media error·unsafe shutdown)"
fi

echo
echo "▶ 메모리 — 16GB 통합. 8B(6.7GB)+임베딩+게이트웨이가 같이 산다"
free -m | awk '/^Mem:/{printf "  %-24s %s\n","가용", $7" MB / "$2" MB"}'
sw=$(free -m | awk '/^Swap:/{print $3}')
[ "${sw:-0}" -gt 2000 ] && wr "스왑" "${sw} MB 사용 — 누수가 기기를 끌어내리는 신호일 수 있다" \
                        || ok "스왑" "${sw} MB 사용"
# ★ 유닛 단위 줄만 센다. 커널 줄("llama-server invoked oom-killer")로 세면 안 된다 —
#   :8080 대화 서버와 :8081 임베딩 서버가 **같은 llama-server 바이너리**라서
#   커널 로그로는 구분이 안 된다. 처음에 그렇게 셌다가 "다른 프로세스가 죽고 있다" 는
#   거짓 경보를 냈다. 유닛 이름은 systemd 줄에만 있다.
#   ★ 그리고 `user@1000.service` 는 뺀다 — 유저 매니저가 **같은 이벤트를 한 번 더**
#     적는 것이라(같은 초, 같은 kill) 그대로 세면 정확히 2배가 되고 "다른 유닛도
#     죽는다" 는 거짓 경보가 또 난다. 두 번 같은 함정에 빠졌다.
oomlines=$(journalctl -b --no-pager 2>/dev/null \
           | grep -oE "[a-zA-Z0-9@_.-]+\.service: A process of this unit has been killed by the OOM killer" \
           | cut -d: -f1 \
           | grep -vE "^user@[0-9]+\.service$" \
           | sort | uniq -c | sort -rn)
if [ -n "$oomlines" ]; then
  total=$(echo "$oomlines" | awk '{s+=$1} END{print s}')
  other=$(echo "$oomlines" | grep -v "llama-embed" | awk '{s+=$1} END{print s+0}')
  summary=$(echo "$oomlines" | awk '{printf "%s×%s ", $2, $1}')
  # llama-embed 는 **일부러** OOM 으로 죽는다 (HISTORY 08-23) — 누수를 스왑 대신
  #   빠른 재시작으로 받는 밸브다. 다른 유닛이 죽는 것은 그것과 전혀 다른 일이다.
  [ "${other:-0}" -gt 0 ] && bad "OOM kill" "$total 회 — $summary · **llama-embed 외가 있다**" \
                          || wr "OOM kill" "$total 회 — 전부 llama-embed (누수 밸브). 횟수는 부하 지표다"
else ok "OOM kill" "없음"; fi

echo
echo "▶ 발열·전력 — 이 저장소의 모든 성능 수치가 MAXN 전제다"
for z in /sys/devices/virtual/thermal/thermal_zone*/; do
  t=$(cat "$z/type" 2>/dev/null); v=$(cat "$z/temp" 2>/dev/null)
  case "$t" in tj-thermal|cpu-thermal|gpu-thermal)
    c=$((v/1000))
    # ★ 85·92 는 **이 프로젝트가 정한 경보값**이다. BSP 의 실제 throttle 임계값과
    #   다르며 그 값은 아직 확인 안 했다 (maintenance-plan §E-3).
    if   [ "$c" -ge 92 ]; then bad "$t" "${c}C — 프로젝트 위험선"
    elif [ "$c" -ge 85 ]; then wr  "$t" "${c}C — 프로젝트 경보선"
    else ok "$t" "${c}C"; fi ;;
  esac
done
rpm=$(cat /sys/class/hwmon/hwmon*/rpm 2>/dev/null | head -1)
[ -n "$rpm" ] && ok "팬" "${rpm} RPM ($(systemctl is-active nvfancontrol 2>/dev/null))" \
              || na "팬" "RPM 을 못 읽었다"
# ★ OC 이벤트는 **부팅마다 0 으로 리셋된다.** 한 번 읽은 값으로 전원 문제를 단정하지
#   않는다 — 부하 시험 전후를 비교해야 뜻이 있다.
oc=""
for f in /sys/class/hwmon/hwmon*/oc?_event_cnt; do
  [ -r "$f" ] && oc="$oc $(basename "$f" | cut -c1-3)=$(cat "$f")"
done
if [ -n "$oc" ]; then
  echo "$oc" | grep -qE '=[1-9]' && wr "OC 이벤트" "$oc — 부하 시험 전후를 비교할 것 (부팅마다 리셋)" \
                                 || ok "OC 이벤트" "$oc (부팅 이후 누계)"
else na "OC 이벤트" "카운터 없음"; fi
mode=$(nvpmodel -q 2>/dev/null | grep 'NV Power Mode' | cut -d: -f2 | xargs)
[ "$mode" = "MAXN" ] && ok "전력 모드" "$mode" || wr "전력 모드" "$mode — 측정은 전부 MAXN 에서 났다"

echo
echo "▶ 서비스 안정성 — 'active' 하나로는 당일 반복 장애가 숨는다"
for u in llama-server llama-embed openclaw-gateway lifetrainer-worker lifetrainer-web lifetrainer-slack; do
  n=$(systemctl --user show "$u" -p NRestarts --value 2>/dev/null)
  s=$(systemctl --user is-active "$u" 2>/dev/null)
  if [ "$s" != active ]; then bad "$u" "$s"
  elif [ "${n:-0}" -ge 10 ]; then wr "$u" "active · 재시작 ${n}회"
  else ok "$u" "active · 재시작 ${n:-0}회"; fi
done

echo
echo "▶ BSP — 섞이면 부트로더·커널·유저스페이스가 따로 논다"
core=$(dpkg-query -W -f='${Version}' nvidia-l4t-core 2>/dev/null)
cand=$(apt-cache policy nvidia-l4t-core 2>/dev/null | awk '/Candidate:/{print $2}')
[ "$core" = "$cand" ] && ok "L4T" "$core" \
                      || wr "L4T" "설치 $core · 후보 $cand — research/hardware.md §8"
held=$(apt-mark showhold 2>/dev/null | tr '\n' ' ')
[ -n "$held" ] && wr "패키지 hold" "$held— 이유는 research/hardware.md §8 (DKMS mt7601u)" \
               || ok "패키지 hold" "없음"
if command -v dkms >/dev/null; then
  krel=$(uname -r)
  dkms status 2>/dev/null | grep -q "$krel" \
    && ok "DKMS" "$(dkms status 2>/dev/null | head -1) — 현재 커널에 빌드돼 있다" \
    || bad "DKMS" "현재 커널($krel)용 모듈이 없다 — WiFi 가 죽을 수 있다"
fi

echo
echo "▶ 노출 — 이 기기에는 창 제목이 든 개인 기록 DB 가 있다"
fw="없음"
systemctl is-active ufw >/dev/null 2>&1 && fw="ufw"
systemctl is-active nftables >/dev/null 2>&1 && fw="nftables"
open=$(ss -tulnH 2>/dev/null | awk '$5 ~ /^(0\.0\.0\.0|\*|\[::\]):/ {split($5,a,":"); print a[length(a)]}' | sort -un | tr '\n' ' ')
# ★ 안 쓰는 것을 끄는 것이 방화벽 규칙보다 먼저다 — 규칙은 잊히지만
#   꺼진 서비스는 잊혀도 안 열린다.
known_unused=""
for p in 111 631; do echo "$open" | grep -qw "$p" && known_unused="$known_unused $p"; done
[ "$fw" = "없음" ] && wr "방화벽" "없음 — 아래 포트가 LAN 전체에 열려 있다" || ok "방화벽" "$fw"
if [ -n "$known_unused" ]; then
  # ★ 백틱을 큰따옴표 안에 두면 **명령 치환으로 실행된다.** 처음에 그렇게 썼다가
  #   메시지 안의 harden-network.sh 가 실제로 돌았다. 안내문에는 백틱을 쓰지 않는다.
  wr "안 쓰는 포트" "$known_unused (rpcbind·CUPS) — bash bench/harden-network.sh 로 닫는다"
else ok "안 쓰는 포트" "없음"; fi
ok "전체 개방 포트" "$open"

echo
echo "▶ journal — 로그가 재부팅을 넘기나"
if [ -d /var/log/journal ]; then
  ok "영속 저장" "$(journalctl --disk-usage 2>/dev/null | grep -oE '[0-9.]+[MG]' | head -1) · 부팅 $(journalctl --list-boots --no-pager 2>/dev/null | wc -l)개 보관"
else
  bad "영속 저장" "꺼짐 — sudo bash bench/enable-persistent-journal.sh"
fi

echo
[ "$warn" -eq 0 ] && echo "  ★ 호스트 이상 없음" || echo "  ★ 확인할 것 ${warn}건"
exit 0
