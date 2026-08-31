#!/bin/bash
# ─────────────────────────────────────────────────────────────
# 모델별 벤치마크 스위트 — 무인 실행용
#
# 여러 모델을 순차로 서버에 올려 동일 조건으로 측정하고 결과를 파일에 남긴다.
# 사람도 에이전트도 없이 끝까지 돈다.
#
# 실행: setsid bash bench/run-model-suite.sh > /dev/null 2>&1 &
# 결과: results/모델별 로그 + summary.md
# ─────────────────────────────────────────────────────────────
set -u
BIN=/home/user/llama.cpp/build/bin
MODELS_DIR=/home/user/project/project-jetson/models
PROJ=/home/user/project/project-jetson
OUT=$PROJ/results
SCRIPTS=$PROJ/scripts
mkdir -p "$OUT"

RUN_LOG="$OUT/run.log"
exec >> "$RUN_LOG" 2>&1

log(){ echo "[$(date '+%m-%d %H:%M:%S')] $*"; }

# 측정 대상: 표시명|파일명|컨텍스트
MODELS=(
  "Qwen3-8B-Q4KM|Qwen3-8B-Q4_K_M.gguf|32768"
  "Qwen3-30B-A3B-IQ2M|Qwen3-30B-A3B-IQ2_M.gguf|32768"
  "EXAONE-3.5-7.8B-Q4KM|EXAONE-3.5-7.8B-Q4_K_M.gguf|32768"
)

# ── 메모리 관리: 프로세스가 실제로 죽고 메모리가 회수될 때까지 대기
stop_server(){
  killall -q llama-server 2>/dev/null
  for _ in $(seq 1 60); do            # 최대 2분 대기
    pgrep -x llama-server >/dev/null || break
    sleep 2
  done
  pgrep -x llama-server >/dev/null && { killall -9 -q llama-server; sleep 3; }
  sync                                 # 더티 페이지 flush
  # available 이 10GB 넘게 회복될 때까지 대기 (페이지 캐시 자동 회수 유도)
  for _ in $(seq 1 30); do
    A=$(free -m | awk '/^Mem:/{print $7}')
    [ "$A" -ge 10000 ] && break
    sleep 2
  done
  log "  [메모리] 정리 후 available $(free -m | awk '/^Mem:/{print $7}') MB"
}

start_server(){   # $1=모델파일 $2=ctx
  stop_server
  # 모델 크기 + 2GB 여유가 없으면 기동 시도하지 않음 (OOM 예방)
  NEED=$(( $(stat -c%s "$MODELS_DIR/$1") / 1048576 + 2048 ))
  AVAIL=$(free -m | awk '/^Mem:/{print $7}')
  if [ "$AVAIL" -lt "$NEED" ]; then
    log "  ⚠️ 메모리 부족: 필요 ${NEED}MB / 가용 ${AVAIL}MB — 기동 중단"
    return 1
  fi
  setsid "$BIN/llama-server" -m "$MODELS_DIR/$1" \
    -ngl 99 -c "$2" --parallel 1 -fa on -ctk q8_0 -ctv q8_0 \
    --chat-template-kwargs '{"enable_thinking":false}' \
    --temp 0.7 --top-p 0.8 --top-k 20 \
    --host 127.0.0.1 --port 8080 > "$OUT/server-$1.log" 2>&1 &
  for _ in $(seq 1 90); do
    curl -sf --max-time 3 http://127.0.0.1:8080/health >/dev/null 2>&1 && return 0
    pgrep -x llama-server >/dev/null || return 1
    sleep 5
  done
  return 1
}

log "════════ 모델 스위트 시작 ════════"

# EXAONE 다운로드가 진행 중이면 완료 대기 (최대 40분)
for _ in $(seq 1 240); do
  pgrep -f "[E]XAONE-3.5-7.8B" >/dev/null 2>&1 || break
  sleep 10
done
P="$MODELS_DIR/EXAONE-3.5-7.8B-Q4_K_M.gguf.part"
if [ -f "$P" ] && [ "$(head -c4 "$P")" = "GGUF" ]; then
  mv "$P" "$MODELS_DIR/EXAONE-3.5-7.8B-Q4_K_M.gguf"
  log "EXAONE 다운로드 완료 및 검증"
elif [ -f "$P" ]; then
  log "EXAONE 다운로드 불완전 — 건너뜀"; rm -f "$P"
fi

for entry in "${MODELS[@]}"; do
  IFS='|' read -r NAME FILE CTX <<< "$entry"
  [ -f "$MODELS_DIR/$FILE" ] || { log "SKIP $NAME (파일 없음)"; continue; }

  log "──────── $NAME ────────"
  SZ=$(stat -c%s "$MODELS_DIR/$FILE" | awk '{printf "%.2f GB",$1/1073741824}')
  log "  크기 $SZ / 서버 기동 중..."
  if ! start_server "$FILE" "$CTX"; then
    log "  ❌ 기동 실패 — 로그: server-$FILE.log"; continue
  fi
  log "  기동 완료. 메모리: $(free -m | awk '/^Mem:/{print $3"/"$2" MB 사용, 여유 "$7"MB"}')"

  # ① 품질 + 얕은 깊이 (7단계 대화)
  log "  [1/3] 품질 스위트..."
  timeout 1800 python3 "$SCRIPTS/chat-bench.py" > "$OUT/$NAME.quality.txt" 2>&1
  log "        완료 (exit $?)"

  # ② 컨텍스트 깊이 곡선
  log "  [2/3] 깊이 곡선..."
  timeout 3600 python3 "$SCRIPTS/deep-context-bench.py" 1,4,8,16,24,40,56,80 \
    > "$OUT/$NAME.depth.txt" 2>&1
  log "        완료 (exit $?)"

  # ③ 툴 콜링 (에이전트 적합성)
  log "  [3/3] 툴 콜링..."
  timeout 300 python3 "$SCRIPTS/tool-bench.py" > "$OUT/$NAME.tools.txt" 2>&1
  log "        완료 (exit $?)"

  log "  $NAME 종료. 피크 메모리: $(free -m | awk '/^Mem:/{print $3}') MB"
done

stop_server
log "════════ 전체 완료 ════════"

# 운영 서버 복구 (30B, 외부 접속 허용)
if [ -f "$MODELS_DIR/Qwen3-30B-A3B-IQ2_M.gguf" ]; then
  setsid "$BIN/llama-server" -m "$MODELS_DIR/Qwen3-30B-A3B-IQ2_M.gguf" \
    -ngl 99 -c 32768 --parallel 1 -fa on -ctk q8_0 -ctv q8_0 \
    --chat-template-kwargs '{"enable_thinking":false}' \
    --temp 0.7 --top-p 0.8 --top-k 20 \
    --host 0.0.0.0 --port 8080 > /home/user/llama-server.log 2>&1 &
  log "운영 서버(30B) 복구 — http://100.64.0.2:8080"
fi
log "결과: $OUT/"
