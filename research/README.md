# research/ — 조사와 실측

**이 폴더의 숫자는 전부 이 기기에서 직접 잰 것이다.** 데이터시트 값은 "사양치" 로
따로 표기하고 실측과 나란히 놓는다 — 이 저장소의 존재 이유가 그 둘이 다르다는 것이다.

| 문서 | 무엇을 쟀나 | 측정일 |
|---|---|---|
| [hardware.md](hardware.md) | 하드웨어 실측, 전력모드, **Super Mode 불가 원인**, 메모리 예산 | 2026-08-13 |
| [performance.md](performance.md) | 대역폭, 컨텍스트 깊이별 성능 곡선, 양자화 품질 | 2026-08-14 |
| [llm-models.md](llm-models.md) | 모델 3종 비교 — 툴 콜링·속도·한국어 토큰 효율 | 2026-08-14 |
| [reference-survey.md](reference-survey.md) | GitHub 생태계 전수 스캔 (704개) — 무엇이 이미 있고 무엇이 비었나 | 2026-08-15 |
| [speech-stack.md](speech-stack.md) | 음성 스택 — ASR·KWS·실시간 통역·TTS 생태계 + 본 기기 실측 | 2026-08-15 |

원본 로그·JSON 은 [`../results/`](../results/), 측정 도구는 [`../scripts/`](../scripts/) 에 있다.

---

## 읽는 순서

처음이면 **hardware → performance → llm-models** 순서다. 뒤 문서가 앞 문서의
숫자를 전제로 한다.

```
hardware.md      60 GB/s · 13.4 GB · MAXN 이 무엇을 푸는가
      ↓          (이 셋이 아래 전부의 상한이다)
performance.md   그래서 깊이 32K 에서 3.96 tok/s 로 떨어진다
      ↓
llm-models.md    그래서 8B 가 30B 를 9,600 토큰에서 추월한다
```

`reference-survey.md` 와 `speech-stack.md` 는 독립적으로 읽어도 된다 —
"만들 가치가 있는가" 를 판단하려고 바깥을 본 기록이다.

## 이미 정정된 것 (문서 안에 공지가 있다)

- **EXAONE 툴 콜링 2/6 은 무효** — 모델 능력이 아니라 채팅 템플릿에 `tools` 렌더링
  코드가 없어서였다. llama.cpp 가 툴 정의를 조용히 버렸다
  ([llm-models.md](llm-models.md) §서두)

원래 결론을 지우지 않고 정정 공지를 위에 붙이는 방식으로 남긴다.
**틀린 과정을 지우면 왜 틀렸는지도 같이 사라진다.**

## 이 측정들이 실제로 쓰인 곳

| 측정 | 어디서 제약이 됐나 |
|---|---|
| 가용 메모리 13.4 GB | llama-server 를 두 프로젝트가 **공유**하는 이유 ([openclaw-agent.md §5](../docs/build/openclaw-agent.md)) |
| 깊이 32K 에서 −70% | Life Trainer 의 "LLM 입력 3~5K 로 끊기" 규칙 ([CLAUDE.md](../Life_Trainer/CLAUDE.md)) |
| 툴 콜링 6/6 (Qwen3-8B) | 에이전트 모델 선정 |
| 프롬프트 처리 295 tok/s | 시스템 프롬프트 3층 분리 — 12,541 토큰이 곧 58초였다 |
