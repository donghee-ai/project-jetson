#!/usr/bin/env bash
# 하루 한 번 앱·기기를 점검하고, **상태가 바뀌었을 때만** 알린다.
#
# ★ 알림 설계에서 어려운 쪽은 "언제 울리나" 가 아니라 **"언제 안 울리나"** 다.
#   이 저장소는 오늘 하루에만 그 함정을 세 번 밟았다:
#     · 큐 실패율이 고쳐진 뒤에도 2주 동안 안 꺼졌다 (issues/0016)
#     · verify-boot 이 "옳지만 아직 증명 못 한" 상태를 실패로 셌다
#     · host-status 가 같은 OOM 을 두 번 세어 거짓 경보를 냈다
#   2026-08-31 에 **네 번째**를 밟았다 — 같은 검사가 WARN → FAIL 로 올라간 것을
#   "해소 ⚠️ OOM" 과 "신규 ❌ OOM" 두 줄로 냈다. 줄 전체를 `diff` 로 비교했기
#   때문인데, 읽는 사람에게는 **하나가 풀리고 다른 하나가 생긴 것**으로 읽힌다.
#   같은 사건을 두 번 세는 것과 같은 얼굴이다 → 이제 **이름을 키로** 비교한다.
#   상시 켜진 경보는 경보가 아니다 — 사람이 그 채널을 끄기 때문이다.
#
#   그래서 판정을 **상태 전이**로 한다:
#     FAIL      → 항상 알린다 (드물어야 하는 일)
#     WARN 변화 → 알린다 (새로 생겼거나 사라졌을 때)
#     WARN 유지 → **안 알린다** (이미 아는 것. 알려진 WARN 은 issues/ 에 있다)
#     전부 OK   → 안 알린다
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/project-jetson"
mkdir -p "$STATE_DIR"
PREV="$STATE_DIR/daily-check.prev"
LT=life-trainer

app=$("$LT/.venv/bin/lt" doctor 2>&1 || true)
host=$(bash operate/tools/host-status.sh 2>&1 || true)

# 항목 이름만 뽑는다 — 뒤의 숫자는 매일 바뀌므로 그걸로 비교하면 매일 "변화" 가 된다.
now_state=$(printf '%s\n%s\n' "$app" "$host" \
  | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -oE '^\s*(\[(WARN|FAIL)\]|[❌⚠️]+)\s+[^ ]+' \
  | sed -E 's/^\s+//; s/\s+/ /g' | sort -u)

fails=$(printf '%s\n' "$now_state" | grep -c 'FAIL\|❌' || true)
prev_state=$(cat "$PREV" 2>/dev/null || echo "")
printf '%s\n' "$now_state" > "$PREV"

# 이름을 키로 비교한다 — 등급(⚠️/❌)이 아니라 항목이 같은지를 본다.
# 등급만 바뀐 것은 **한 줄**로 낸다. 줄 전체를 비교하면 WARN→FAIL 이
# "해소 + 신규" 두 줄이 되어 같은 사건을 두 번 세게 된다 (2026-08-31).
state_diff() {
  awk -v prev="$1" -v now="$2" '
    function lvl(s) { return (s ~ /FAIL|\xe2\x9d\x8c/) ? 2 : 1 }
    function nm(s)  { sub(/^[^ ]+ +/, "", s); return s }
    BEGIN {
      n = split(prev, P, "\n")
      for (i = 1; i <= n; i++) if (P[i] != "") pl[nm(P[i])] = P[i]
      n = split(now, N, "\n")
      for (i = 1; i <= n; i++) if (N[i] != "") nl[nm(N[i])] = N[i]

      for (k in nl) {
        if (!(k in pl))          out[++c] = sprintf("2 신규:  %s", nl[k])
        else if (pl[k] != nl[k]) out[++c] = (lvl(nl[k]) > lvl(pl[k])) \
                                   ? sprintf("1 악화:  %s → %s", pl[k], nl[k]) \
                                   : sprintf("3 완화:  %s → %s", pl[k], nl[k])
      }
      for (k in pl) if (!(k in nl)) out[++c] = sprintf("4 해소:  %s", pl[k])

      # 급한 것부터. awk 의 for-in 은 순서가 없으므로 여기서 고정한다.
      for (i = 1; i <= 4; i++)
        for (j = 1; j <= c; j++)
          if (substr(out[j], 1, 1) == i "") print substr(out[j], 3)
    }'
}

# ── 자기 시험지 ───────────────────────────────────────────────────────────
#
# ★ CLAUDE.md §1: "안 울려야 하는 예제를 같이 둔다."
#   `check-docs.sh --self-test` 과 같은 취지다. 이 판정은 **안 울려야 할 때
#   안 울리는 것**이 어려운 쪽이라, 전이 다섯 가지를 표로 고정한다.
self_test() {
  local fails=0
  _case() {  # $1=이름  $2=prev  $3=now  $4=기대(줄바꿈 구분)
    local got; got=$(state_diff "$2" "$3")
    if [ "$got" = "$4" ]; then
      echo "  ✅ $1"
    else
      echo "  ❌ $1"; echo "     기대: ${4:-(없음)}"; echo "     실제: ${got:-(없음)}"
      fails=$((fails + 1))
    fi
  }

  # 2026-08-31 에 실제로 잘못 나온 것. 한 줄이어야 한다
  _case "WARN→FAIL 은 악화 한 줄" \
    '⚠️ OOM' '❌ OOM' '악화:  ⚠️ OOM → ❌ OOM'
  _case "FAIL→WARN 은 완화 한 줄" \
    '❌ OOM' '⚠️ OOM' '완화:  ❌ OOM → ⚠️ OOM'
  _case "없던 것이 생기면 신규" \
    '' '⚠️ 스왑' '신규:  ⚠️ 스왑'
  _case "있던 것이 사라지면 해소" \
    '⚠️ 스왑' '' '해소:  ⚠️ 스왑'
  # ★ 이게 이 검사의 핵심이다 — 같으면 **아무 말도 하지 않아야 한다**
  _case "그대로면 조용하다" \
    '⚠️ 스왑
⚠️ OC' '⚠️ 스왑
⚠️ OC' ''
  _case "급한 것이 위로 온다" \
    '⚠️ OOM
⚠️ 디스크' '❌ OOM' '악화:  ⚠️ OOM → ❌ OOM
해소:  ⚠️ 디스크'
  _case "[WARN] 서식도 같이 다룬다" \
    '[WARN] ActivityWatch' '[FAIL] ActivityWatch' '악화:  [WARN] ActivityWatch → [FAIL] ActivityWatch'

  echo
  if [ "$fails" -eq 0 ]; then echo "  전이 7가지 통과"; return 0
  else echo "  ❌ ${fails}건 실패"; return 1; fi
}

if [ "${1:-}" = "--self-test" ]; then self_test; exit $?; fi

changed=""
[ "$now_state" != "$prev_state" ] && changed=$(state_diff "$prev_state" "$now_state")

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
$(printf '%s\n' "$changed" | sed 's/^/  /')"
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
