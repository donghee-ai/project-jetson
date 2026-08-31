#!/usr/bin/env bash
# llama-server 의 디코드 경로를 Nsight Systems 로 프로파일한다.
#
# ## 왜 이 방식인가
#
# 두 벌을 동시에 못 올린다 — 가용 4.5GB 인데 8B 한 벌이 6.8GB 다.
# `nsys` 는 이미 도는 프로세스에 **attach 할 수 없다** (프로세스를 nsys 아래에서 띄워야 한다).
# 설령 들어가도 GPU 를 동시에 쓰면 커널 시간이 오염된다 — 이 저장소가
# `fcntl.flock` 으로 GPU 를 단일 자원으로 다루는 것과 같은 이유다.
#
# 그래서 **한 벌을 nsys 세션 아래에서 다시 띄운다.** 합성 벤치가 아니라
# 실사용 트래픽(llama-server 가 실제로 받는 요청)을 잰다.
#
# `--duration` 을 쓰지 않는다: 수집이 끝나며 앱을 죽이면 `Restart=always` 가
# 다시 띄우고, 드롭인이 살아 있으니 무한 루프가 된다.
# `launch` + `start`/`stop` 세션으로 **수집만** 켰다 끈다.
#
# ## 중간에 죽어도 원래대로 돌아온다 (trap)

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1
ROOT=$(pwd)

# ★ 세션 이름은 매번 다르게. 같은 이름이 살아 있으면
#   `Process launch is not allowed in this state.` 로 조용히 실패한다 (실측).
SESSION="llama-profile-$$"
DROPIN="$HOME/.config/systemd/user/llama-server.service.d/99-nsys.conf"
OUT="${1:-$ROOT/measure/results/decode-8b}"
SECONDS_TO_COLLECT="${2:-90}"
# ★ 기본을 node 로 둔다.
#   llama.cpp 는 CUDA Graphs 를 쓴다 — 디코드 한 스텝을 그래프로 묶어 재생한다.
#   기본 추적(graph)은 **그래프를 통째로 한 덩어리로만** 보므로 안쪽 커널이 안 잡힌다.
#   실제로 첫 측정에서 API·memcpy 는 97초가 잡혔는데 **커널은 0.43초뿐**이었다 —
#   그 0.43초는 그래프를 만들기 전 워밍업이고, 나머지 1,038회 재생은 통째로 가려졌다.
#   node 는 오버헤드가 크지만 **커널별 시간을 보려면 이것뿐이다.**
GRAPH_TRACE="${3:-node}"

log() { printf "\033[36m▶\033[0m %s\n" "$*"; }
ok()  { printf "  \033[32m✅\033[0m %s\n" "$*"; }
bad() { printf "  \033[31m❌\033[0m %s\n" "$*"; }

restore() {
  echo
  log "원래대로 되돌린다"
  nsys stop --session="$SESSION" >/dev/null 2>&1 || true
  nsys sessions list 2>/dev/null | awk '/llama-profile/{print $NF}' \
    | while read -r s; do nsys stop --session="$s" >/dev/null 2>&1 || true; done
  rm -f "$DROPIN"
  rmdir "$(dirname "$DROPIN")" 2>/dev/null || true
  systemctl --user daemon-reload
  systemctl --user restart --no-block llama-server
  for _ in $(seq 1 60); do
    curl -sf --max-time 3 127.0.0.1:8080/health >/dev/null 2>&1 && break; sleep 2
  done
  curl -sf --max-time 5 127.0.0.1:8080/health >/dev/null && ok "llama-server 복구" || bad "llama-server 가 안 돌아왔다 — operate/tools/install.sh 확인"
}
trap restore EXIT INT TERM

# ── 0. 사전 점검 ────────────────────────────────────────────────────────
log "사전 점검"
h=$(date +%H)
if [ "$h" -ge 2 ] && [ "$h" -lt 6 ]; then
  bad "야간 배치 창(02:00~05:50)이다. GPU 를 다투면 숫자가 오염된다."; exit 1
fi
ok "야간 배치 창 밖 ($(date '+%H:%M'))"
command -v nsys >/dev/null || { bad "nsys 없음"; exit 1; }
ok "nsys $(nsys --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"

# ── 1. 세션 아래에서 띄우는 드롭인 ──────────────────────────────────────
log "nsys 세션 드롭인 설치"
mkdir -p "$(dirname "$DROPIN")"
cat > "$DROPIN" <<CONF
# 임시 — measure/tools/profile-decode.sh 가 넣고 뺀다. 남아 있으면 지울 것.
[Service]
ExecStart=
ExecStart=/usr/local/bin/nsys launch --session-new=$SESSION --trace=cuda,osrt,nvtx \\
          --cuda-graph-trace=$GRAPH_TRACE --cuda-memory-usage=true $ROOT/operate/tools/llama-server-qwen3.sh
CONF
systemctl --user daemon-reload
# ★ --no-block: ExecStartPost 가 :8080 을 무한 폴링하므로 restart 가
#   TimeoutStartSec=600 까지 블록된다. 대기는 우리가 직접 한다.
systemctl --user restart --no-block llama-server

log "모델 로딩 대기 (최대 180초)"
for _ in $(seq 1 90); do
  curl -sf --max-time 3 127.0.0.1:8080/health >/dev/null 2>&1 && break; sleep 2
done
if ! curl -sf --max-time 5 127.0.0.1:8080/health >/dev/null; then
  bad "서버가 안 떴다 — nsys 오류:"
  journalctl --system --since '3 min ago' --no-pager 2>/dev/null \
    | grep -iE 'nsys' | tail -3 | sed 's/^/     /'
  exit 1
fi
ok "llama-server 가 nsys 세션 아래에서 떴다"

# ── 2. 수집 ─────────────────────────────────────────────────────────────
log "수집 시작 → $OUT.nsys-rep"
nsys start --session="$SESSION" -o "$OUT" --force-overwrite=true >/dev/null 2>&1 || { bad "start 실패"; exit 1; }

log "부하 — 디코드를 실제로 돌린다 (${SECONDS_TO_COLLECT}초)"
end=$(( $(date +%s) + SECONDS_TO_COLLECT ))
n=0
while [ "$(date +%s)" -lt "$end" ]; do
  curl -sf --max-time 60 127.0.0.1:8080/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"qwen3-8b","messages":[{"role":"user","content":"젯슨 오린 NX 의 통합 메모리 구조를 세 문장으로 설명해라."}],"max_tokens":128,"stream":false}' \
    >/dev/null 2>&1 && n=$((n+1))
done
ok "요청 $n 건 처리"

log "수집 종료"
nsys stop --session="$SESSION" >/dev/null 2>&1
sleep 3
[ -f "$OUT.nsys-rep" ] && ok "$(ls -lh "$OUT.nsys-rep" | awk '{print $9, $5}')" || bad "리포트가 안 생겼다"
