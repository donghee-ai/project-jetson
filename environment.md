# environment.md — 측정 환경 (자동 생성)

> `make verify` 로 다시 뽑는다. **이 저장소의 모든 수치는 아래 환경에서 잰 것이다.**
> 마지막 갱신: 2026-08-28 10:33 KST

```
════════════════════════════════════════════════
 JetPack 설치 검증
════════════════════════════════════════════════

▶ 1. 패키지
  ✅ nvidia-jetpack             6.2.3+b81
     L4T: 

▶ 2. CUDA
  ✅ nvcc                       release 12.6
  ✅ /usr/local/cuda            → cuda-12.6
  ❌ libcudart                  없음
  ❌ libcublas                  없음
  ❌ libcufft                   없음
  ❌ libcurand                  없음

▶ 3. cuDNN / TensorRT
  ✅ cuDNN                      libcudnn.so.9.3.0
  ✅ TensorRT                   libnvinfer.so.10
  ✅ python3 tensorrt           10.3.0

▶ 4. ★ DLA (딥러닝 가속기) — 그동안 미검증 항목
  ❌ DLA 디바이스           /dev/nvhost-nvdla* 없음

▶ 5. 하드웨어 가속 블록 (영상 파이프라인용)
  ✅ NVDEC (영상 디코딩)   857 MHz
  ✅ NVENC (영상 인코딩)   793 MHz
  ✅ OFA (옵티컬 플로우)  780 MHz
  ✅ GPU                        918 MHz (현재 전력모드 상한)

▶ 6. 전력 모드
     현재 모드 : MAXN
     활성 conf : nvpmodel_p3767_0000.conf
  ⚠️  Super Mode                 비활성 — 아래 §Super Mode 참조

▶ 7. 온도 / 전력
     RAM 14461/15643MB
     GR3D_FREQ 0%
     tj@57.031C
     VDD_IN 4958mW

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
```

## 읽는 법

| 항목 | 왜 여기 있나 |
|---|---|
| **전력 모드** | 이 저장소의 성능 수치는 **전부 MAXN(`pmode:0000`)** 이다. 15W·25W 에서는 재지 않았다 |
| **Super Mode 비활성** | conf 파일은 있으나 디바이스 트리에 `-super` 가 없어 부팅마다 되돌려진다 |
| **DLA 없음** | `/dev/nvhost-nvdla*` 가 안 잡힌다. 이 보드에서 DLA 는 쓰지 않았다 |
| **libcudart/cublas 등 ❌** | 런타임 `.so` 를 표준 경로에서 못 찾는다는 표시다. llama.cpp 는 `/usr/local/cuda` 를 직접 링크하므로 빌드·추론에는 지장이 없다 |
| RAM 사용량 | 측정 시점의 값이다. LLM 가용 예산은 [README ③](README.md) 참조 |
