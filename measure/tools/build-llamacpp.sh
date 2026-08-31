#!/bin/bash
# ─────────────────────────────────────────────────────────────
# llama.cpp CUDA 빌드 (Jetson Orin NX / SM 8.7)
#
# 사전 조건:
#   sudo apt install -y cmake ninja-build ccache libcurl4-openssl-dev
#
# 실행: bash measure/tools/build-llamacpp.sh        (sudo 불필요)
# ─────────────────────────────────────────────────────────────
set -u
SRC="${LLAMA_SRC:-$HOME/llama.cpp}"
CUDA=/usr/local/cuda
JOBS=$(nproc)

echo "════════════════════════════════════════════════"
echo " llama.cpp CUDA 빌드 — Jetson Orin NX (SM 8.7)"
echo "════════════════════════════════════════════════"

# ── 사전 점검
fail=0
for t in cmake git; do
  command -v $t >/dev/null || { echo "  ❌ $t 미설치"; fail=1; }
done
[ -x "$CUDA/bin/nvcc" ] || { echo "  ❌ nvcc 없음 ($CUDA/bin/nvcc)"; fail=1; }
[ -d "$SRC" ] || { echo "  ❌ 소스 없음: $SRC"; fail=1; }
if [ ! -f /usr/include/curl/curl.h ] && [ -z "$(ls /usr/include/*/curl/curl.h 2>/dev/null)" ]; then
  echo "  ⚠️  libcurl 헤더 없음 → LLAMA_CURL=OFF 로 빌드 (모델 -hf 자동 다운로드 불가)"
  CURL_OPT="-DLLAMA_CURL=OFF"
else
  CURL_OPT="-DLLAMA_CURL=ON"
fi
[ "$fail" = "1" ] && { echo; echo "위 항목을 먼저 해결하세요."; exit 1; }

command -v ninja >/dev/null && GEN="-G Ninja" || GEN=""
command -v ccache >/dev/null && CCACHE="-DGGML_CCACHE=ON" || CCACHE="-DGGML_CCACHE=OFF"

echo
echo "  소스     : $SRC"
echo "  커밋     : $(git -C "$SRC" log -1 --format='%h %ad' --date=short 2>/dev/null)"
echo "  CUDA     : $($CUDA/bin/nvcc --version | grep -oE 'release [0-9.]+')"
echo "  빌드 잡  : $JOBS"
echo "  제너레이터: $([ -n "$GEN" ] && echo Ninja || echo 'Unix Makefiles')"
echo "  curl     : ${CURL_OPT#-D}"

# ── 구성
echo
echo "▶ 1/3  CMake 구성"
cd "$SRC" || exit 1
rm -rf build
cmake -B build $GEN \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_ARCHITECTURES=87 \
  -DCMAKE_CUDA_COMPILER="$CUDA/bin/nvcc" \
  -DGGML_CUDA_FA=ON \
  -DGGML_CUDA_GRAPHS=ON \
  -DGGML_NATIVE=ON \
  $CCACHE $CURL_OPT 2>&1 | tail -15
[ ${PIPESTATUS[0]:-0} -eq 0 ] || { echo "  ❌ 구성 실패"; exit 1; }

# ── 빌드
echo
echo "▶ 2/3  컴파일 (수 분~20분 소요)"
START=$SECONDS
cmake --build build --config Release -j "$JOBS" 2>&1 | \
  grep -viE "^\[|warning:|note:" | tail -20
RC=${PIPESTATUS[0]:-1}
ELAPSED=$((SECONDS-START))
if [ "$RC" -ne 0 ]; then
  echo "  ❌ 빌드 실패 (${ELAPSED}초)"
  echo "  전체 로그: cd $SRC && cmake --build build -j$JOBS"
  exit 1
fi
echo "  ✅ 완료 ($((ELAPSED/60))분 $((ELAPSED%60))초)"

# ── 검증
echo
echo "▶ 3/3  검증"
B="$SRC/build/bin"
for x in llama-cli llama-bench llama-server llama-quantize; do
  [ -x "$B/$x" ] && printf "  ✅ %-16s %s\n" "$x" "$(du -h $B/$x | cut -f1)" \
                 || printf "  ❌ %-16s 없음\n" "$x"
done
echo
echo "  --- CUDA 백엔드 인식 확인 ---"
"$B/llama-bench" --help >/dev/null 2>&1 && \
  "$B/llama-cli" --version 2>&1 | head -5 | sed 's/^/  /'

cat <<EOF

════════════════════════════════════════════════
 실행 경로: $B

 다음 단계 — 모델 받기 (예: Qwen3-8B Q4_K_M, 약 4.9GB)
   mkdir -p ~/project/project-jetson/models && cd ~/project/project-jetson/models
   $B/llama-cli -hf Qwen/Qwen3-8B-GGUF:Q4_K_M -p "안녕" -n 32

 벤치마크
   $B/llama-bench -m ~/project/project-jetson/refs/models/<모델>.gguf -p 512 -n 128 -ngl 99
════════════════════════════════════════════════
EOF
