# 레퍼런스 조사 — GitHub 생태계 전수 스캔

> 조사일: 2026-08-15 / 도구: [`scripts/gh-api-collect.py`](../scripts/gh-api-collect.py) · [`scripts/gh-research*.mjs`](../scripts/) · [`scripts/gh-analyze.py`](../scripts/gh-analyze.py)
> 원본: [`results/gh-final.json`](../results/gh-final.json)
> 제외 조건: 자율주행 관련, 아카이브됨, 1년 6개월 이상 방치, 별 3개 미만

---

## 0. 조사 방법과 그 결과

### Playwright 스크래핑은 실패했다 — API가 답이었다

| 방식 | 검색어 | 성공 | 성공률 | 수집 |
|---|---|---|---|---|
| Playwright 1차 | 24 | 12 | 50% | 97 |
| Playwright 2차 (지연 8초 + 재시도) | 43 | 13 | **30%** | 99 |
| Playwright 3차 | 29 | 9 | 31% | 56 |
| **GitHub REST API** | **64** | **64** | **100%** | **856** |

**지연을 늘릴수록 오히려 성공률이 떨어졌다.** github.com 웹 검색은 미인증 요청을
공격적으로 차단한다. 반면 `api.github.com`은 미인증도 10회/분을 안정적으로 허용하고,
**라이선스(SPDX)·최종 푸시일·아카이브 여부**를 정확히 제공한다.

분류 기준 4개 중 3개가 API에서만 신뢰성 있게 나온다.

```
① 젯슨 적합성   설명·토픽        (양쪽 가능)
② 에이전트 용도  설명·토픽        (양쪽 가능)
③ 유지보수      pushed_at        ← API만 정확
④ 라이선스      spdx_id          ← API만 제공
```

> **교훈: GitHub 대량 조사는 API를 쓸 것.** 스크래핑은 데이터도 부실하고 차단도 심하다.

### 최종 집계

```
통합 946개 → 필터링 → 704개
  제외: 1년6개월+ 방치 135 / 별 3 미만 84 / 아카이브 21 / 자율주행 2
  라이선스 확보 727 / 최근 6개월 내 갱신 586
```

### 점수 산식

```
유지보수 30 + 라이선스 20 + 인기(로그) 25 + 젯슨신호 20 + 다중매칭 5 = 100
```

---

## 1. ★ 젯슨 생태계의 현실 — 얇다

젯슨을 **명시적으로 언급**한 저장소는 704개 중 **93개**뿐이다.
그중 별 1,000개 이상은 10개 미만이다.

**이것이 기회이자 경고다.** 남이 만들어둔 게 없어 직접 해야 하지만,
반대로 **젯슨용으로 제대로 만들면 그 자체가 희소가치**가 된다.

### 필수 도구 — 지금 당장 설치할 것

| 저장소 | ★ | 라이선스 | 갱신 | 왜 |
|---|---|---|---|---|
| **`rbonghi/jetson_stats`** | 2,611 | AGPL-3.0 | 2026-08-09 | **`jtop`** — 젯슨 모니터링 사실상 표준. `tegrastats`보다 훨씬 낫다 |
| `dusty-nv/jetson-containers` | 4,830 | NOASSERTION | 2026-08-10 | 컨테이너 생태계 표준. llama.cpp·ollama·vllm 빌드 레시피 |
| `dusty-nv/jetson-inference` | 8,963 | MIT | 2025-10-16 | Hello AI World. TensorRT 비전 추론 교과서 |

> `jtop`을 이 프로젝트 초반에 알았다면 `tegrastats` 파싱 스크립트를 안 짜도 됐다.

### 추론 엔진 · 최적화

| 저장소 | ★ | 라이선스 | 갱신 | 비고 |
|---|---|---|---|---|
| `pytorch/TensorRT` | 2,987 | BSD-3 | 2026-08-14 | PyTorch → TensorRT 컴파일러 |
| `google-ai-edge/LiteRT-LM` | 6,201 | Apache-2.0 | 2026-08-15 | 구글 엣지 LM 런타임. llama.cpp 대안 후보 |
| `RightNow-AI/picolm` | 1,907 | MIT | 2026-02-22 | **$10 보드에서 1B LLM** — 극한 경량화 사례 |
| `Michael-A-Kuykendall/shimmy` | 5,748 | Apache-2.0 | 2026-08-06 | 순수 Rust WebGPU 추론, OpenAI 호환 |
| `Qengineering/Jetson-Nano-Ubuntu-20-image` | 974 | BSD-3 | 2026-07-25 | 젯슨 OS 이미지 빌드 |

### 비전 · TensorRT

| 저장소 | ★ | 라이선스 | 갱신 | 비고 |
|---|---|---|---|---|
| `triple-mu/YOLOv8-TensorRT` | 1,803 | MIT | 2026-08-14 | YOLOv8 TensorRT 가속 |
| `infracv/rf-detr-cpp` | 198 | Apache-2.0 | 2026-08-14 | 프로덕션급 C++/TensorRT 엔진 |
| `enazoe/yolo-tensorrt` | 1,202 | MIT | 2026-04-12 | darknet → TensorRT |
| **`kornia/vision-rt`** | 14 | Apache-2.0 | 2026-08-13 | **"NVIDIA Jetson용 실시간 신경망 비전"** — kornia 조직의 신규 프로젝트. 별은 적지만 주목 |
| `LSH9832/edgeyolo` | 528 | Apache-2.0 | 2026-04-20 | 엣지 실시간 앵커프리 검출기 |
| `Intellindust-AI-Lab/EdgeCrafter` | 310 | Apache-2.0 | 2026-08-14 | [TMLR 26] 엣지용 경량 ViT |
| `NVIDIA/DeepStream` | 199 | NOASSERTION | 2026-08-07 | NVIDIA 공식 DeepStream 모노레포 |
| `TzuHuanTai/RaspberryPi-WebRTC` | 981 | Apache-2.0 | 2026-08-14 | Pi·젯슨 실시간 WebRTC 스트리밍 |

---

## 2. ★ 영상 감시 — 이미 만들어져 있다

앞서 "젯슨 최적 프로젝트"로 구상했던 **카메라 + 로컬 검출 + 이벤트 알림**은
**이미 성숙한 구현이 존재한다.** 처음부터 만들 이유가 없다.

| 저장소 | ★ | 라이선스 | 갱신 | 내용 |
|---|---|---|---|---|
| **`blakeblackshear/frigate`** | **35,103** | **MIT** | 2026-08-14 | **IP카메라 실시간 로컬 객체검출 NVR.** 이 분야의 사실상 표준 |
| `SharpAI/DeepCamera` | 2,998 | MIT | 2026-06-18 | AI 카메라 스킬 플랫폼, AI NVR |

**Frigate가 결정적이다.** 35k 스타에 MIT, 활발히 유지보수되며 Coral TPU·GPU 가속을
지원한다. **"젯슨으로 CCTV 분석"은 Frigate를 얹으면 끝난다.**

### 그래서 남는 빈틈

Frigate는 **객체 검출까지**만 한다. 비어 있는 것은 그 위층이다.

```
Frigate           : "사람 감지됨" (바운딩 박스)
                          ↓  ← 여기가 비어 있다
VLM + LLM 레이어  : "택배기사가 상자를 놓고 감 / 모르는 사람이 창문을 봄"
                    자연어 검색 · 이벤트 요약 · 오탐 필터링
```

`yiliu-li/Visual-Agentic-Memory`(15★), `mohamed-abo-taha/local-vision-agent`(0★)처럼
개념 구현은 있으나 **아무도 제대로 만들지 않았다.** Frigate에 붙이는 VLM 레이어가
현실적인 진입점이다.

---

## 3. 에이전트 — 레드오션, 하지만 쓸 만한 것들

프레임워크를 새로 만드는 건 의미가 없다. **기존 것에 젯슨 특화 레이어를 얹는 쪽**이 맞다.

### 경량 · 자체호스팅 (젯슨에 적합)

| 저장소 | ★ | 라이선스 | 비고 |
|---|---|---|---|
| **`HKUDS/nanobot`** | 47,004 | MIT | **초경량 자체호스팅 개인 AI 에이전트** — 메모리 제약 환경에 유리 |
| `Mintplex-Labs/anything-llm` | 64,718 | MIT | 올인원. RAG+에이전트+문서 |
| `zhayujie/CowAgent` | 46,510 | MIT | 슈퍼 AI 어시스턴트 & 에이전트 하네스 |
| `zylon-ai/private-gpt` | 57,443 | Apache-2.0 | 프라이빗 AI API 레이어 |
| `langbot-app/LangBot` | 17,415 | Apache-2.0 | 프로덕션급 에이전틱 IM 플랫폼 |

### 대형 프레임워크 (참고용)

`NousResearch/hermes-agent` 230k · `langchain-ai/langchain` 144k ·
`infiniflow/ragflow` 88k · `ruvnet/ruflo` 67k · `langgenius/dify` 152k

### MCP 생태계 — 에이전트 툴 확장

| 저장소 | ★ | 라이선스 | 비고 |
|---|---|---|---|
| `bytedance/UI-TARS-desktop` | 38,590 | Apache-2.0 | 멀티모달 AI 에이전트 스택 |
| `github/github-mcp-server` | 32,259 | MIT | GitHub 공식 MCP 서버 |
| `ChromeDevTools/chrome-devtools-mcp` | 49,183 | Apache-2.0 | 코딩 에이전트용 Chrome DevTools |
| `DeusData/codebase-memory-mcp` | 38,965 | MIT | 코드 인텔리전스 MCP |
| `upstash/context7` | 60,762 | MIT | 최신 문서 컨텍스트 제공 |

> **툴 콜링 검증 결과 Qwen3-8B가 6/6**이었으므로 MCP 서버를 붙일 수 있다.
> 30B-A3B(4/6)·EXAONE(2/6)로는 MCP 에이전트가 불안정하다.

---

## 4. RAG · 메모리 — 메모리 제약에 맞는 대안들

일반 RAG는 임베딩 모델을 별도로 올려야 해서 13.4 GB 예산에 부담이다.
**가벼운 대안**이 여럿 나와 있다.

| 저장소 | ★ | 라이선스 | 왜 젯슨에 좋은가 |
|---|---|---|---|
| **`VectifyAI/PageIndex`** | 35,186 | MIT | **벡터리스(Vectorless)** — 임베딩 모델 불필요. 추론 기반 문서 인덱스 |
| **`StarTrail-org/LEANN`** | 12,786 | MIT | [MLsys2026] **저장공간 극소화** RAG |
| `memvid/memvid` | 16,216 | Apache-2.0 | 에이전트 메모리 레이어. 복잡한 RAG 대체 |
| `HKUDS/LightRAG` | 38,871 | MIT | [EMNLP2025] 경량 RAG |
| `alibaba/zvec` | 15,444 | Apache-2.0 | **인프로세스** 경량 벡터DB — 별도 서버 불필요 |
| `topoteretes/cognee` | 30,030 | Apache-2.0 | AI 메모리 플랫폼 |

**`PageIndex`와 `zvec`가 특히 맞는다.** 전자는 임베딩 모델을 안 올려도 되고,
후자는 벡터DB 서버 프로세스를 안 띄워도 된다. 둘 다 메모리를 아낀다.

---

## 5. 문서 처리 — grant-radar 직결

| 저장소 | ★ | 라이선스 | 갱신 | 비고 |
|---|---|---|---|---|
| **`PaddlePaddle/PaddleOCR`** | 87,662 | Apache-2.0 | 2026-07-22 | PDF·이미지 → 구조화 데이터. 한중일 강함 |
| `opendataloader-project/opendataloader-pdf` | 28,409 | Apache-2.0 | 2026-08-13 | AI용 PDF 파서 |
| `Unstructured-IO/unstructured` | 15,311 | Apache-2.0 | 2026-08-14 | 문서 → 구조화 데이터 |
| `ocrmypdf/OCRmyPDF` | 34,448 | MPL-2.0 | 2026-08-06 | 스캔 PDF에 OCR 텍스트 레이어 추가 |
| `tesseract-ocr/tesseract` | 75,921 | Apache-2.0 | 2026-08-14 | 고전 OCR 엔진 |

> ⚠️ **HWP 파서는 조사에서 발견되지 않았다.** `hwp korean document parser` 검색이
> 유의미한 결과를 내지 못했다. grant-radar의 HWP 파싱은 여전히 직접 해결해야 하는
> 문제이며, 이것이 해자가 된다는 [기획 문서](../docs/grant-radar-plan.md)의 판단은 유효하다.

---

## 6. 음성 — 젯슨 GPU 활용도 높음

| 저장소 | ★ | 라이선스 | 비고 |
|---|---|---|---|
| **`k2-fsa/sherpa-onnx`** | 14,185 | Apache-2.0 | **STT·TTS·화자분리 통합. 엣지 명시 지원** |
| `QwenAudio/SenseVoice` | 9,072 | MIT | 다국어 음성인식 (한국어 포함) |
| `Zackriya-Solutions/meetily` | 29,145 | MIT | 프라이버시 우선 AI 회의 어시스턴트 |

`sherpa-onnx`가 최적이다 — ONNX Runtime 기반이라 aarch64에서 잘 돌고,
STT/TTS/VAD를 한 번에 해결한다.

---

## 7. 홈 오토메이션 — 24시간 저전력의 정석

| 저장소 | ★ | 라이선스 | 비고 |
|---|---|---|---|
| `home-assistant/core` | 89,926 | Apache-2.0 | 홈 오토메이션 표준 |
| `homeassistant-ai/ha-mcp` | 4,381 | MIT | **Home Assistant MCP 서버** — LLM 에이전트 연동 |
| `blakeblackshear/frigate` | 35,103 | MIT | NVR (§2 참조). HA 연동 |

`ha-mcp`가 흥미롭다. Home Assistant를 MCP로 노출하면 **로컬 LLM이 집 안의
기기를 직접 제어**할 수 있다. 툴 콜링 6/6인 Qwen3-8B와 조합 가능하다.

---

## 8. 운영 · 관측

| 저장소 | ★ | 라이선스 | 비고 |
|---|---|---|---|
| `Helicone/helicone` | 6,071 | Apache-2.0 | LLM 관측 플랫폼 |
| `openlit/openlit` | 2,689 | Apache-2.0 | OpenTelemetry 기반 AI 엔지니어링 |
| `evidentlyai/evidently` | 7,810 | Apache-2.0 | ML·LLM 관측 |
| `maximhq/bifrost` | 7,322 | Apache-2.0 | 고성능 AI 게이트웨이 |
| `niklasfrick/spark-dashboard` | 92 | MIT | **하드웨어 + LLM 추론 실시간 모니터링** |
| `lance0/rookery` | 6 | Apache-2.0 | **llama-server 관리 커맨드센터** |

---

## 9. 한국어 — 생태계가 매우 얇다

| 저장소 | ★ | 라이선스 | 비고 |
|---|---|---|---|
| `HeegyuKim/open-korean-instructions` | 472 | - | **공개 한국어 instruction 데이터셋 모음** |
| `entelecheia/eKoNLPy` | 56 | MIT | 경제 분석용 한국어 NLP |
| `J-Seo/K-HALU` | 37 | MIT | 한국어 환각 벤치마크 |

**한국어 도구 생태계는 사실상 비어 있다.** 조사된 것 중 별 500개를 넘는 것이 없다.
EXAONE의 한국어 토큰 효율 19% 우위([llm-models.md](llm-models.md))를 감안하면
한국어 특화 도구는 여전히 공백 영역이다.

---

## 10. 종합 판단 — 젯슨으로 할 만한 것

### ❌ 하지 말 것

| 아이템 | 이유 |
|---|---|
| 에이전트 프레임워크 자체 제작 | 230k~47k 스타 프로젝트가 즐비. 승산 없음 |
| RAG 프레임워크 자체 제작 | 동일 |
| CCTV 객체검출 NVR | **Frigate(35k, MIT)가 이미 완성형** |
| 범용 OCR | PaddleOCR·Tesseract로 충분 |

### ✅ 빈틈이 있는 것

| 아이템 | 근거 | 난이도 |
|---|---|---|
| **Frigate 위의 VLM 레이어** | 객체검출 위층(자연어 이해·검색)이 비어 있음 | 중 |
| **젯슨 성능 데이터 공개** | 젯슨 명시 저장소 93개뿐, 실측 벤치마크 자료 희소 | **낮음** |
| **HWP 파싱** | 조사에서 발견 안 됨. 한국 공공문서 필수 | 높음 |
| **한국어 로컬 LLM 도구** | 생태계 거의 비어 있음 | 중 |
| **로컬 LLM + Home Assistant** | `ha-mcp` 존재하나 조합 사례 적음 | 낮음 |

### 가장 낮은 진입장벽

**이미 확보한 실측 데이터를 공개하는 것.**

이 프로젝트에서 측정한 것들 — MAXN 전력모드에서 GPU TPC가 절반 게이팅된다는 사실,
Super Mode가 디바이스 트리 제약으로 불가하다는 원인 규명, 메모리 대역폭 실측 60 GB/s,
컨텍스트 깊이별 성능 저하 곡선, 모델 3종 툴 콜링 비교 — 는 **젯슨 생태계 93개
저장소 어디에도 정리되어 있지 않다.**

`Andyyyy64/whichllm`(6.3k★)처럼 "내 하드웨어에서 뭐가 도는가"를 다루는 도구는 있으나
**젯슨 데이터가 없다.**

---

## 11. 추가 클론 권장

현재 [`reference/`](../reference/)에 4개가 있다. 아래를 추가하면 좋다.

```bash
cd reference
git clone --depth 1 https://github.com/rbonghi/jetson_stats.git        # jtop
git clone --depth 1 https://github.com/blakeblackshear/frigate.git     # NVR 표준
git clone --depth 1 https://github.com/k2-fsa/sherpa-onnx.git          # STT/TTS
git clone --depth 1 https://github.com/VectifyAI/PageIndex.git         # 벡터리스 RAG
git clone --depth 1 https://github.com/HKUDS/nanobot.git               # 경량 에이전트
```

### 즉시 설치 권장

```bash
sudo pip3 install -U jetson-stats && sudo reboot   # jtop
```

`tegrastats` 파싱보다 훨씬 정확하고, 전력모드·팬·클럭 제어까지 된다.

---

## 12. 미조사 영역

- GitHub 외 소스 (HuggingFace Spaces, GitLab, 논문 구현체)
- 각 후보의 **실제 젯슨 동작 검증** — 현재는 설명·토픽 기반 추정
- 메모리 요구사항 실측 — 13.4 GB 예산 내 동작 여부
- 한국어 성능 — 도구별 한국어 지원 수준
