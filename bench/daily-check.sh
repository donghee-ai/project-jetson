#!/usr/bin/env bash
# 하루 한 번 앱·기기를 점검하고, **상태가 바뀌었을 때만** 알린다.
#
# ★ 알림 설계에서 어려운 쪽은 "언제 울리나" 가 아니라 **"언제 안 울리나"** 다.
#   이 저장소는 오늘 하루에만 그 함정을 세 번 밟았다:
#     · 큐 실패율이 고쳐진 뒤에도 2주 동안 안 꺼졌다 (issues/0016)
#     · verify-boot 이 "옳지만 아직 증명 못 한" 상태를 실패로 셌다
#     · host-status 가 같은 OOM 을 두 번 세어 거짓 경보를 냈다
#   상시 켜진 경보는 경보가 아니다 — 사람이 그 채널을 끄기 때문이다.
#
#   그래서 판정을 **상태 전이**로 한다:
#     FAIL      → 항상 알린다 (드물어야 하는 일)
#     WARN 변화 → 알린다 (새로 생겼거나 사라졌을 때)
#     WARN 유지 → **안 알린다** (이미 아는 것. 알려진 WARN 은 issues/ 에 있다)
#     전부 OK   → 안 알린다
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/project-jetson"
mkdir -p "$STATE_DIR"
PREV="$STATE_DIR/daily-check.prev"
LT=Life_Trainer

app=$("$LT/.venv/bin/lt" doctor 2>&1 || true)
host=$(bash bench/host-status.sh 2>&1 || true)

# 항목 이름만 뽑는다 — 뒤의 숫자는 매일 바뀌므로 그걸로 비교하면 매일 "변화" 가 된다.
now_state=$(printf '%s\n%s\n' "$app" "$host" \
  | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -oE '^\s*(\[(WARN|FAIL)\]|[❌⚠️]+)\s+[^ ]+' \
  | sed -E 's/^\s+//; s/\s+/ /g' | sort -u)

fails=$(printf '%s\n' "$now_state" | grep -c 'FAIL\|❌' || true)
prev_state=$(cat "$PREV" 2>/dev/null || echo "")
printf '%s\n' "$now_state" > "$PREV"

changed=""
[ "$now_state" != "$prev_state" ] && changed=$(diff <(printf '%s\n' "$prev_state") <(printf '%s\n' "$now_state") | grep -E '^[<>]' || true)

if [ "${fails:-0}" -eq 0 ] && [ -z "$changed" ]; then
  echo "▶ 변화 없음 — 알리지 않는다"
  printf '%s\n' "$now_state" | sed 's/^/     /'
  exit 0
fi

echo "▶ 알릴 것이 있다"
msg="🩺 젯슨 일일 점검 — $(date '+%m-%d %H:%M')"
[ "${fails:-0}" -gt 0 ] && msg="$msg
❗ FAIL ${fails}건"
if [ -n "$changed" ]; then
  msg="$msg
변화:
$(printf '%s\n' "$changed" | sed 's/^</  해소: /; s/^>/  신규: /')"
fi
msg="$msg

지금 상태:
$(printf '%s\n' "$now_state" | sed 's/^/  /')

자세히: make host-status · lt doctor"

echo "$msg"
# ★ 발송은 명시적 지시가 있을 때만 (CLAUDE.md 규칙). 타이머가 --post 를 준다.
if [ "${1:-}" = "--post" ]; then
  "$LT/.venv/bin/python" - "$msg" <<'PY'
import sys
from lifetrainer.config import load_config
from lifetrainer.slackio.notify import SlackNotifier
n = SlackNotifier(load_config())
if n.enabled:
    n.post(text=sys.argv[1])
    print("  → Slack 발송")
else:
    print("  → Slack 비활성, 발송 안 함")
PY
fi
