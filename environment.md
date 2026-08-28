# environment.md — 측정 환경 (자동 생성)

> `make verify` 로 다시 뽑는다. **이 저장소의 모든 수치는 아래 환경에서 잰 것이다.**
> 아래 블록은 손으로 고치지 않는다 — 마커 사이를 `bench/gen-environment.sh` 가 덮어쓴다.

<!-- verify:begin — 이 블록은 `make verify` 가 생성한다. 손으로 고치지 말 것 -->

> 생성 시각: 2026-08-29 00:09 KST

```
════════════════════════════════════════════════
 JetPack 설치 검증
════════════════════════════════════════════════

▶ 1. 패키지
  ✅ nvidia-jetpack             6.2.3+b81
  ✅ L4T (nv_tegra_release)     R36.5.2
     nvidia-l4t-core           36.5.2-20260716114719  (APT 후보 36.5.2-20260716114719)

▶ 2. CUDA
  ✅ nvcc                       release 12.6
  ✅ /usr/local/cuda            → cuda-12.6
  ✅ libcudart                  libcudart.so.12
  ✅ libcublas                  libcublas.so.12
  ✅ libcufft                   libcufft.so.11
  ✅ libcurand                  libcurand.so.10

▶ 3. cuDNN / TensorRT
  ✅ cuDNN                      libcudnn.so.9.3.0
  ✅ TensorRT                   libnvinfer.so.10
  ✅ python3 tensorrt           10.3.0

▶ 4. ★ DLA (딥러닝 가속기) — 그동안 미검증 항목
  ✅ DLA 노드                 /dev/nvhost-ctrl-nvdla0
  ✅ DLA 노드                 /dev/nvhost-ctrl-nvdla1
  ✅ libcudla                   libcudla.so.1
     → 노드 2개와 런타임이 있다. **쓸 수 있는지는 아직 모른다** —
       trtexec --onnx=<model> --useDLACore=0 --allowGPUFallback 로 확인할 것

▶ 5. 하드웨어 가속 블록 (영상 파이프라인용)
  ✅ NVDEC (영상 디코딩)   857 MHz
  ✅ NVENC (영상 인코딩)   793 MHz
  ✅ OFA (옵티컬 플로우)  780 MHz
  ✅ GPU                        918 MHz (현재 전력모드 상한)

▶ 6. 전력 모드
     현재 모드 : MAXN
     활성 conf : nvpmodel_p3767_0000.conf
  ⚠️  Super Mode                 이 보드에서는 불가 — 부팅마다 되돌려진다 (research/hardware.md)

▶ 7. 온도 / 전력
     RAM 11115/15643MB
     GR3D_FREQ 0%
     tj@58.531C
     VDD_IN 4958mW

════════════════════════════════════════════════
 §PATH 설정 (nvcc 가 안 잡히면)

   echo 'export PATH=/usr/local/cuda/bin:$PATH' >> ~/.bashrc
   echo 'export LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH' >> ~/.bashrc
   source ~/.bashrc

 §Super Mode — 이 보드에서는 불가 (실측 결론, 재시도하지 말 것)

   conf 심링크를 바꿔도 nvpower.sh 가 부팅마다 되돌리고,
   하드웨어 과전류 보호도 25W 로 설정된다.
   근거와 재현 경위: research/hardware.md
════════════════════════════════════════════════
```

<!-- verify:end -->

## 읽는 법

| 항목 | 왜 여기 있나 |
|---|---|
| **전력 모드** | 이 저장소의 성능 수치는 **전부 MAXN(`pmode:0000`)** 이다. 15W·25W 에서는 재지 않았다 |
| **Super Mode 비활성** | conf 파일은 있으나 부팅마다 되돌려지고 하드웨어 과전류 보호가 25W 로 잡힌다. **검증기가 활성화 절차를 더 이상 안내하지 않는다** — 같은 시도를 반복하지 않기 위해서다 |
| **DLA 노드** | 노드와 `libcudla` 가 **있다**. 다만 노드 존재는 가용성이 아니다 — `trtexec --useDLACore` 로 재 본 적이 없어서 이 저장소는 DLA 를 안 썼다 |
| **BSP 일관성** | 2026-08-29 에 36.5.2 로 통일했다. 커널 `hold` 의 이유(DKMS WiFi 드라이버)와 경위는 [research/hardware.md §8](research/hardware.md) |
| RAM 사용량 | 측정 시점의 값이다. LLM 가용 예산은 [README ③](README.md) 참조 |

> **2026-08-29.** L4T 를 36.5.2 로 통일하면서 커널이 5.15.199-tegra 가 됐다.
> WiFi 드라이버(DKMS `mt7601u`)는 새 커널에 자동 재빌드됐다 —
> 그 걱정이 커널을 `hold` 하고 있던 이유였다 ([research/hardware.md §8](research/hardware.md)).

> **2026-08-28 정정.** 이 문서는 오랫동안 `L4T:` 빈칸 · `libcudart` 외 3종 ❌ ·
> `DLA 없음` 을 **실측 결과로** 싣고 있었다. 셋 다 [`bench/verify-jetpack.sh`](bench/verify-jetpack.sh)
> 의 버그였다 — 정규식이 실제 형식을 못 읽었고, `find` 가 심링크 `lib64` 를 안 내려갔고,
> 글롭이 `nvhost-ctrl-nvdla*` 를 못 잡았다. **사양치와 실측이 다르다고 말하는 저장소가
> 측정 도구의 버그를 실측으로 싣고 있었다.** 같은 일이 다시 나지 않도록 이 블록은
> 이제 사람이 붙여넣지 않고 `make verify` 가 생성한다.
