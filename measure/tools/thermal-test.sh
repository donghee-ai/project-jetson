#!/bin/bash
# ─────────────────────────────────────────────────────────────
# 전력모드 변경 후 발열 확인 — CPU+GPU 동시 부하
#
# 지금 걸려 있는 전력모드에서 tj 온도가 어디까지 오르는지 확인.
# 이 보드(Orin NX 16GB)의 모드는 MAXN · 25W · 15W · 10W 다 — `nvpmodel -q` 로 확인한다.
# sudo 불필요.
#
# 실행: bash measure/tools/thermal-test.sh [지속초, 기본 120]
# 중단: Ctrl+C (부하는 자동 정리됨)
#
# ── ★ 숫자의 출처를 섞지 않는다 (issues/0020) ─────────────────────────
#
# 전에는 머리말이 "40W 모드에서" 라고 적혀 있었다. **이 보드에 40W 모드는 없다**
# (Orin AGX 것을 옮겨 적은 것으로 보인다). 그리고 판정이 "tj 92C 초과 — 스로틀링
# 영역" 이라고 말했는데, **92°C 는 이 프로젝트가 정한 값**이지 칩의 한계가 아니다.
#
# 읽는 사람은 그것을 BSP 임계값으로 읽고 "아직 88°C 니 칩 기준으로 여유가 있다" 고
# 판단한다 — 그 판단의 근거가 실은 우리 자신이다.
#
# 그래서 **둘 다 찍고, BSP 값은 커널에서 실행 시점에 읽는다.** 하드코딩하면
# BSP 판올림 때 또 조용히 어긋난다.
# ─────────────────────────────────────────────────────────────
DUR=${1:-120}

# 우리가 정한 경보값. **칩의 한계가 아니다.** 보수적으로 BSP 스로틀보다 아래에 둔다.
WARN_TJ=92     # 이 위는 "식히는 쪽을 검토" — 프로젝트 판단
WATCH_TJ=85    # 이 위는 "여유가 적다"    — 프로젝트 판단

# BSP 가 말하는 값. 커널 sysfs 에서 읽는다 (없으면 빈 문자열 → "미확인" 으로 찍는다).
_trip() {  # $1=zone type, $2=trip type  →  섭씨 정수
  local z t
  for z in /sys/class/thermal/thermal_zone*; do
    [ "$(cat "$z/type" 2>/dev/null)" = "$1" ] || continue
    for t in "$z"/trip_point_*_type; do
      [ -e "$t" ] || continue
      [ "$(cat "$t" 2>/dev/null)" = "$2" ] || continue
      awk '{printf "%d", $1/1000}' "${t%_type}_temp" 2>/dev/null
      return
    done
  done
}
BSP_THROTTLE=$(_trip cpu-thermal passive)   # 실측 99 (cpu/gpu/soc 공통)
BSP_CRITICAL=$(_trip tj-thermal critical)   # 실측 104
S=$(mktemp -d)
trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$S"; echo; echo "정리 완료."; exit' INT TERM EXIT

echo "════════════════════════════════════════════════"
echo " 발열 테스트 — ${DUR}초"
echo "════════════════════════════════════════════════"
echo "  전력모드 : $(nvpmodel -q 2>/dev/null | grep 'NV Power Mode' | cut -d: -f2 | xargs)"
echo "  CPU 코어 : $(nproc)"
echo "  GPU 상한 : $(( $(cat /sys/class/devfreq/17000000.gpu/max_freq) / 1000000 )) MHz"
# ★ 우리 값과 BSP 값을 **나란히** 찍는다. 하나로 합치면 출처가 다시 사라진다.
echo "  경보값   : ${WARN_TJ}C (이 프로젝트가 정함 · 칩 한계 아님)"
echo "  BSP     : 스로틀 ${BSP_THROTTLE:-미확인}C · critical ${BSP_CRITICAL:-미확인}C (커널 sysfs)"
echo

# ── GPU 부하: 행렬곱 무한 루프
cat > "$S/burn.cu" <<'EOF'
#include <cstdio>
__global__ void k(float*a,float*b,float*c,int n){
  int i=blockIdx.x*blockDim.x+threadIdx.x;
  if(i<n){float s=0;for(int j=0;j<256;j++)s+=a[i]*b[i]+s*1.000001f;c[i]=s;}
}
int main(){int n=1<<22;float*a,*b,*c;
  cudaMalloc(&a,n*4);cudaMalloc(&b,n*4);cudaMalloc(&c,n*4);
  cudaMemset(a,1,n*4);cudaMemset(b,1,n*4);
  while(1){k<<<(n+255)/256,256>>>(a,b,c,n);cudaDeviceSynchronize();}
  return 0;}
EOF

if /usr/local/cuda/bin/nvcc -O2 -o "$S/burn" "$S/burn.cu" 2>/dev/null; then
  "$S/burn" & GPUPID=$!
  echo "  ▶ GPU 부하 시작 (PID $GPUPID)"
else
  echo "  ⚠️ GPU 부하 컴파일 실패 — CPU만 테스트"
fi

# ── CPU 부하: 전 코어
ncpu=$(nproc)
for _ in $(seq 1 "$ncpu"); do
  ( while :; do :; done ) &
done
echo "  ▶ CPU 부하 시작 ($(nproc) 코어)"
echo

printf "  %-9s %-7s %-7s %-9s %-7s %-9s\n" "경과" "tj" "GPU%" "GPUclk" "CPU평균" "전력"
echo "  ──────────────────────────────────────────────────────"

MAXTJ=0; MAXW=0; OVER_WARN=0
for t in $(seq 5 5 "$DUR"); do
  sleep 5
  L=$(timeout 3 tegrastats --interval 500 2>/dev/null | head -1)
  tj=$(echo "$L" | grep -oE "tj@[0-9.]+C" | grep -oE "[0-9.]+")
  # ★ `[0-9]+` 로 두 번 거르면 **"GR3D" 의 3 을 같이 집는다** — g 가 "3\n98" 이 되어
  #   출력 줄이 통째로 깨진다. 실제로 깨져 있었다 (2026-09-03, 돌려보고 발견).
  #   `%` 를 남긴 채 뽑아 마지막에 뗀다. tj·VDD_IN 은 앞부분에 숫자가 없어 무사했다.
  g=$(echo "$L" | grep -oE "GR3D_FREQ [0-9]+%" | grep -oE "[0-9]+%" | tr -d '%')
  w=$(echo "$L" | grep -oE "VDD_IN [0-9]+mW" | grep -oE "[0-9]+")
  clk=$(( $(cat /sys/class/devfreq/17000000.gpu/cur_freq 2>/dev/null || echo 0) / 1000000 ))
  cpu=$(echo "$L" | grep -oE "@[0-9]+," | grep -oE "[0-9]+" | awk '{s+=$1;n++} END {if(n)printf "%d",s/n; else print 0}')
  awk -v a="${tj:-0}" -v b="$MAXTJ" 'BEGIN{exit !(a>b)}' && MAXTJ=$tj
  [ "${w:-0}" -gt "$MAXW" ] 2>/dev/null && MAXW=$w
  printf "  %-9s %-7s %-7s %-9s %-7s %-9s\n" "${t}s" "${tj:-?}C" "${g:-?}%" "${clk}MHz" "${cpu:-?}MHz" "$(( ${w:-0} / 1000 ))W"
  awk -v a="${tj:-0}" -v w="$WARN_TJ" 'BEGIN{exit !(a>w)}' && OVER_WARN=1
done

echo
echo "════════════════════════════════════════════════"
echo "  최고 온도 : ${MAXTJ}C"
echo "  최대 전력 : $(( MAXW / 1000 ))W"
# ★ 판정은 **우리 경보값** 기준이다. BSP 임계값과 얼마나 떨어져 있는지를 같이 말해야
#   읽는 사람이 "누가 정한 선을 넘었는지" 를 안다.
if [ "$OVER_WARN" = "1" ]; then
  echo "  ⚠️ tj ${WARN_TJ}C 초과 — **이 프로젝트가 정한 경보값**입니다 (칩 한계 아님)."
  if [ -n "$BSP_THROTTLE" ]; then
    awk -v a="$MAXTJ" -v b="$BSP_THROTTLE" 'BEGIN{exit !(a>b)}' \
      && echo "     BSP 스로틀 ${BSP_THROTTLE}C 도 넘었습니다 — 하드웨어가 실제로 조이는 구간." \
      || echo "     BSP 스로틀 ${BSP_THROTTLE}C 까지는 아직 여유가 있습니다."
  fi
  echo "     25W 모드 또는 냉각 개선 검토"
elif awk -v a="$MAXTJ" -v w="$WATCH_TJ" 'BEGIN{exit !(a>w)}'; then
  echo "  ⚠️ tj ${WATCH_TJ}C 초과 — 여유가 적음 (이 값도 프로젝트가 정한 것)"
else
  echo "  ✅ 온도 여유 충분 (경보값 ${WARN_TJ}C · BSP 스로틀 ${BSP_THROTTLE:-미확인}C)"
fi
echo "════════════════════════════════════════════════"
