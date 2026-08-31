#!/usr/bin/env bash
# 8B 서버를 새벽에 한 번 재기동한다 — **자라는 익명 메모리를 회수하려고.**
#
# ## 왜 필요한가 (2026-08-30 18:12 실측)
#
# 전역 OOM 이 나서 커널이 `llama-server`(8B)를 죽였다. cgroup 한도가 아니라
# `CONSTRAINT_NONE, global_oom` — 기기 전체가 램+스왑을 다 썼다.
# 커널이 남긴 그 순간의 표:
#
#     llama-server 8B   RSS 6,577 + 스왑 5,719 = 12,296MB   ← 혼자 12.3GB
#     llama-embed       RSS 2,020 + 스왑     0              ← MemorySwapMax=0 이 작동
#     나머지 107개      RSS 거의 0 — 전부 스왑으로 밀려나 있었다
#     스왑              7,833 / 8,009MB (97.8%)
#
# 8B 는 시간이 지나며 자란다:
#
#     가동 15시간   약 8,600~9,000MB
#     가동 42시간        12,296MB      ← 여기서 기기가 죽었다
#
# ★ **지연은 안 는다.** `llm_call` 의 `summarize_doc` 평균이 08-24 이후 19~20초로
#   평평하고, 8B 가 9 → 12.3GB 로 자란 08-29~30 구간에도 안 올랐다.
#   `pswpin` 되읽기도 0.6% 다 — 자라는 것은 **할당됐지만 거의 안 건드리는** 페이지다.
#   그래서 "느려서" 가 아니라 **"자리를 먹어서"** 재기동한다.
#
# ★ `OOMScoreAdjust=-200` 은 못 막는다. 점수를 낮출 뿐 면제가 아니라,
#   8B 가 압도적 1위면 그래도 선택된다. 실제로 그렇게 죽었다.
#
# ## 왜 06:10 인가
#
#     05:50  lifetrainer-nightly-stop 시작 (임베딩 배치 — 실측 3~6분)
#     06:10  ← 여기
#     06:20  lifetrainer-backup
#
# 야간 창이 닫힌 뒤, 백업 전. 8B 로드는 10~30초라 백업까지 여유가 있다.
#
# ## 왜 게이트웨이도 같이 재기동하나
#
# `openclaw-gateway.service` 가 `Requires=llama-server.service` 다. 8B 를 재기동하면
# 게이트웨이가 같이 내려가는데, 의존이 끊겨 내려간 것은 `Restart=always` 가
# 반드시 되살려 주지 않는다. **명시적으로 올린다.**
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/project-jetson"
mkdir -p "$STATE_DIR"
SIZES="$STATE_DIR/llama-server-size.csv"

# ── 재기동 전 크기를 남긴다 ────────────────────────────────────────────────
#
# ★ 이 줄이 이 스크립트의 절반이다. 08-30 사고 때 8B 의 크기 이력이 **한 점밖에
#   없었다** — OOM 이 나서 커널이 표를 남긴 그 순간뿐이었다. heartbeat 는 헬스만
#   보고 host-status 는 `free` 를 화면에 찍고 버린다. 매일 한 점씩 쌓아 두면
#   "선형으로 자라나 / 수렴하나" 를 다음에는 물어볼 수 있다.
pid=""
for p in /proc/[0-9]*; do
  [ -r "$p/cmdline" ] || continue
  grep -qa 'Qwen3-8B' "$p/cmdline" 2>/dev/null || continue
  [ "$(cat "$p/comm" 2>/dev/null)" = "llama-server" ] || continue
  pid="$(basename "$p")"; break
done

if [ -n "$pid" ]; then
  rss=$(awk '/^VmRSS:/{printf "%.0f", $2/1024}' "/proc/$pid/status")
  swp=$(awk '/^VmSwap:/{printf "%.0f", $2/1024}' "/proc/$pid/status")
  up=$(ps -o etimes= -p "$pid" | tr -d ' ')
  [ -s "$SIZES" ] || echo "when,uptime_sec,rss_mb,swap_mb,total_mb" > "$SIZES"
  echo "$(date -Is),${up},${rss},${swp},$((rss + swp))" >> "$SIZES"
  echo "  재기동 전: RSS ${rss}MB + 스왑 ${swp}MB = $((rss + swp))MB (가동 $((up / 3600))시간)"
else
  echo "  ⚠️ 8B 프로세스를 못 찾았다 — 크기 기록을 건너뛴다"
fi

# ── 재기동 ────────────────────────────────────────────────────────────────
echo "  llama-server 재기동…"
systemctl --user restart llama-server.service || { echo "  ❌ 실패"; exit 1; }

# llama-server.service 의 ExecStartPost 가 /health 200 까지 기다리므로
# 여기 도달했으면 모델은 이미 서빙 가능하다. 게이트웨이를 그 뒤에 올린다.
echo "  openclaw-gateway 재기동…"
systemctl --user restart openclaw-gateway.service || echo "  ⚠️ 게이트웨이 재기동 실패 — 확인 필요"

if [ -n "$pid" ]; then
  sleep 2
  for p in /proc/[0-9]*; do
    [ -r "$p/cmdline" ] || continue
    grep -qa 'Qwen3-8B' "$p/cmdline" 2>/dev/null || continue
    [ "$(cat "$p/comm" 2>/dev/null)" = "llama-server" ] || continue
    awk '/^VmRSS:/{printf "  재기동 후: RSS %.0fMB\n", $2/1024}' "$p/status"
    break
  done
fi

echo "  기록: $SIZES"
