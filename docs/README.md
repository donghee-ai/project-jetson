# docs/ — 구축 기록과 기획

**측정 결과 자체는 여기 없다.** 그건 [`research/`](../research/) 에 있다.
이 폴더는 "그 측정을 근거로 무엇을 만들었고, 무엇을 만들기로 했고,
무엇을 접었는가" 를 남긴다.

폴더가 곧 상태다.

| 폴더 | 뜻 | 다시 읽을 이유 |
|---|---|---|
| **[build/](build/)** | 지금 이 기기에서 **돌고 있는 것**의 구축 기록 | 재현·복구·함정 회피 |
| **[plans/](plans/)** | 하기로 했지만 **아직 안 끝난 것** | 다음에 뭘 할지 |
| **[archive/](archive/)** | 판단이 끝났거나 **전제가 무효화된 것** | 같은 검토를 두 번 하지 않기 위해 |

---

## build/ — 가동 중

| 문서 | 무엇 | 상태 |
|---|---|---|
| [build/llm-runtime.md](build/llm-runtime.md) | llama.cpp CUDA 빌드, llama-server 운영, 모델 로드 | ✅ `llama-server.service` active |
| [build/openclaw-agent.md](build/openclaw-agent.md) | OpenClaw 게이트웨이 + 로컬 LLM 결합, **함정 14가지** | ✅ 게이트웨이 active (Slack 채널만 꺼짐) |

실행 자산(유닛·스크립트)은 문서가 아니라 [`openclaw-setup/`](../openclaw-setup/) 에 있다.

> **`build/openclaw-agent.md §4` 는 코드에서 직접 참조된다.** `llm/client.py`,
> `llm/tools.py`, `llm/trigger.py` 등이 주석에서 §번호로 가리킨다
> (`pattern` 금지·모델 id 확인 같은 하드웨어 제약의 출처). 절을 재배열하지 말 것.

## plans/ — 미완

| 문서 | 무엇 | 어디까지 |
|---|---|---|
| [plans/voice-agent-plan.md](plans/voice-agent-plan.md) | 완전 로컬 한국어 음성 에이전트 — 3단 게이트 설계 | **Phase 0 완료** (마이크 없이 전 구간 실측). 마이크 확보 후 Phase 1 |
| [plans/opensource-plan.md](plans/opensource-plan.md) | 측정 자료 공개·`whichllm` 기여 | 스키마 확인·패치 작성 완료, **PR 승인 대기** |
| [plans/grant-radar-plan.md](plans/grant-radar-plan.md) | 지원사업 자격요건 매칭 서비스 | **미착수.** 사업화 검토 대상 |

> `plans/opensource-plan.md` 는 이 저장소 **바깥**(`../../opensource/`)의 산출물을
> 참조한다. GitHub 에서는 그 링크가 열리지 않는다.

## archive/ — 끝난 검토

**지우지 않는 이유**: 왜 안 골랐는지가 남아 있지 않으면 같은 후보를 다시 검토하게 된다.

| 문서 | 무엇 | 왜 보관인가 |
|---|---|---|
| [archive/project-candidates.md](archive/project-candidates.md) | 후보 7개 전체 비교 (CCTV·음향·인덱서 등) | 권고는 **B(음향 이상 감지)** 였으나, 실제로는 [Life Trainer](../Life_Trainer/) 로 갔다 |
| [archive/project-proposal.md](archive/project-proposal.md) | Understudy — VLM 이 검출기를 가르치는 폐루프 카메라 | **미채택.** 카메라 하드웨어 미확보. 선생-학생 구조는 음성 쪽으로 계승됐다 |
| [archive/vision-agent-plan.md](archive/vision-agent-plan.md) | Frigate 위에 VLM 레이어 얹기 | **전제 무효.** 클론한 Frigate 소스에 이미 구현돼 있었다 (문서 §서두 정정 참조) |

---

## 실제로 만들어진 것은 어디에

세 갈래 중 살아남아 상시 구동에 들어간 것은 **[Life Trainer](../Life_Trainer/)** 다.
자체 문서 트리를 갖고 있다 — [HANDOFF.md](../Life_Trainer/HANDOFF.md) 부터 읽는다.
