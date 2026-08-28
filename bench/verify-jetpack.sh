#!/bin/bash
# ─────────────────────────────────────────────────────────────
# JetPack 설치 후 검증 — sudo 불필요
# 실행: bash bench/verify-jetpack.sh
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
# ★ 정규식이 'R36' 과 'REVISION' 사이를 한 글자로 가정하고 있었다. 실제로는
#   "# R36 (release), REVISION: 5.0, GCID: ..." 이라 매칭이 조용히 실패해
#   environment.md 가 "L4T: " 를 빈칸으로 싣고 있었다 (2026-08-28).
l4t=$(sed -n '1s/.*\bR\([0-9]\+\).*REVISION: \([0-9.]\+\).*/R\1.\2/p' /etc/nv_tegra_release 2>/dev/null)
[ -n "$l4t" ] && ok "L4T (nv_tegra_release)" "$l4t" || ng "L4T (nv_tegra_release)" "읽기 실패"

# ★ JetPack 메타패키지와 L4T 구성요소는 같은 릴리스로 움직여야 한다.
#   어긋난 채로 두면 부트로더·커널·유저스페이스가 섞인다. hold 도 같이 본다 —
#   hold 는 의도일 수 있으나 **기록 없는 hold** 는 다음 사람에게 사고다.
core=$(dpkg-query -W -f='${Version}' nvidia-l4t-core 2>/dev/null)
cand=$(apt-cache policy nvidia-l4t-core 2>/dev/null | awk '/Candidate:/{print $2}')
[ -n "$core" ] && echo "     nvidia-l4t-core           $core${cand:+  (APT 후보 $cand)}"
if [ -n "$core" ] && [ -n "$cand" ] && [ "$core" != "$cand" ]; then
  wr "BSP 일관성" "설치본과 APT 후보가 다르다 — 유지보수 창에서 한 릴리스로 통일할 것"
fi
held=$(apt-mark showhold 2>/dev/null | grep -c . )
[ "${held:-0}" -gt 0 ] && wr "패키지 hold" "$(apt-mark showhold 2>/dev/null | tr '\n' ' ')— hold 이유가 기록돼 있는지 확인"

echo
echo "▶ 2. CUDA"
if command -v nvcc >/dev/null 2>&1; then
  ok "nvcc" "$(nvcc --version | grep -oE 'release [0-9.]+' | head -1)"
else
  ng "nvcc" "PATH에 없음 — 아래 §PATH 설정 참조"
fi
[ -d /usr/local/cuda ] && ok "/usr/local/cuda" "→ $(readlink -f /usr/local/cuda | sed 's|.*/||')" \
                       || ng "/usr/local/cuda" "없음"
# ★ 전에는 find 로 파일을 찾았는데 /usr/local/cuda/lib64 가 **심링크**라
#   find 가 그 아래로 안 내려갔다. 넷 다 설치돼 있는데 environment.md 가
#   "❌ 없음" 을 싣고 있었다 (2026-08-28). 판정 근거를 "파일이 보이나" 에서
#   **"링커가 찾나"** 로 옮긴다 — 실제로 링크되는 것이 그것이기 때문이다.
for l in libcudart libcublas libcufft libcurand; do
  f=$(ldconfig -p 2>/dev/null | awk -F'=> ' -v n="$l" '$0 ~ n"\\.so" {print $2; exit}')
  [ -z "$f" ] && f=$(find -L /usr/local/cuda/lib64 /usr/lib/aarch64-linux-gnu \
                       -maxdepth 1 -name "$l.so*" 2>/dev/null | head -1)
  [ -n "$f" ] && ok "$l" "$(basename "$f")" || ng "$l" "ldconfig·경로 어디에도 없음"
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
# ★ 글롭이 /dev/nvhost-nvdla* 라 실제 노드인 nvhost-ctrl-nvdla0/1 을 못 잡아
#   "DLA 없음" 을 싣고 있었다 (2026-08-28). 그리고 **노드 존재는 가용성이 아니다** —
#   여기서는 관측된 것만 적고, 쓸 수 있는지는 아래 안내대로 따로 재야 한다.
n=0
for d in /dev/nvhost*nvdla*; do [ -e "$d" ] && { ok "DLA 노드" "$d"; n=$((n+1)); }; done
[ $n -eq 0 ] && ng "DLA 노드" "/dev/nvhost*nvdla* 없음"
f=$(ldconfig -p 2>/dev/null | awk -F'=> ' '/libcudla\.so/{print $2; exit}')
[ -n "$f" ] && ok "libcudla" "$(basename "$f")" || ng "libcudla" "없음"
if [ $n -gt 0 ]; then
  echo "     → 노드 ${n}개와 런타임이 있다. **쓸 수 있는지는 아직 모른다** —"
  echo "       trtexec --onnx=<model> --useDLACore=0 --allowGPUFallback 로 확인할 것"
fi

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
# ★ compatible 첫 줄은 "nvidia,p3768-0000+p3767-0000" — **캐리어보드+모듈**이다.
#   앞에서 자르면 캐리어(p3768)를 모듈로 착각한다. '+' 없는 줄이 모듈이다.
module=$(tr '\0' '\n' < /proc/device-tree/compatible 2>/dev/null \
         | grep -v '+' | grep -oE 'p[0-9]{4}-[0-9]{4}' | head -1)
case "$c" in
  *_super.conf) ok "Super Mode" "활성화됨 (conf: $c)" ;;
  *)
    # ★ 전에는 무조건 "아래 §Super Mode 참조" 로 보내고 푸터가 활성화 절차를 찍었다.
    #   이 보드(p3767-0000)에서는 **이미 실측으로 안 된다고 결론난 것**이라
    #   절차를 노출하면 다음 사람이 같은 것을 다시 시도한다.
    if [ "$module" = "p3767-0000" ]; then
      wr "Super Mode" "이 보드에서는 불가 — 부팅마다 되돌려진다 (research/hardware.md)"
    else
      wr "Super Mode" "비활성 — 아래 §Super Mode 참조 (모듈 ${module:-불명})"
    fi ;;
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
EOF

# ★ 활성화 절차는 **이 보드에서 안 된다고 결론나지 않은 경우에만** 찍는다.
#   p3767-0000 에서는 conf 를 바꿔도 부팅마다 되돌려지고 하드웨어 과전류 보호가
#   25W 로 잡힌다는 것이 이미 실측돼 있다. 절차를 남겨두면 같은 시도를 반복하게 된다.
if [ "$module" = "p3767-0000" ]; then
cat <<'EOF'

 §Super Mode — 이 보드에서는 불가 (실측 결론, 재시도하지 말 것)

   conf 심링크를 바꿔도 nvpower.sh 가 부팅마다 되돌리고,
   하드웨어 과전류 보호도 25W 로 설정된다.
   근거와 재현 경위: research/hardware.md
════════════════════════════════════════════════
EOF
else
cat <<'EOF'

 §Super Mode 활성화 (100 → 157 TOPS) — 이 보드에서는 미검증

   sudo ln -sf /etc/nvpmodel/nvpmodel_<module>_super.conf /etc/nvpmodel.conf
   sudo nvpmodel -m 0          # MAXN_SUPER
   sudo reboot
   sudo jetson_clocks          # 재부팅 후 클럭 고정
   tegrastats                  # 부하 시 tj 온도 확인
════════════════════════════════════════════════
EOF
fi
