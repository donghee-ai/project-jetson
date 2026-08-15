# project-jetson

**NVIDIA Jetson Orin NX 16GB (reComputer J4012) 로컬 LLM 구축 · 실측 기록**

사양서 수치가 아닌 **직접 측정한 값**으로 하드웨어 한계를 규명하고,
그 위에서 어떤 LLM을 어떻게 돌릴지 결정한 과정의 기록.

---

## 핵심 실측 요약

| 항목 | 사양치 | **실측** |
|---|---|---|
| 메모리 대역폭 | 102.4 GB/s | **60.0 GB/s** (58%) |
| GPU (출고 15W 모드) | 1024 CUDA core | **512 (4 SM)** — TPC 절반 게이팅 |
| GPU (MAXN 적용 후) | — | **1024 (8 SM) @ 918 MHz** |
| Super Mode (157 TOPS) | 지원 표기 | **사용 불가** — 디바이스 트리 제약 |
| LLM 가용 메모리 | 16 GB | **약 13.4 GB** (통합 메모리, OS 제외) |

> 출고 기본값(15W)에서는 **GPU 절반이 꺼져 있었다.** MAXN 적용으로 SM이 4 → 8이 됐다.
> Super Mode는 conf 파일이 존재함에도 디바이스 트리에 `-super`가 없어 부팅마다 되돌려진다.

---

## 모델 선택 결론 — 용도별로 다르다

| 용도 | 권장 모델 | 근거 |
|---|---|---|
| **에이전트 · 툴 콜링** | Qwen3-8B Q4_K_M | 툴 콜링 **6/6** (30B 4/6, EXAONE 2/6) |
| **한국어 문서 처리** | EXAONE 3.5 7.8B | 토큰 **19% 절약** + 전 구간 최고 속도 |
| 짧은 대화 (~9K) | Qwen3-30B-A3B IQ2_M | 얕은 깊이에서 가장 빠름 |
| 사업화 (라이선스) | Qwen3 계열 | Apache 2.0 (EXAONE은 `other`) |

**컨텍스트 깊이 9,600 토큰에서 8B가 30B를 추월한다.** 얕은 벤치마크만 보고
모델을 고르면 안 된다는 것이 이 프로젝트의 가장 큰 교훈.

---

## 디렉토리 구조

```
├── research/     조사 결과
│   ├── hardware.md          하드웨어 실측 · 전력모드 · 메모리 예산
│   ├── performance.md       벤치마크 (대역폭 · 깊이별 곡선 · 품질)
│   └── llm-models.md        모델 3종 비교 (툴콜링 · 속도 · 한국어)
├── docs/         구축 · 기획 기록
│   ├── llm-runtime.md       llama.cpp 빌드 · 서버 운영
│   └── grant-radar-plan.md  응용 서비스 기획
├── scripts/      측정 · 자동화 도구
├── results/      측정 원본 데이터
├── models/       GGUF 가중치 (git 제외)
└── reference/    타 프로젝트 클론 (git 제외)
```

---

## 환경

```
하드웨어   Jetson Orin NX 16GB / Ampere 1024 CUDA / Cortex-A78AE ×8 / LPDDR5 128-bit
소프트웨어 JetPack 6.2.3 / L4T 36.5.0 / CUDA 12.6 / TensorRT 10.3 / Ubuntu 22.04
추론       llama.cpp (CUDA, SM 8.7, Flash Attention, CUDA Graphs)
전력모드   MAXN (CPU 8코어 1984 MHz / GPU 8 SM 918 MHz)
```

---

## 주요 스크립트

| 스크립트 | 용도 |
|---|---|
| `scripts/verify-jetpack.sh` | JetPack · CUDA · DLA · 전력모드 일괄 검증 |
| `scripts/membw.cu` | 메모리 대역폭 실측 (LLM 속도 예측의 기준값) |
| `scripts/build-llamacpp.sh` | llama.cpp CUDA 빌드 (SM 8.7) |
| `scripts/run-model-suite.sh` | 모델별 벤치마크 무인 실행 |
| `scripts/deep-context-bench.py` | 컨텍스트 깊이별 성능 + 장거리 검색 |
| `scripts/tool-bench.py` | 툴 콜링 정확도 (에이전트 적합성) |
| `scripts/thermal-test.sh` | CPU+GPU 동시 부하 발열 측정 |

---

## 기록해둔 함정들

- **Super Mode 불가 원인** — `nvpower.sh`가 부팅마다 conf 심링크를 되돌리고,
  하드웨어 과전류 보호도 25W로 설정된다 ([research/hardware.md](research/hardware.md))
- **25W 프로파일이 MAXN보다 느리다** — GPU를 408 MHz로 고정 제한
- **`llama-cli`의 `-no-cnv` 미동작** — stdin EOF 시 프롬프트 무한 출력 (30MB 로그 발생)
- **슬롯 수가 KV 캐시를 배수로 잡는다** — 기본 4슬롯이면 4배 소요
- **`pkill -f` 자기매칭** — 자신의 셸 명령줄까지 죽인다

---

## 라이선스

문서·스크립트는 자유롭게 참고하되, `reference/` 하위 프로젝트와 `models/` 가중치는
각 원저작자의 라이선스를 따른다.
