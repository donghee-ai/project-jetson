# H2 — 완전 로컬 한국어 음성 에이전트 (실측 기반 실행 계획)

> 작성일: 2026-08-15 / 상태: **Phase 0 완료 (마이크 없이 전 구간 실측)**
> 측정 환경: Jetson Orin NX 16GB / MAXN / JetPack 6.2.3 / llama.cpp b1-a94d563 / sherpa-onnx 1.13.5
> 관련: [speech-stack-research.md](speech-stack.md) · [project-candidates.md](../project-candidates.md) · [project-proposal.md](../project-proposal.md)

---

## 0. 결론 — 성립한다. 단, LLM을 매번 부르면 안 된다

| 구간 | 실측 | 판정 |
|---|---|---|
| **ASR** (SenseVoice-Small int8, CPU 8스레드) | **RTF 0.042** — 1.3초 음성을 0.12초에 | ✅ 문제없음 |
| **TTS** (Supertonic-3 int8, CPU) | RTF 0.57~0.80 (실시간 1.5~1.8배속) | ⚠️ 응답이 길면 부담 |
| **LLM 툴콜** (Qwen3-4B Q4) | **2.45초** | ❌ **지연의 95%** |
| **LLM 툴콜** (Qwen3-8B Q4) | 3.54초 | ❌ 더 느림 |
| **핫패스 규칙** | **0.19 ms** | ✅ **LLM 대비 약 6,500배** |
| 슬롯 정확도 (음성 경로) | **8/8 (4B)** / 7/8 (8B) | ✅ 실용 수준 |

**설계 결론: 3단 게이트.** 웨이크워드(상시·초저비용) → 핫패스 규칙(0.2 ms) → LLM(2.5초, 폴백만).
[Understudy](../project-proposal.md)의 선생-학생 구조가 음성에서 그대로 성립한다.

---

## 1. 오늘 구축한 것

```
설치   sherpa-onnx 1.13.5 (pip, CPU) — ASR·TTS·VAD·KWS 통합
모델   refs/models/speech/
       ├─ SenseVoice-Small (zh/en/ja/ko/yue)            1.0 GB   한국어 ASR
       ├─ Supertonic-3 int8 (31개 언어)                  123 MB   한국어 TTS (상용급)
       ├─ vits-mimic3 ko_KO-kss_low                       64 MB   한국어 TTS (경량)
       └─ silero_vad.onnx                                632 KB   발화 구간 검출
       refs/models/Qwen3-4B-Q4_K_M.gguf                       2.5 GB   ★ 음성용 LLM 후보
스크립트 scripts/ha-tool-bench.py      한국어 스마트홈 툴콜 정확도
        scripts/speech-bench.py       TTS·ASR RTF + E2E
        scripts/asr-source-test.py    ASR 실패 원인 분리 (TTS 교체 대조)
        scripts/voice-e2e-bench.py    ★ 슬롯 정확도 기준 E2E
        scripts/hotpath-router.py     ★ 규칙 라우터 + LLM 대조
```

**마이크 없이 전 구간을 측정했다.** TTS로 명령 음성을 합성해 ASR에 먹이는 방식이다.

---

## 2. 실측 상세

### 2-1. 한국어 툴 콜링 (텍스트 입력, Qwen3-8B) — 11/12

`scripts/ha-tool-bench.py`

| 분류 | 결과 |
|---|---|
| 기본 제어·병렬 2건·수치 인자·복합 2종·상태 조회·씬·타이머 | ✅ 전부 통과 |
| **구어체** "아 덥다 에어컨 좀" | ✅ `climate_set(area=거실, mode=cool)` |
| ★부정문 "불 켜지 마" | ✅ 툴 미호출 |
| ★잡담 "오늘 기분 어때?" | ✅ 툴 미호출 |
| **★대상 생략 "불 꺼줘"** | ❌ **되묻지 않고 거실로 추측** |

> 한국어 구어체 처리는 기대 이상이었다. 실패한 것은 언어 능력이 아니라
> **모호할 때 되묻지 않는 태도**다. 프롬프트·정책으로 해결할 문제.

### 2-2. ★ 채점 기준을 바꿨다 — 문자열 → 슬롯

처음엔 ASR 결과를 원문과 문자열 비교했다. 그랬더니 이런 것이 오답 처리됐다.

```
원문 "에어컨 이십사도로 맞춰줘"  →  인식 "에어컨 24도로 맞춰줘"   ❌ 판정
원문 "십 분 뒤에 알려줘"        →  인식 "10분 뒤에 알려줘"       ❌ 판정
```

**둘 다 정답이다.** ASR의 숫자 정규화(ITN) 결과이며 의미는 정확하다.
실제 시스템에서 중요한 것은 받아쓴 글자가 아니라 **최종적으로 기기가 어떻게 동작했는가**다.
그래서 채점 단위를 **툴 호출의 슬롯**으로 바꿨다. — 이 저장소의 [기존 교훈](../../../measure/findings/performance.md)과 같다: *측정 기준이 틀리면 결론도 틀린다.*

### 2-3. 음성 경로 E2E — 슬롯 정확도

`scripts/voice-e2e-bench.py` (TTS=Supertonic → ASR=SenseVoice → LLM 툴콜)

| 모델 | 텍스트 직행 | **음성 경로** | ASR 손실 | LLM 지연 | ASR 지연 |
|---|---|---|---|---|---|
| Qwen3-8B Q4 | 8/8 (100%) | 7/8 (88%) | 12%p | 3.54 s | 0.17 s |
| **Qwen3-4B Q4** | **8/8 (100%)** | **8/8 (100%)** | **0%p** | **2.45 s** | 0.12 s |

**4B가 8B보다 빠르고, 정확도도 뒤지지 않았다.** 음성 명령은 짧고 구조가 단순해서
8B의 추가 능력이 필요 없다.

> ⚠️ **주의 — 이 비교는 확정이 아니다.**
> ① 각 1회 측정(n=1) ② TTS 합성음이 실행마다 미세하게 달라져, 8B가 틀린 1건은
> "안방 불 꺼"가 **"안방 볼꺼"로 잘못 합성·인식된 케이스**였다. 4B 실행 때는
> 같은 문장이 "안방 불꺼"로 정상 인식됐다. **입력이 달랐다.**
> ③ 4B는 검사하지 않은 인자에서 오류를 냈다 — `state=off`인데 `brightness_pct=30`(모순),
> 에어컨에 `mode=heat`. **8B에는 없던 오류다.**
> → 반복 측정 전까지 "4B가 낫다"고 단정하지 말 것.

### 2-4. ASR 실패 원인 분리 — 인식기가 아니라 합성음이 문제였다

`scripts/asr-source-test.py`

```
원어민 실제 녹음 → "조금만 생각을 하면서 살면 훨씬 편할 거야."   ✅ 완벽
mimic3(low) 합성 → "에어컨이 14도로 맞춰줘" (24→14), "15분"(십분→15분)  ❌ 숫자 왜곡
Supertonic 합성  → "에어컨 24도로 맞춰줘", "10분 뒤에 알려줘"     ✅ 의미 정확
```

**같은 인식기인데 결과가 갈렸다.** ASR 성능 문제가 아니라 **TTS 품질 문제**였다.
경량 TTS(64 MB)는 숫자를 왜곡시킬 정도로 발음이 부정확하다.

| | mimic3-kss(low) | **Supertonic-3** |
|---|---|---|
| 크기 | 64 MB | 123 MB |
| RTF | 0.12~0.19 (5~9배속) | 0.57~0.80 (1.5~1.8배속) |
| 품질 | 숫자 왜곡 발생 | 의미 보존 |

> **응답 TTS는 속도와 품질을 맞바꾼다.** 짧은 확인 응답("거실 불 켰습니다")은
> 경량 모델로 충분하고, 긴 안내는 Supertonic이 낫다. **용도별 이중 구성**이 답이다.

### 2-5. ★ 핫패스 라우터 — LLM을 빼면 6,500배 빨라진다

`scripts/hotpath-router.py`

```
정확도   15/15
핫패스   11/15 건이 규칙으로 즉답 · LLM 폴백 4/15 (되묻기·씬·잡담 — 넘기는 게 정답)
지연     평균 0.193 ms / 최대 2.25 ms
대조군   같은 명령을 LLM으로: 1,270 ms  → 약 6,500배
```

**규칙이 LLM보다 나은 경우도 확인됐다.** ASR이 "안방 볼꺼"로 잘못 인식한 입력에서
8B LLM은 **불을 켜버렸지만**(끄라는 명령인데) 규칙 라우터는 관찰된 오인식 패턴을
사전에 등록해 두었기에 정확히 껐다.

> 조명 On/Off, 밝기, 온도, 센서 조회, 타이머 — 가정에서 쓰는 명령은 종류가 적다.
> **소수의 명령이 발화의 대부분을 차지하므로, 그 소수만 규칙으로 잡으면 체감이 바뀐다.**

---

## 3. 아키텍처 — 3단 게이트

```
[상시] 웨이크워드(KWS) + VAD          CPU, 수 밀리와트급        ← 미검증
   ↓ 발화 감지
[0.12s] ASR — SenseVoice int8         CPU 8스레드, GPU 미사용
   ↓ 텍스트
[0.2ms] 핫패스 규칙 라우터             명령의 대부분을 즉답      ★ 여기서 끝나면 체감 0초
   ↓ 규칙이 확신 못 하면
[2.5s] LLM 툴콜 — Qwen3-4B            GPU
   ↓ 슬롯
Home Assistant 실행 → 응답 TTS (경량/고품질 이중)
```

### ★ 학습 루프 — 학생이 자란다

```
LLM이 처리한 발화 + 슬롯  →  로그  →  반복되는 표현을 규칙으로 승격  →  핫패스 확대
```

시간이 지날수록 LLM 호출 비율이 떨어진다. **[Understudy](../project-proposal.md)의 선생-학생 구조와 동일**하며,
"선생 호출률 하락 곡선"이 여기서도 성과 지표가 된다.

### 왜 젯슨인가 — 정직하게

| | |
|---|---|
| ✅ **CPU와 GPU가 동시에 다른 일을 한다** | ASR·TTS는 CPU 8코어, LLM은 GPU. 실측에서 ASR RTF 0.042를 **GPU를 전혀 쓰지 않고** 달성했다 |
| ✅ 통합 메모리 | ASR+TTS+LLM 동시 상주 (아래 §4) |
| ✅ 24시간 대기 4.6 W | 항상 켜 두는 기기의 전제 |
| ✅ 프라이버시 | 집 안 대화가 밖으로 안 나간다 |
| ⚠️ **비전 엔진 미사용** | NVDEC·DLA·OFA·VIC를 안 쓴다. **이 항목만 보면 미니PC로도 된다** |

---

## 4. 메모리 예산 — 여유가 크다

| 구성요소 | 실측/추정 |
|---|---|
| ASR SenseVoice int8 | ~0.3 GB |
| TTS Supertonic int8 | ~0.15 GB |
| VAD + KWS | ~0.05 GB |
| **LLM Qwen3-4B Q4 (ctx 8K)** | **~3.0 GB** (서버 기동 후 실측 여유 9.4 GB) |
| **합계** | **≈ 3.5 GB / 13.4 GB** |

**10 GB가 남는다.** Home Assistant, 로그 DB, 나중에 VLM까지 얹을 수 있다.
영상 프로젝트(Frigate+VLM)와 달리 메모리가 제약이 아니다.

---

## 5. 지연 예산 — 현재와 목표

| 단계 | 현재 | 핫패스 적용 시 |
|---|---|---|
| 발화 종료 감지 (VAD) | 0.3~0.7 s (설정값) | 동일 |
| ASR | 0.12 s | 0.12 s |
| 의도 처리 | **2.45 s (LLM)** | **0.0002 s (규칙)** |
| 응답 TTS (짧은 확인) | 0.3~0.9 s | 0.2 s (경량 TTS) |
| **합계** | **≈ 3.2~4.2 s** | **≈ 0.6~1.0 s** |

**핫패스가 있고 없고가 제품의 성패를 가른다.** 3초짜리 음성 비서는 아무도 안 쓴다.

---

## 6. 다음 단계

### Phase 1 — 하드웨어 (마이크 2만원)

- [ ] USB 마이크 연결 (USB3.1 허브 비어 있음) — **현재 캡처 장치 없음**
- [ ] 실제 사람 음성으로 §2-3 재측정 ← **합성음 측정은 상한선일 뿐**
- [ ] 거리·잡음·에코 조건별 ASR 정확도
- [ ] 스피커 확보 — 현재 출력은 **HDMI뿐**

### Phase 2 — 웨이크워드 (미검증 영역)

- [ ] sherpa-onnx `KeywordSpotter`로 한국어 키워드 검출 가능 여부 확인
- [ ] 안 되면: **TTS로 학습 데이터 합성 → 소형 KWS 학습** ([H3](speech-stack.md))
- [ ] 상시 대기 전력·CPU 점유 측정 (24시간 가동 근거)

### Phase 3 — 실제 연동

- [ ] Home Assistant 설치 (`jetson-containers/smart-home`) + 실기기 1종
- [ ] 핫패스 → HA 서비스 호출 배선
- [ ] 로그 → 규칙 승격 루프 구현

### Phase 4 — 반복 측정 (공개 전 필수)

- [ ] 4B vs 8B **n≥5 반복** — §2-3의 단서 조항 해소
- [ ] 실패 케이스 수집: 되묻기 정책, 반대 동작 방지(끄라 했는데 켜는 오류)
- [ ] 발화 200건 규모 벤치마크 확대

---

## 7. 리스크

| 리스크 | 심각도 | 근거 · 대응 |
|---|---|---|
| **반대로 동작하는 오류** | **높음** | 실측에서 발생 ("안방 볼꺼" → 켜버림). 위험 동작은 확인 응답 필수 |
| 마이크 환경 열화 | 높음 | 합성음은 잡음·거리·에코가 없다. **Phase 1 전까지 모든 수치는 상한선** |
| 되묻지 않는 태도 | 중간 | "불 꺼줘"에서 확인됨. 정책·프롬프트로 해결 |
| 한국어 웨이크워드 부재 | 중간 | 생태계 공백. H3(TTS→KWS 학습)로 우회 |
| 젯슨 강점 미노출 | 중간 | 비전 엔진 미사용. **CPU/GPU 병렬성으로만 정당화됨** |
| n=1 | 중간 | 전 항목 1회 측정. 공개 시 명시 |

---

## 8. 재현

```bash
pip3 install --user sherpa-onnx

# 모델 (refs/models/speech/)
B=https://github.com/k2-fsa/sherpa-onnx/releases/download
curl -L $B/asr-refs/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2 | tar xj
curl -L $B/tts-refs/models/sherpa-onnx-supertonic-3-tts-int8-2026-05-11.tar.bz2 | tar xj
curl -L $B/tts-refs/models/vits-mimic3-ko_KO-kss_low.tar.bz2 | tar xj
curl -LO $B/asr-refs/models/silero_vad.onnx

# LLM 서버 (음성용 — 컨텍스트를 작게)
~/llama.cpp/build/bin/llama-server -m refs/models/Qwen3-4B-Q4_K_M.gguf \
  -ngl 99 -c 8192 --parallel 1 -fa on -ctk q8_0 -ctv q8_0 --jinja \
  --chat-template-kwargs '{"enable_thinking":false}' --host 127.0.0.1 --port 8080

python3 scripts/ha-tool-bench.py       # 한국어 툴콜 정확도
python3 scripts/voice-e2e-bench.py     # ★ 음성 경로 슬롯 정확도
python3 scripts/hotpath-router.py      # ★ 규칙 라우터 대조
```
