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

**폴더가 곧 상태다.** `research/` 는 잰 것, `docs/build/` 는 돌고 있는 것,
`docs/plans/` 는 안 끝난 것, `docs/archive/` 는 접은 것.

```
├── research/            조사 · 실측  → 색인: research/README.md
│   ├── hardware.md          하드웨어 실측 · 전력모드 · 메모리 예산
│   ├── performance.md       벤치마크 (대역폭 · 깊이별 곡선 · 품질)
│   ├── llm-models.md        모델 3종 비교 (툴콜링 · 속도 · 한국어)
│   ├── reference-survey.md  GitHub 생태계 전수 스캔 (704개)
│   └── speech-stack.md      음성 스택 (ASR · KWS · 통역 · TTS) + 실측
├── docs/                구축 · 기획   → 색인: docs/README.md
│   ├── build/               ✅ 가동 중
│   │   ├── llm-runtime.md       llama.cpp 빌드 · 서버 운영
│   │   └── openclaw-agent.md    에이전트 게이트웨이 결합 · 함정 14가지
│   │                            + §7 Life Trainer 결합 (MCP · 폴더 감옥 · 예산)
│   ├── plans/               ⏳ 미완
│   │   ├── voice-agent-plan.md  한국어 음성 에이전트 (Phase 0 실측 완료)
│   │   ├── opensource-plan.md   측정 자료 공개 · whichllm 기여
│   │   └── grant-radar-plan.md  지원사업 매칭 서비스 (미착수)
│   └── archive/             ⛔ 판단 종료 · 전제 무효
│       ├── project-candidates.md  후보 7개 비교
│       ├── project-proposal.md    Understudy (미채택)
│       └── vision-agent-plan.md   Frigate+VLM (전제 무효)
├── Life_Trainer/        ★ 상시 구동 중인 응용 — 자체 문서 트리를 갖는다
├── openclaw-setup/      OpenClaw 실행 자산 (systemd 유닛 · 설치 스크립트)
├── scripts/             측정 · 자동화 도구
├── results/             측정 원본 데이터 (음성 wav 포함)
├── models/              GGUF · 음성 모델 가중치 (git 제외)
└── reference/           타 프로젝트 클론 (git 제외)
```

---

## 이 위에 올린 것 — Life Trainer

측정으로 확인한 제약(**60 GB/s · 11 tok/s · 13.4 GB**) 위에서 실제로 돌아가는 응용.

하루 활동을 10분 단위 144슬롯으로 계측하고, 야간에 관심사 문서를 수집·요약하고,
Slack 으로 리포트를 보내며, **물어보면 자기 기록을 근거로 답하는** 온디바이스 개인 에이전트.
**실사용 중** — 타이머 7개와 상시 서비스 3개가 재부팅을 넘겨 돌고 있다.

```
ActivityWatch(PC) ──Tailscale──┐
RSS · arXiv ───────────────────┼─→ SQLite(WAL·FTS5) ─→ GPU 단일 큐 ─→ Slack · 웹 플래너
수동 입력 / Slack 대화 ─────────┘                        (Qwen3-8B, llama-server 공유)
```

설계 3원칙: **수집이 8할이다 · 집계는 SQL 문장화는 LLM · GPU 는 단일 자원이다.**

**2026-08-24: 같은 물건이 OpenClaw 의 에이전트로도 돈다.** 툴을 고르는 주체가
규칙에서 **모델**로 바뀐 경로가 하나 더 생겼다 — 슬래시 명령 10개·플래너·RAG 를
MCP 툴 13개로 내보내고, **지정된 폴더 밖으로는 못 나간다.** 프레임워크 기본
구성이 시스템 프롬프트 12,541 토큰이던 것을 **5,247 토큰**으로 줄인 것이 이 결합의
대부분이었다 ([openclaw-agent.md §7](docs/build/openclaw-agent.md)).

→ **[Life_Trainer/HANDOFF.md](Life_Trainer/HANDOFF.md)** (지금 상태 · 이어받는다면 여기부터) ·
[README](Life_Trainer/README.md) · [전체 설명서](Life_Trainer/docs/handbook.md) ·
[설계서](Life_Trainer/docs/life-trainer-design.md) ·
[버그 기록](Life_Trainer/HISTORY/) (깨진 가정 21건)

---

## 또 하나의 트랙 — 음성 (Phase 0 실측 완료, 미착수)

같은 하드웨어 위에서 **완전 로컬 한국어 음성 에이전트**가 성립하는지를 마이크 없이
전 구간 측정했다. TTS 로 명령 음성을 합성해 ASR 에 먹이는 방식이라 **상한선**이다.

| 구간 | 실측 | 판정 |
|---|---|---|
| ASR (SenseVoice-Small int8, CPU 8스레드) | **RTF 0.042** — 1.3초 음성을 0.12초에 | ✅ |
| TTS (Supertonic-3 int8, CPU) | RTF 0.57~0.80 | ⚠️ 응답이 길면 부담 |
| LLM 툴콜 (Qwen3-4B Q4) | **2.45초** | ❌ **지연의 95%** |
| 핫패스 규칙 | **0.19 ms** | ✅ LLM 대비 약 **6,500배** |

**결론: 성립한다. 단 LLM 을 매번 부르면 안 된다.** 웨이크워드 → 핫패스 규칙(0.2ms)
→ LLM(2.5초, 폴백만) 의 3단 게이트 설계로 갔다.

이 "규칙 게이트가 먼저, LLM 은 폴백" 구조는 **Life Trainer 의 `llm/trigger.py` 로
그대로 옮겨갔다** — 음성에서 먼저 실측하고 텍스트에서 재사용한 셈이다.

→ [research/speech-stack.md](research/speech-stack.md) (생태계 조사) ·
[docs/plans/voice-agent-plan.md](docs/plans/voice-agent-plan.md) (실행 계획)

> **진짜 공백은 기술이 아니라 언어다.** ASR·TTS 는 52k~14k 스타 프로젝트가 완성형으로
> 널려 있어 조립만 하면 된다. 한국어 음성 도구 생태계가 사실상 비어 있는 것이 문제다.

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

**하드웨어 · 런타임**

| 스크립트 | 용도 | 결과 |
|---|---|---|
| `verify-jetpack.sh` | JetPack · CUDA · DLA · 전력모드 일괄 검증 | [hardware.md](research/hardware.md) |
| `membw.cu` | 메모리 대역폭 실측 (LLM 속도 예측의 기준값) | 〃 |
| `thermal-test.sh` | CPU+GPU 동시 부하 발열 측정 | 〃 |
| `build-llamacpp.sh` | llama.cpp CUDA 빌드 (SM 8.7) | [llm-runtime.md](docs/build/llm-runtime.md) |

**모델 벤치마크**

| 스크립트 | 용도 | 결과 |
|---|---|---|
| `run-model-suite.sh` | 모델별 벤치마크 무인 실행 | [llm-models.md](research/llm-models.md) |
| `deep-context-bench.py` | 컨텍스트 깊이별 성능 + 장거리 검색 | [performance.md](research/performance.md) |
| `chat-bench.py` | 운영 설정 그대로 대화로 깊이·품질 동시 측정 | 〃 |
| `tool-bench.py` | 툴 콜링 정확도 (에이전트 적합성) | [llm-models.md](research/llm-models.md) |

**음성 스택** — 마이크 없이 TTS 합성음으로 상한선을 먼저 쟀고, 나중에 실마이크로 다시 쟀다

| 스크립트 | 용도 | 결과 |
|---|---|---|
| `speech-bench.py` | TTS · ASR · 엔드투엔드 (합성음) | [speech-stack.md](research/speech-stack.md) |
| `asr-source-test.py` | ASR 실패 원인 분리 — 인식기인가 합성음인가 | 〃 |
| `voice-e2e-bench.py` | 음성 에이전트 E2E, **슬롯 정확도** 기준 | [voice-agent-plan.md](docs/plans/voice-agent-plan.md) |
| `mic-live-bench.py` | 실마이크 입력으로 같은 측정 (잡음·거리·에코 포함) | 〃 |
| `hotpath-router.py` | 규칙 라우터로 LLM 을 빼면 얼마나 빨라지나 (**약 6,500배**) | 〃 |
| `ha-tool-bench.py` | 구어체 스마트홈 툴 콜링 정확도 | 〃 |

**생태계 조사**

| 스크립트 | 용도 | 결과 |
|---|---|---|
| `gh-research*.mjs` | Playwright 로 GitHub 검색 (**70% 차단당함**) | [reference-survey.md](research/reference-survey.md) |
| `gh-api-collect.py` | REST API 로 전환해 재수집 | 〃 |
| `gh-analyze.py` | 4개 소스 병합 + 젯슨 적합성 점수화 | 〃 |

---

## 기록해둔 함정들

- **Super Mode 불가 원인** — `nvpower.sh`가 부팅마다 conf 심링크를 되돌리고,
  하드웨어 과전류 보호도 25W로 설정된다 ([research/hardware.md](research/hardware.md))
- **25W 프로파일이 MAXN보다 느리다** — GPU를 408 MHz로 고정 제한
- **`llama-cli`의 `-no-cnv` 미동작** — stdin EOF 시 프롬프트 무한 출력 (30MB 로그 발생)
- **슬롯 수가 KV 캐시를 배수로 잡는다** — 기본 4슬롯이면 4배 소요
- **`pkill -f` 자기매칭** — 자신의 셸 명령줄까지 죽인다
- **툴 스키마의 정규식 하나가 요청 전체를 400 으로 만든다** — llama.cpp 의 GBNF 변환기가
  `pattern` 을 못 다룬다. 앵커를 붙여도 안 된다 ([docs/build/openclaw-agent.md](docs/build/openclaw-agent.md))
- **에이전트 워크스페이스의 인격 파일은 지워도 다시 생긴다** — `openclaw agents add` 가
  깔아 두는 6,122바이트가 시스템 프롬프트에 통째로 실리는데, 삭제하면 재기동 때
  시드된다. **비워서 남겨야** 시드가 안 돈다 (§7-2)
- **"툴을 불렀다"와 "일을 했다"는 다르다** — 8B 가 계획 목록만 조회하고 완료는
  사용자에게 시켰다. 툴 이름만 채점하면 통과한다. **툴 인자까지 봐야** 잡힌다 (§7-5)
- **소형 모델에게 선택 인자는 없는 것과 같다** — "내일 계획 넣어줘" 에서 `day` 를
  3/3 빠뜨려 계획이 조용히 오늘에 들어갔다. 프롬프트로도 툴 결과 경고로도 안 잡혔고,
  **스키마에서 필수로 만들자 0/3** 이 됐다 (왕복도 하나 줄었다)
- **ActivityWatch 의 마지막 이벤트는 duration 이 자란다** — 끝 시각 기준으로 페이징하면
  진행 중인 활동이 조각나고 이중 계산된다. 시작 시각 기준 + 겹침 재조회가 정답
  ([Life_Trainer/docs/research/activitywatch.md](Life_Trainer/docs/research/activitywatch.md))
- **aw-server 를 Tailscale IP 에만 바인딩하면 수집이 멈춘다** — 로컬 워처가
  `localhost:5600` 으로 붙기 때문. `0.0.0.0` + 방화벽으로 대역 제한이 맞다
- **합성음 인식률이 무너져도 ASR 탓이 아닐 수 있다** — 같은 인식기가 원어민 녹음은
  완벽히 받아썼다. 원인을 가르는 실험을 따로 설계해야 했다
  ([speech-stack.md](research/speech-stack.md))
- **문자열 완전일치로 음성을 채점하면 정답이 오답이 된다** — "이십사도" → "24도" 는
  ASR 의 숫자 정규화(ITN) 결과라 의미는 정확하다. 채점 기준을 **슬롯 정확도**로 바꿨다
- **EXAONE 툴 콜링 2/6 은 모델 탓이 아니었다** — 채팅 템플릿에 `tools` 렌더링 코드가
  없어 llama.cpp 가 툴 정의를 조용히 버렸다. **모델은 툴 존재를 몰랐다**
  ([llm-models.md](research/llm-models.md))

---

## 라이선스

문서·스크립트는 자유롭게 참고하되, `reference/` 하위 프로젝트와 `models/` 가중치는
각 원저작자의 라이선스를 따른다.
