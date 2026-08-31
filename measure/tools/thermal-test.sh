#!/bin/bash
# ─────────────────────────────────────────────────────────────
# 전력모드 변경 후 발열 확인 — CPU+GPU 동시 부하
#
# 40W 모드에서 tj 온도가 어디까지 오르는지, 스로틀링이 걸리는지 확인.
# sudo 불필요.
#
# 실행: bash measure/tools/thermal-test.sh [지속초, 기본 120]
# 중단: Ctrl+C (부하는 자동 정리됨)
# ─────────────────────────────────────────────────────────────
DUR=${1:-120}
S=$(mktemp -d)
trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$S"; echo; echo "정리 완료."; exit' INT TERM EXIT

echo "════════════════════════════════════════════════"
echo " 발열 테스트 — ${DUR}초"
echo "════════════════════════════════════════════════"
echo "  전력모드 : $(nvpmodel -q 2>/dev/null | grep 'NV Power Mode' | cut -d: -f2 | xargs)"
echo "  CPU 코어 : $(nproc)"
echo "  GPU 상한 : $(( $(cat /sys/class/devfreq/17000000.gpu/max_freq) / 1000000 )) MHz"
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

MAXTJ=0; MAXW=0; THROTTLE=0
for t in $(seq 5 5 $DUR); do
  sleep 5
  L=$(timeout 3 tegrastats --interval 500 2>/dev/null | head -1)
  tj=$(echo "$L" | grep -oE "tj@[0-9.]+C" | grep -oE "[0-9.]+")
  g=$(echo "$L" | grep -oE "GR3D_FREQ [0-9]+%" | grep -oE "[0-9]+")
  w=$(echo "$L" | grep -oE "VDD_IN [0-9]+mW" | grep -oE "[0-9]+")
  clk=$(( $(cat /sys/class/devfreq/17000000.gpu/cur_freq 2>/dev/null || echo 0) / 1000000 ))
  cpu=$(echo "$L" | grep -oE "@[0-9]+," | grep -oE "[0-9]+" | awk '{s+=$1;n++} END {if(n)printf "%d",s/n; else print 0}')
  awk -v a="${tj:-0}" -v b="$MAXTJ" 'BEGIN{exit !(a>b)}' && MAXTJ=$tj
  [ "${w:-0}" -gt "$MAXW" ] 2>/dev/null && MAXW=$w
  printf "  %-9s %-7s %-7s %-9s %-7s %-9s\n" "${t}s" "${tj:-?}C" "${g:-?}%" "${clk}MHz" "${cpu:-?}MHz" "$(( ${w:-0} / 1000 ))W"
  awk -v a="${tj:-0}" 'BEGIN{exit !(a>92)}' && THROTTLE=1
done

echo
echo "════════════════════════════════════════════════"
echo "  최고 온도 : ${MAXTJ}C"
echo "  최대 전력 : $(( MAXW / 1000 ))W"
if [ "$THROTTLE" = "1" ]; then
  echo "  ⚠️ tj 92C 초과 — 스로틀링 영역. 25W 모드 또는 냉각 개선 검토"
elif awk -v a="$MAXTJ" 'BEGIN{exit !(a>85)}'; then
  echo "  ⚠️ tj 85C 초과 — 여유가 적음. 장시간 부하 시 주의"
else
  echo "  ✅ 온도 여유 충분"
fi
echo "════════════════════════════════════════════════"
