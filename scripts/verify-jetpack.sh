#!/bin/bash
# ─────────────────────────────────────────────────────────────
# JetPack 설치 후 검증 — sudo 불필요
# 실행: bash scripts/verify-jetpack.sh
# ─────────────────────────────────────────────────────────────
CUDA_BIN=/usr/local/cuda/bin
[ -d "$CUDA_BIN" ] && export PATH="$CUDA_BIN:$PATH"

ok(){ printf "  \033[32m✅\033[0m %-26s %s\n" "$1" "$2"; }
ng(){ printf "  \033[31m❌\033[0m %-26s %s\n" "$1" "$2"; }
wr(){ printf "  \033[33m⚠️\033[0m  %-26s %s\n" "$1" "$2"; }

echo "════════════════════════════════════════════════"
echo " JetPack 설치 검증"
echo "════════════════════════════════════════════════"

echo
echo "▶ 1. 패키지"
v=$(dpkg -l nvidia-jetpack 2>/dev/null | awk '/^ii/{print $3}')
[ -n "$v" ] && ok "nvidia-jetpack" "$v" || ng "nvidia-jetpack" "미설치"
echo "     L4T: $(head -1 /etc/nv_tegra_release 2>/dev/null | grep -oE 'R[0-9]+ .REVISION: [0-9.]+' | tr -d ',')"

echo
echo "▶ 2. CUDA"
if command -v nvcc >/dev/null 2>&1; then
  ok "nvcc" "$(nvcc --version | grep -oE 'release [0-9.]+' | head -1)"
else
  ng "nvcc" "PATH에 없음 — 아래 §PATH 설정 참조"
fi
[ -d /usr/local/cuda ] && ok "/usr/local/cuda" "→ $(readlink -f /usr/local/cuda | sed 's|.*/||')" \
                       || ng "/usr/local/cuda" "없음"
for l in libcudart libcublas libcufft libcurand; do
  f=$(find /usr/local/cuda/lib64 /usr/lib/aarch64-linux-gnu -name "$l.so*" 2>/dev/null | head -1)
  [ -n "$f" ] && ok "$l" "$(basename $f)" || ng "$l" "없음"
done

echo
echo "▶ 3. cuDNN / TensorRT"
f=$(find /usr/lib/aarch64-linux-gnu -name "libcudnn.so.*" 2>/dev/null | head -1)
[ -n "$f" ] && ok "cuDNN" "$(basename $f)" || ng "cuDNN" "없음"
f=$(find /usr/lib/aarch64-linux-gnu -name "libnvinfer.so.*" 2>/dev/null | head -1)
[ -n "$f" ] && ok "TensorRT" "$(basename $f)" || ng "TensorRT" "없음"
python3 -c "import tensorrt; print(tensorrt.__version__)" 2>/dev/null \
  | xargs -I{} bash -c 'printf "  \033[32m✅\033[0m %-26s %s\n" "python3 tensorrt" "{}"' \
  || wr "python3 tensorrt" "import 실패 (python 바인딩 별도 확인)"

echo
echo "▶ 4. ★ DLA (딥러닝 가속기) — 그동안 미검증 항목"
n=0
for d in /dev/nvhost-nvdla*; do [ -e "$d" ] && { ok "DLA 디바이스" "$d"; n=$((n+1)); }; done
[ $n -eq 0 ] && ng "DLA 디바이스" "/dev/nvhost-nvdla* 없음"
[ $n -gt 0 ] && echo "     → DLA ${n}기 사용 가능. GPU와 동시 구동 가능"

echo
echo "▶ 5. 하드웨어 가속 블록 (영상 파이프라인용)"
for d in /sys/class/devfreq/*/; do
  n=$(basename "$d")
  case "$n" in
    *nvdec*) ok "NVDEC (영상 디코딩)" "$(( $(cat $d/max_freq) / 1000000 )) MHz" ;;
    *nvenc*) ok "NVENC (영상 인코딩)" "$(( $(cat $d/max_freq) / 1000000 )) MHz" ;;
    *ofa*)   ok "OFA (옵티컬 플로우)"  "$(( $(cat $d/max_freq) / 1000000 )) MHz" ;;
    *17000000.gpu*) ok "GPU" "$(( $(cat $d/max_freq) / 1000000 )) MHz (현재 전력모드 상한)" ;;
  esac
done

echo
echo "▶ 6. 전력 모드"
m=$(nvpmodel -q 2>/dev/null | grep -A1 "NV Power Mode" | tail -1)
c=$(readlink -f /etc/nvpmodel.conf | sed 's|.*/||')
echo "     현재 모드 : $(nvpmodel -q 2>/dev/null | grep 'NV Power Mode' | cut -d: -f2 | xargs)"
echo "     활성 conf : $c"
case "$c" in
  *_super.conf) ok "Super Mode" "활성화됨" ;;
  *) wr "Super Mode" "비활성 — 아래 §Super Mode 참조" ;;
esac

echo
echo "▶ 7. 온도 / 전력"
tegrastats --interval 1000 2>/dev/null | head -1 | \
  grep -oE "(RAM [0-9]+/[0-9]+MB|GR3D_FREQ [0-9]+%|tj@[0-9.]+C|VDD_IN [0-9]+mW)" | sed 's/^/     /'

cat <<'EOF'

════════════════════════════════════════════════
 §PATH 설정 (nvcc 가 안 잡히면)

   echo 'export PATH=/usr/local/cuda/bin:$PATH' >> ~/.bashrc
   echo 'export LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH' >> ~/.bashrc
   source ~/.bashrc

 §Super Mode 활성화 (100 → 157 TOPS)

   sudo ln -sf /etc/nvpmodel/nvpmodel_p3767_0000_super.conf /etc/nvpmodel.conf
   sudo nvpmodel -m 0          # MAXN_SUPER
   sudo reboot
   sudo jetson_clocks          # 재부팅 후 클럭 고정
   tegrastats                  # 부하 시 tj 온도 확인 (스로틀링 여부)
════════════════════════════════════════════════
EOF
