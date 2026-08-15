# Jetson Orin NX 16GB — 기기 실측 스펙 및 LLM 후보 정리

> 측정일: 2026-08-13 / 측정 방식: 대상 기기에서 직접 명령 실행
> 호스트명: `ubuntu` / 대상: reComputer J4012 (추정, 아래 §2 참조)

---

## 1. 실측 하드웨어 스펙

| 항목 | 실측값 | 확인 근거 |
|---|---|---|
| 모듈 | **Jetson Orin NX 16GB** | `/proc/device-tree/compatible` → `nvidia,p3767-0000` |
| 캐리어 보드 | `p3768-0000` 호환 (Orin 레퍼런스 캐리어 DTB) | `nvidia,p3768-0000+p3767-0000` |
| SoC | Tegra234 (T234) | `nvidia,tegra234` |
| GPU | Ampere, 1024 CUDA core / 32 Tensor core | 데이터시트 |
| CPU | Cortex-A78AE 8코어, 최대 1984 MHz | `lscpu` |
| CPU 현재 상태 | **8코어 전부 온라인** (MAXN 적용 후) | `nproc` → 8 |
| 메모리 | **16,018 MB LPDDR5 통합 메모리**, 102.4 GB/s, 128-bit | `/proc/meminfo`, 데이터시트 |
| 스왑 | 7.6 GiB **zram** (1.9G × 4), 물리 스왑 없음 | `swapon --show` |
| 스토리지 | **FORESEE XP1000F128G 128GB NVMe** → root 116G, 여유 95G | `lsblk`, `df -h` |
| OS | Ubuntu 22.04.5 LTS (jammy) | `lsb_release -a` |
| 커널 | 5.15.185-tegra | `uname -a` |
| L4T | **36.5.0** (빌드 2026-01-16) = **JetPack 6.2.x** | `/etc/nv_tegra_release` |
| 전력 모드 | **MAXN (mode 0)** — CPU 8코어 1984MHz / GPU 918MHz | `nvpmodel -q` |
| 냉각 | PWM 팬 탑재 (pwm1 = 78/255) | `hwmon0 = pwmfan` |
| 유휴 상태 | 4.6W, tj 56~60°C, GPU 306 MHz | `tegrastats` |

### 사용 가능한 전력 모드

활성 프로파일: `/etc/nvpmodel/nvpmodel_p3767_0000.conf` (**비-Super**)

| ID | 모드 | 비고 |
|---|---|---|
| **0** | **MAXN** | **현재 활성** — 상한 해제, DVFS 유지 |
| 1 | 10W | |
| 2 | 15W | 출고 기본값 |
| 3 | 25W | GPU 408 MHz로 고정 제한 — MAXN보다 불리 |

`nvpmodel_p3767_0000_super.conf` 파일은 존재하나 **디바이스 트리 제약으로 사용 불가**. §3-② 참조.

---

## 2. reComputer J4012 일치 여부 — **일치**

| 대조 항목 | J4012 사양 | 실측 | 판정 |
|---|---|---|---|
| 모듈 | Orin NX 16GB | p3767-0000 (Orin NX 16GB) | ✅ |
| 메모리 | 16GB | 16,018 MB | ✅ |
| SSD | 128GB NVMe 번들 | FORESEE XP1000F128G 128GB | ✅ |
| AI 성능 | 100 TOPS | (25W 모드 기준 사양 일치) | ✅ |

### 확인된 차이점 2가지

1. **디바이스 트리가 Seeed J401이 아닌 NVIDIA 레퍼런스 캐리어(`p3768-0000`)로 인식됨.**
   J401이 devkit 캐리어 호환 보드이므로 정상 동작이며, 소프트웨어만으로 J401임을
   100% 증명할 Seeed 고유 EEPROM ID는 없음. → **케이스 라벨로 최종 확인 필요.**
2. **JetPack 버전이 출고 상태와 다름.** J4012는 JetPack 5.1.1로 출고되나
   현 기기는 **JetPack 6.2로 재플래시**된 상태(L4T 36.5.0, 2026-01-16 빌드).

---

## 3. 셋업 (완료됨 — 2026-08-13)

### ① CUDA 스택 ✅ 설치 완료

```
nvidia-jetpack   6.2.3+b81
nvcc             release 12.6      /usr/local/cuda → cuda-12.6
CUDA 런타임      /usr/local/cuda-12.6/targets/aarch64-linux/lib/   ← lib64 아님
cuDNN            libcudnn.so.9.3.0
TensorRT         libnvinfer.so.10 / python 바인딩 10.3.0
DLA              2코어 (TensorRT 확인) — /dev/nvhost-ctrl-nvdla0, nvdla1
PVA              /dev/nvhost-ctrl-pva0
카메라 파이프라인 nvhost-ctrl-isp / vi0 / vi1 / nvcsi
```

### ② ⚠️ Super Mode는 이 기기에서 사용 불가

**원인: 디바이스 트리 compatible 문자열에 `-super`가 없음.**

```
/proc/device-tree/compatible = nvidia,p3768-0000+p3767-0000
                               nvidia,p3767-0000     ← "-super" 없음
```

`/etc/systemd/nvpower.sh`가 부팅마다 이 문자열을 읽고 conf를 강제 재선택한다.

```bash
# line 96  : "p3767-0000-super" 매칭 실패
# line 119 : machine="p3767-0000" (폴백)
# line 174 : 링크는 super인데 machine은 non-super → 불일치 감지
# line 196 : unlink /etc/nvpmodel.conf
# line 202 : rm /var/lib/nvpmodel/status      ← 저장된 모드까지 삭제
# line 255 : 비-super conf 로 재생성
```

**symlink를 수동으로 바꿔도 재부팅하면 되돌아간다.**

게다가 같은 스크립트가 SKU별로 하드웨어 과전류 보호를 설정한다 (line 649~654).

```
실측:  curr1_max  4944 mA → 25.1 W      ← p3767-0000 (비-super)
       curr1_crit 5928 mA → 30.1 W
       (super였다면 40W)
```

**conf를 억지로 바꿔도 하드웨어가 25W에서 막는다.** 소프트웨어로 우회 불가.

Super Mode를 쓰려면 `p3767-0000-super` 디바이스 트리로 **전체 재플래시**가 필요하다.
얻는 것은 GPU 918 → 1173 MHz(+28%)와 전력 예산 25 → 40W. 현재 구축한 환경
(JetPack·WiFi 드라이버·DKMS·커널 hold)이 전부 초기화되므로 **권장하지 않음**.

### ③ ✅ MAXN 적용 완료 — 현 구성에서 최선

| | 15W (출고 기본) | **MAXN (현재)** | 25W (참고) |
|---|---|---|---|
| CPU 코어 | 4 | **8** | 8 |
| CPU 상한 | 1420 MHz | **1984 MHz** | 1497 MHz |
| GPU SM | 4 (TPC 252) | **8 (TPC 240)** | 8 |
| **GPU 상한** | 612 MHz | **918 MHz** | **408 MHz** ↓ |
| 전력 제어 | 15W 소프트캡 | 하드웨어 25/30W | 25W 소프트캡 |

**25W 프로파일은 GPU를 408 MHz로 고정 제한**하므로 MAXN보다 불리하다.
MAXN은 상한만 해제하고 DVFS는 그대로 동작한다 (유휴 시 GPU 306 MHz로 하강 확인).

```bash
sudo nvpmodel -m 0    # MAXN — symlink 조작 불필요
sudo reboot
```

`machine`과 conf가 둘 다 non-super로 일치하므로 relink가 발생하지 않고
`/var/lib/nvpmodel/status`(pmode:0000)가 재부팅 후에도 유지된다.

> **`jetson_clocks`는 영구 적용하지 말 것.** DVFS를 끄고 클럭을 최대로 고정하므로
> 유휴에도 GPU가 918 MHz로 상주한다. 24시간 가동 기기에서는 낭비.
> 벤치마크 시 측정값 안정화 용도로만 사용.

### 검증 명령

```bash
bash scripts/verify-jetpack.sh    # 패키지·CUDA·DLA·전력모드 일괄 확인
bash scripts/thermal-test.sh 120  # CPU+GPU 동시 부하 발열 측정
nvpmodel -q          # MAXN 확인
lscpu | grep -i off  # 8코어 전부 온라인인지 확인
tegrastats           # 부하 시 tj 온도 모니터링
```

> ⚠️ MAXN은 하드웨어 전류 보호(25W/30W)와 열 트립(70/99/104°C)으로 제한된다.
> 부하 테스트로 온도 확인 권장: `bash scripts/thermal-test.sh 120`

---

## 4. 메모리 예산 (모델 선택의 핵심 제약)

Jetson의 16GB는 dGPU의 전용 VRAM이 아니라 **CPU/GPU 공유 통합 메모리**다.

```
MemTotal                                  16,018 MB
현재 available (GUI + 개발 세션 구동 중)   13,588 MB
  ├─ GUI 스택 (gnome-shell/Xorg/gjs)          530 MB  ← 헤드리스로 회수
  └─ 개발 세션 프로세스                     ~1,100 MB  ← 종료 시 회수

헤드리스 전환 시 확보 가능                 ~14,500 MB (14.5 GB)
CUDA 컨텍스트 + OOM 여유분 차감 후
llama.cpp 실사용 안전 예산                 ~13,500 MB (13.5 GB)  ★ 기준값
```

헤드리스 전환:
```bash
sudo systemctl set-default multi-user.target && sudo reboot
```

### 예산에 포함되는 항목

총 사용량 = **모델 가중치 + KV 캐시 + 연산 버퍼(~0.5GB) + CUDA 컨텍스트(~0.3GB)**

KV 캐시는 컨텍스트 길이에 비례하며 모델 구조(레이어 수 × KV 헤드 수)로 결정된다. (근사치)

| 모델 | 토큰당 KV (FP16) | 32K 컨텍스트 (FP16) | 32K (Q8 양자화) |
|---|---|---|---|
| Qwen3-30B-A3B (48L, KV헤드 4) | ~98 KB | ~3.2 GB | ~1.6 GB |
| Qwen3-8B (36L, KV헤드 8) | ~144 KB | ~4.7 GB | ~2.4 GB |

> 주의: 8B 덴스 모델이 30B MoE보다 KV 캐시가 **더 크다.** 긴 컨텍스트를 쓸 계획이면
> 가중치 크기만 보고 판단하면 안 된다. `--cache-type-k q8_0 --cache-type-v q8_0` 권장.

### ★ 메모리 대역폭 실측 (MAXN 기준)

사양치를 쓰지 말고 **실측값을 기준으로 계산할 것.**

```
사양치 (LPDDR5 128-bit)      102.4 GB/s
─────────────────────────────────────────
실측 순차 읽기               60.0 GB/s     ← 사양 대비 58%
실측 D2D 복사 (읽기+쓰기)    69.5 GB/s
```

측정 조건: 2 GiB 버퍼(L2 2MB 초과), float4 벡터 로드, 8 SM × 48블록 × 256스레드.
부하 중 GPU 918 MHz 도달 확인, VDD_IN 11~13W, tj 61°C — **전력·발열 제한 아님.**
단일 클라이언트(GPU)로 메모리 컨트롤러를 포화시키지 못하는 구조적 한계로 보인다.

측정 도구: `scripts/membw.cu`
```bash
/usr/local/cuda/bin/nvcc -O3 -o /tmp/membw scripts/membw.cu && /tmp/membw
```

### 속도 상한 계산

```
생성 속도 상한 ≈ 60 GB/s ÷ (토큰당 읽는 바이트)
llama.cpp 실효율 87%  ← 실측으로 확정 (초기 가정 60~80%는 보수적이었음)
```

**토큰 생성은 연산이 아니라 메모리 대역폭이 천장이다.** 이 기기 성능 예측의 유일한 기준.

| 모델 (양자화) | 디스크 | **토큰당 읽기** | tok/s |
|---|---|---|---|
| Qwen3-4B Q4_K_M | 2.5 GB | 2.5 GB | ~21 (추정) |
| **Qwen3-8B Q4_K_M** | **4.68 GB** | 4.68 GB | **11.1 ★실측** |
| Qwen3-14B Q4_K_M | 8.5 GB | 8.5 GB | ~6 (추정) |
| **Qwen3-30B-A3B IQ2_M (MoE)** | 10.9 GB | **~2.5 GB** | ~21 (미검증) |

**MoE의 우위 가설.** 총 10.9 GB를 메모리에 상주시키지만 토큰당 활성 전문가
~2.5 GB만 읽으므로 14B 덴스보다 크면서 3배 이상 빨라야 한다. 동일 하드웨어
공개 벤치마크(Qwen 3.6 35B-A3B IQ2_M → 20.8 tok/s)가 이를 뒷받침하나
**이 기기에서는 아직 미검증.** 라우팅 오버헤드로 추정치보다 낮을 수 있다.

---

### ★ 실측 벤치마크 (2026-08-14)

```
llama.cpp b1-a94d563 / CUDA / ngl=99 / flash-attn=1 / MAXN

| model                  | size     | params | test  |          t/s |
| qwen3 8B Q4_K - Medium | 4.68 GiB | 8.19 B | pp512 | 353.74 ±5.66 |
| qwen3 8B Q4_K - Medium | 4.68 GiB | 8.19 B | tg128 |  11.14 ±0.01 |

측정 중: GR3D_FREQ 97~99% / VDD_IN 20.4W / tj 67°C
```

| 지표 | 값 | 의미 |
|---|---|---|
| **pp512** (프롬프트 처리) | **353.7 tok/s** | 연산 바운드. 입력 3,000토큰 → 8.5초 |
| **tg128** (토큰 생성) | **11.14 tok/s** | 대역폭 바운드. 60 GB/s ÷ 4.68 GB의 **87%** |

**전력·발열 여유 확인.** 20.4W는 하드웨어 상한 25W 이내, 67°C는 열 트립 99°C에
한참 못 미친다. MAXN 상시 가동에 문제 없음.

#### 실무 환산 예시 (grant-radar 유형 작업)

```
공고 1건 = 입력 3,000토큰 + 출력 300토큰
  프롬프트 처리  3000 ÷ 354  =  8.5초
  토큰 생성       300 ÷ 11.1 = 27.0초
  ────────────────────────────────────
  건당 약 35초  →  하루 100건 = 약 1시간
```

#### 재현 방법

```bash
~/llama.cpp/build/bin/llama-bench -m ~/project/project-jetson/models/Qwen3-8B-Q4_K_M.gguf \
  -ngl 99 -p 512 -n 128 -fa 1 -r 2
```

> ⚠️ `llama-cli`는 `-no-cnv`가 동작하지 않아 대화형 모드로 진입한다.
> stdin이 EOF면 프롬프트를 무한 출력하므로(실제로 30MB 로그 발생)
> **벤치마크에는 반드시 `llama-bench`를 쓸 것.**

---

## 5. 모델 후보

### 5-1. 티어별 후보

> 속도 기준: 실측 대역폭 60 GB/s × 실효율 87% (§4). ★ 표시는 실측값.

| 티어 | 모델 | 양자화 / 크기 | 생성 속도 | 판정 |
|---|---|---|---|---|
| **기준선 (검증 완료)** | **Qwen3-8B** | Q4_K_M / 4.68 GB | **11.1 tok/s ★실측** | ✅ GUI 켠 채로도 구동 |
| 품질 우선 (덴스) | Qwen3-14B, Phi-4 14B, Gemma 3 12B | Q4_K_M / ~8.5 GB | ~6 tok/s | ⚠️ 느림 |
| **최대 품질 (MoE)** | Qwen3-30B-A3B | **IQ2_M / 10.9 GB** | ~21 tok/s (미검증) | ⚠️ 헤드리스 필수 |
| 실시간 / 멀티유저 | Qwen3-4B, Gemma 3 4B | Q4_K_M / ~2.5 GB | ~21 tok/s | ✅ |
| 비전 (VLM) | Gemma 3 12B, VILA / LLaVA 계열 | Q4 | — | 로봇/카메라 파이프라인용 |

**14B 덴스는 권장하지 않는다.** 4~6 tok/s는 실사용에 답답하고, 같은 메모리로
30B-A3B MoE를 올리면 3배 빠르면서 품질도 더 좋다.

### 5-2. MoE 모델이란 (A3B 표기 해설)

`Qwen3-30B-A3B` = **총 30B 파라미터 / 토큰당 활성(Activated) 3B**

각 레이어의 FFN이 128개 전문가로 분할되어 있고, 라우터가 토큰마다 8개만 선택해 통과시킨다.

| | 결정 요인 | 이 기기에서의 의미 |
|---|---|---|
| 메모리 점유 | **총** 파라미터 (30B) | 전문가 전부를 메모리에 상주시켜야 함 |
| 생성 속도 | **활성** 파라미터 (3B) | 토큰당 읽는 양이 적어 빠름 |
| 모델 품질 | 총 파라미터에 근접 | 14B 덴스보다 우수 |

같은 규칙: `Gemma 4 26B-A4B`(총 26B/활성 4B), `Llama 4 Scout 109B-A17B`(총 109B/활성 17B)

**단, 파라미터 비율(10배)만큼 빨라지지 않는다.** 체감 2~3배 수준. 전문가 선택이
토큰마다 바뀌는 랜덤 액세스라 순차 읽기 효율이 떨어지고, 어텐션 레이어는 항상 전량 계산된다.

### 5-3. Qwen3-30B-A3B 양자화별 적합성

안전 예산 **13.5 GB** 기준.

| 양자화 | 가중치 | + KV(Q8) + 버퍼 | 총합 | 판정 |
|---|---|---|---|---|
| Q4_K_M | 18.6 GB | — | — | ❌ 논외 |
| Q3_K_M | 14.7 GB | — | — | ❌ 가중치만으로 초과 |
| IQ3_XS | ~12.7 GB | 8K ctx 0.4 + 0.8 | 13.9 GB | ⚠️ 천장 초과, 사실상 불가 |
| **IQ2_M** | **10.9 GB** | 32K ctx 1.6 + 0.8 | **13.3 GB** | ✅ **유일한 현실적 선택** |
| IQ2_XXS | ~8.6 GB | 32K 1.6 + 0.8 | 11.0 GB | ⚠️ 여유롭지만 품질 붕괴 위험 |

> 동일 하드웨어(Orin NX 16GB) 공개 벤치마크에서도 Qwen 3.6 35B-A3B를 **IQ2_M**으로 구동해
> 20.8 tok/s를 기록했다. 다른 크기가 안 들어가서 그런 것이지 선택이 아니다.

### 5-4. 메모리 우회로 — 이 기기에서는 전부 불가

| 방법 | 이 기기에서 불가능한 이유 |
|---|---|
| 부분 GPU 오프로드 (`-ngl`) | CPU/GPU가 **동일 물리 메모리** 사용. 계산 주체만 바뀌고 메모리는 1바이트도 절약 안 됨 |
| `--cpu-moe` 전문가 오프로드 | 위와 동일한 이유로 무의미 |
| NVMe mmap 스트리밍 | NVMe ~2-3 GB/s vs RAM 102 GB/s. **40배 느려** 토큰 생성 사실상 정지 |
| zram 스왑 7.6 GB | 압축 RAM이라 용량을 새로 만들지 못함. 가중치가 스왑으로 가면 종료 |

**결론: 모델 전체가 메모리에 들어가야 한다. 예외 없음.**

### 5-5. IQ2_M 30B-A3B vs Q4_K_M 8B — 실무 판단

MoE는 저비트 양자화에 **상대적으로 취약하다.** 토큰당 활성 경로가 3B뿐이라 양자화 오차를
흡수할 여유(redundancy)가 덴스 모델보다 적다. 반면 총 파라미터에 담긴 지식량은 30B급이다.

| 용도 | 우세 모델 |
|---|---|
| 지식 질의, 다국어, 긴 문맥 요약 | IQ2_M 30B-A3B |
| **코딩, 정확한 지시 이행, 구조화 출력(JSON/툴콜)** | **Q4_K_M 8B** |

저비트에서 가장 먼저 무너지는 것이 포맷 준수와 정밀도이므로, **에이전트 / 툴 콜링 용도라면
8B Q4가 더 안정적일 가능성이 높다.** 벤치마크 점수만으로는 판단 불가 — 실제 태스크로 A/B 필요.

> ⚠️ 웹의 "16GB VRAM 최적 모델" 가이드(Qwen3-Coder 32B Q4 등)는 RTX 4080/5080 기준이다.
> 그쪽은 대역폭이 ~717 GB/s로 **이 기기의 7배**다. tok/s 수치를 그대로 대입하면 안 된다.

---

## 6. 추론 백엔드 선택

| 우선순위 | 백엔드 | 적합 상황 | 비고 |
|---|---|---|---|
| **1** | **llama.cpp (CUDA + Flash Attention + CUDA graphs)** | 단일 사용자 | Orin NX 공개 벤치마크 대부분이 이 조합. GGUF의 IQ2/IQ3 양자화 폭이 넓어 16GB에 유리 |
| 2 | Ollama | 빠른 프로토타이핑 | **반드시 Jetson CUDA 빌드** (`dustynv/ollama` via jetson-containers). 일반 빌드는 CPU로 폴백 |
| 3 | vLLM | 동시 접속 서빙 | KV 캐시 압박으로 4B~8B급으로 제한 |
| 4 | TensorRT-LLM / MLC | 프로덕션 고정 모델 | 최고 속도, 빌드/변환 난이도 높음 |

---

## 7. 권장 진행 순서

1. `sudo apt install nvidia-jetpack` — CUDA 스택 설치
2. Super Mode 프로파일 활성화 + `nvpmodel -m 0` + 재부팅
3. `bash scripts/thermal-test.sh 120` 으로 MAXN 부하 시 온도/스로틀링 확인
4. jetson-containers로 llama.cpp CUDA 컨테이너 구동
5. **Qwen3-8B Q4_K_M(5GB)으로 기준선 확보** — GUI 켠 채 가능, 즉시 테스트
6. 헤드리스 전환 (`multi-user.target`)
7. Qwen3-30B-A3B IQ2_M 구동 후 실제 태스크로 5번과 A/B 비교
8. 용도 확정 후 필요 시 TensorRT-LLM으로 최적화

---

## 참고 자료

- [Seeed reComputer J4012 제품 페이지](https://www.seeedstudio.com/reComputer-J4012-p-5586.html)
- [NVIDIA Jetson Orin NX 시리즈 데이터시트](https://developer.nvidia.com/downloads/jetson-orin-nx-module-series-data-sheet)
- [JetPack 6.2 SDK](https://developer.nvidia.com/embedded/jetpack-sdk-62)
- [JetPack 6.2 Super Mode 상세](https://www.edge-ai-vision.com/2025/01/nvidia-jetpack-6-2-brings-super-mode-to-nvidia-jetson-orin-nano-and-jetson-orin-nx-modules/)
- [Orin NX 16GB LLM 실측 벤치마크 (dnhkng)](https://dnhkng.github.io/posts/jetson-orin-nx-vram-tuning/)
- [Jetson LLM 실행 가이드 (llama.cpp / Ollama)](https://proventusnova.com/blog/llm-inference-jetson-orin-llamacpp-ollama/)
- [Qwen3-30B-A3B GGUF 양자화 크기표 (Unsloth)](https://huggingface.co/unsloth/Qwen3-30B-A3B-GGUF)
- [bartowski Qwen3-30B-A3B GGUF](https://huggingface.co/bartowski/Qwen_Qwen3-30B-A3B-GGUF)
