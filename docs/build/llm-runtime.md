# LLM 런타임 구축 기록 — llama.cpp on Jetson Orin NX 16GB

> 구축일: 2026-08-14
> 관련: [hardware.md](../../research/hardware.md)

---

## 1. 현재 구성 요약

| 항목 | 값 |
|---|---|
| llama.cpp | `b1-a94d563` (2026-08-13 커밋) |
| 빌드 | CUDA 12.6 / `CMAKE_CUDA_ARCHITECTURES=87` / FA·CUDA Graphs ON |
| 설치 경로 | `~/llama.cpp/build/bin/` |
| 모델 저장소 | `~/project/project-jetson/models/` |
| 전력 모드 | MAXN (CPU 8코어 1984MHz / GPU 8SM 918MHz) |
| GPU 인식 | `CUDA0: Orin (15642 MiB)` |

### 보유 모델

| 파일 | 크기 | 파라미터 |
|---|---|---|
| `Qwen3-8B-Q4_K_M.gguf` | 4.68 GB | 8.19 B (덴스) |
| `Qwen3-30B-A3B-IQ2_M.gguf` | 9.71 GB | 30.53 B (MoE, 활성 3.3B) |

---

## 2. 벤치마크 요약

> **전체 측정 결과는 [performance.md](../../research/performance.md)** 참조.
> 컨텍스트 깊이별 곡선, 품질 검증 7단계, 장거리 검색, 메모리 안정성 포함.

조건: `-ngl 99 -p 512 -n 128 -fa 1 -r 2` / MAXN

| 모델 | 크기 | **pp512** (프롬프트) | **tg128** (생성) | 전력 | 온도 |
|---|---|---|---|---|---|
| Qwen3-8B Q4_K_M | 4.68 GiB | **353.74 ± 5.66** | **11.14 ± 0.01** | 20.4 W | 67°C |
| **Qwen3-30B-A3B IQ2_M** | 9.71 GiB | **264.26 ± 4.16** | **14.22 ± 0.02** | 12~14.7 W | 59~61°C |
| 차이 | — | **−25%** | **+28%** | −32% | 더 낮음 |

### MoE 가설 — 확인됨, 단 예측보다 약함

**결론: 30B-A3B가 8B보다 토큰 생성 28% 빠르다.** 파라미터가 3.7배 큰 모델인데도.

다만 예측(약 21 tok/s)에는 못 미쳤다. 실측 14.22에서 역산하면 **토큰당 3.7~4.2 GB**를
읽는다. "활성 3.3B / 총 30.5B" 비율로 추정한 2.5 GB보다 크다. 원인 2가지:

1. **어텐션은 희소하지 않다.** MoE로 분할된 것은 FFN뿐이며, 48개 레이어의
   어텐션 가중치·임베딩·출력 레이어는 **매 토큰 전부 읽는다.**
2. **랜덤 액세스 패널티.** 전문가 선택이 토큰마다 바뀌므로 순차 읽기가 아니다.
   실측 대역폭 60 GB/s는 순차 기준이라 MoE 실효 대역폭은 그보다 낮다.

### 프롬프트 처리는 MoE가 불리 (−25%)

프리필 단계에서는 512토큰 배치가 여러 전문가를 두루 활성화하여 연산량이 증가한다.
**긴 입력 / 짧은 출력** 작업에서는 이 손해가 생성 이득을 상쇄한다.

#### 실무 환산 (입력 3,000 / 출력 300 토큰)

> ⚠️ 아래는 **빈 컨텍스트 속도 기준의 초기 추정**이다.
> 실제로는 깊이에 따라 생성이 느려지므로 **[benchmark-results.md §7](../../research/performance.md#7-실무-기준표)의
> 실측 기준표(건당 약 40초)를 사용할 것.**

| | 프롬프트 | 생성 | 합계 |
|---|---|---|---|
| Qwen3-8B | 8.5초 | 26.9초 | 35.4초 |
| Qwen3-30B-A3B | 11.4초 | 21.1초 | 32.5초 |
| **30B-A3B (깊이 3K 실측 반영)** | **12.7초** | **27.5초** | **약 40초** ★ |

전체로는 30B가 유리. **짧은 입력 / 긴 출력**(대화·창작)이면 격차가 더 벌어진다.

---

## 3. ★ 한국어 토크나이저 효율 (실측)

동일 의미 문장을 각 언어로 토큰화한 결과. `/tokenize` 엔드포인트 사용.

| 언어 | 토큰 | 문자 | 문자당 토큰 | 영어 대비 |
|---|---|---|---|---|
| 중국어 | 20 | 47 | 0.426 | 0.69배 |
| **영어** | **29** | **200** | **0.145** | **기준** |
| 일본어 | 42 | 59 | 0.712 | 1.45배 |
| **한국어** | **45** | **75** | **0.600** | **1.55배** |

**한국어는 같은 내용에 토큰이 55% 더 든다.** Qwen3는 119개 언어 지원을 표방하고
한국어도 공식 목록에 있으나, 토크나이저는 중국어에 최적화되어 있다.

### 실질 영향

```
표시 속도    14.22 tok/s
한국어 실효  ≈ 9.2 tok/s 상당 (영어 기준 환산)
```

컨텍스트도 그만큼 빨리 찬다. 32K 컨텍스트에 담기는 분량:

| 언어 | 문자 수 | 비고 |
|---|---|---|
| 한국어 | 약 54,000자 | A4 28~34쪽 |
| 영어 | 약 226,000자 | 약 2.4배 |

---

## 4. 컨텍스트 / 메모리 예산

### 모델 구조 (GGUF 메타데이터)

```
qwen3moe.block_count                48       레이어
qwen3moe.context_length          32768       모델 최대 컨텍스트
qwen3moe.attention.head_count       32
qwen3moe.attention.head_count_kv     4       ← GQA, KV 캐시가 가볍다
qwen3moe.expert_count              128
qwen3moe.expert_used_count           8       ← 토큰당 8/128 전문가
qwen3moe.expert_feed_forward_length 768
```

**KV 헤드가 4개뿐**이라 8B(KV 헤드 8)보다 컨텍스트당 메모리가 적다.
긴 문서 처리에는 30B-A3B가 8B보다 유리하다.

### ⚠️ 슬롯 수가 KV 캐시를 배수로 잡는다

llama-server는 `--parallel N` 슬롯마다 독립된 KV 캐시를 확보한다.
**기본값 4슬롯이면 KV 캐시가 4배** 소요된다. 혼자 쓰면 `--parallel 1`이 맞다.

| 설정 | 대화당 컨텍스트 | KV 캐시 |
|---|---|---|
| `-c 16384` (슬롯 4, 기본) | 16,384 | 4배 |
| **`-c 32768 --parallel 1`** | **32,768** | **1배** ★ 현재 |

### 메모리 여유 확보 수단

| 조치 | 확보량 |
|---|---|
| `--parallel 1` | KV 캐시 3/4 회수 |
| `-ctk q8_0 -ctv q8_0` | KV 캐시 **절반** (FP16 대비) |
| GUI 종료 (헤드리스) | ~0.5 GB |
| VS Code·브라우저 종료 | ~1 GB |

---

## 5. 실행 명령

### 서버 (권장 — 현재 구성)

```bash
~/llama.cpp/build/bin/llama-server \
  -m ~/project/project-jetson/models/Qwen3-30B-A3B-IQ2_M.gguf \
  -ngl 99 -c 32768 --parallel 1 \
  -fa on -ctk q8_0 -ctv q8_0 \
  --chat-template-kwargs '{"enable_thinking":false}' \
  --temp 0.7 --top-p 0.8 --top-k 20 \
  --host 0.0.0.0 --port 8080
```

접속 경로:

| 경로 | 주소 |
|---|---|
| 로컬 | `http://localhost:8080` |
| **Tailscale** | **`http://100.64.0.2:8080`** |
| LAN | `http://192.168.1.100:8080` |

### CLI (대화형)

```bash
~/llama.cpp/build/bin/llama-cli \
  -m ~/project/project-jetson/models/Qwen3-30B-A3B-IQ2_M.gguf \
  -ngl 99 -c 32768 -fa on -ctk q8_0 -ctv q8_0 \
  --temp 0.6 --top-p 0.95 --top-k 20 --min-p 0
```

### 벤치마크

```bash
~/llama.cpp/build/bin/llama-bench -m ~/project/project-jetson/models/<모델>.gguf \
  -ngl 99 -p 512 -n 128 -fa 1 -r 2
```

### 주요 플래그

| 플래그 | 의미 |
|---|---|
| `-ngl 99` | 전 레이어 GPU. **없으면 CPU로 돌아 10배 느려짐** |
| `-fa on` | Flash Attention |
| `-ctk/-ctv q8_0` | KV 캐시 8비트 (FP16 대비 절반) |
| `--parallel 1` | 슬롯 1개 (혼자 쓸 때) |
| `--chat-template-kwargs '{"enable_thinking":false}'` | Qwen3 사고 모드 비활성 |

---

## 6. Qwen3 사고(thinking) 모드

Qwen3는 기본적으로 추론 과정을 생성한다. `하이` 한 마디에 **61토큰 / 4.5초**를
소모했으며 그중 약 50토큰이 `Reasoning` 블록이었다.

| 용도 | thinking |
|---|---|
| 일상 대화 · 번역 · 요약 | **끄기** (약 5배 빠름) |
| 코딩 · 수학 · 다단계 추론 | 켜기 |
| **JSON 구조화 출력** | **반드시 끄기** — 사고 텍스트가 섞여 파싱이 깨진다 |

끄는 방법 2가지:
- 서버 전역: `--chat-template-kwargs '{"enable_thinking":false}'`
- 메시지별: 프롬프트 끝에 `/no_think`

권장 샘플링 값이 모드별로 다르다.

| 모드 | 설정 |
|---|---|
| thinking ON | `--temp 0.6 --top-p 0.95 --top-k 20 --min-p 0` |
| thinking OFF | `--temp 0.7 --top-p 0.8 --top-k 20` |

---

## 7. 모델 선택 가이드

### 한국어 특화 모델 후보

| 모델 | Q4_K_M | 13GB 예산 | 라이선스 |
|---|---|---|---|
| EXAONE 3.5 7.8B (LG) | 4.44 GB | ✅ 여유 | ⚠️ `other` |
| HyperCLOVAX SEED Think 14B (Naver) | 8.30 GB | ✅ 가능 | ⚠️ `other` |
| EXAONE 4.5 33B (LG) | 18.67 GB | ❌ 초과 | ⚠️ `other` |
| Kanana 2 3B (Kakao) | ~2 GB | ✅ | 상대적으로 자유 |

### ⚠️ 라이선스가 사업화의 갈림길

EXAONE·HyperCLOVA X SEED 모두 라이선스가 `other`이며, EXAONE은 자체
`EXAONE AI Model License`로 **연구·비상업 용도 제한** 조항이 있는 것으로 알려져 있다.

**Qwen3는 Apache 2.0**이라 상업적 사용에 제약이 없다.
grant-radar 등 사업화 대상에는 Qwen3 계열이 안전하다.

> 각 모델 카드의 라이선스 전문을 직접 확인할 것. 애매하면 Apache 2.0 계열 선택.

### 용도별 권장

| 용도 | 모델 | 근거 |
|---|---|---|
| **사업화 대상 · 기본 모델** | **Qwen3-30B-A3B IQ2_M** | Apache 2.0, 생성 +28%, 품질 검증 통과 |
| 짧은 입력 · 대량 배치 | Qwen3-8B Q4_K_M | 프롬프트 처리 +34% (353 vs 264) |
| 개인용 한국어 | EXAONE / HyperCLOVAX (검증 필요) | 한국어 품질 우위 가능성 |

### IQ2_M 품질 — 검증 완료 (사전 예측이 틀렸음)

착수 전 **"2비트대 양자화는 포맷 준수·지시 이행에서 먼저 무너진다"** 고 예측했으나,
실측에서는 그런 징후가 없었다.

| 검증 항목 | 결과 |
|---|---|
| JSON 구조화 출력 (7필드) | ✅ 전부 정확, 설명문 없이 순수 JSON |
| 조건 대조 추론 (함정 문항) | ✅ 정답 |
| 정확한 단어 수 준수 | ✅ 정확히 5단어 |
| 장거리 검색 (최대 31,901 토큰) | ✅ **10/10** |

상세: [benchmark-results.md §5](../../research/performance.md#5-품질-검증--실제-대화-7단계)

> 각 항목 1회 측정이므로 통계적 견고성은 없다. 다회 검증 필요.

### 남은 미검증 항목

- 8B 모델의 깊이별 곡선 (KV 헤드가 2배라 깊은 컨텍스트에서 순위 역전 가능)
- 한국어 특화 모델과의 실품질 비교 (라이선스 확인 선행)

---

## 8. 트러블슈팅 (실제 겪은 문제)

| 증상 | 원인 / 조치 |
|---|---|
| **`llama-cli`가 프롬프트 `> `를 무한 출력 (30MB 로그)** | `-no-cnv`가 동작하지 않아 대화형 모드 진입 후 stdin EOF. **벤치마크는 `llama-bench` 사용.** CLI 대화는 실제 터미널에서 직접 실행 |
| `failed to fit params to free device memory: n_gpu_layers already set by user` | **정상 경고.** `-ngl`을 명시했으니 자동 조정을 안 한다는 안내 |
| `control-looking token: 128247 '</s>' was not control-type` | 모델 메타데이터의 알려진 흠. 동작 무관 |
| 모델 로드 후 OOM | 슬롯 수 확인 (`--parallel 1`), KV 양자화(`-ctk/-ctv q8_0`), 컨텍스트 축소 |
| GPU 사용률 0%인데 CPU 100% | `-ngl` 누락 → CPU 추론. 또는 위 무한루프 |
| 첫 로드가 30초~1분 | 정상 (NVMe에서 9.71GB 읽기). 2회차부터 페이지 캐시로 빨라짐 |

### 프로세스 종료 시 주의

`pkill -f llama-cli` 같은 명령은 **자신의 셸 명령줄까지 매칭**해 스스로를 죽인다.
프로세스명 기준으로 종료할 것.

```bash
killall -q llama-server    # 프로세스명 매칭 (안전)
pkill -f "[l]lama-server"  # 문자클래스로 자기매칭 회피
```

---

## 9. 다음 작업

- [x] ~~IQ2_M 품질 A/B~~ — **완료.** JSON·조건대조·지시이행 전부 통과
      ([benchmark-results.md §5](../../research/performance.md#5-품질-검증--실제-대화-7단계))
- [x] ~~컨텍스트 깊이별 성능~~ — **완료.** 32K에서 3.96 tok/s (−70%)
      ([benchmark-results.md §4](../../research/performance.md#4--컨텍스트-깊이별-성능-가장-중요한-측정))
- [ ] 지속 부하 발열 — `scripts/thermal-test.sh 300` (MAXN에서 미실행)
- [ ] 8B 모델의 깊이별 곡선 — 깊은 컨텍스트에서 순위 역전 가능성
- [ ] EXAONE 3.5 7.8B 한국어 품질 비교 (라이선스 확인 선행)
- [ ] 헤드리스 전환으로 메모리 0.5GB 확보
- [ ] grant-radar 파이프라인 연결 (실측 기준: **건당 약 40초**, 하루 100건 = 1시간 7분)
