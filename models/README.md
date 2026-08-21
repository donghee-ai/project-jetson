# models/

가중치 저장 위치. **git에서 제외됨** (`.gitignore`). 전체 약 23 GB.

## LLM (GGUF)

| 파일 | 크기 | 용도 |
|---|---|---|
| `Qwen3-8B-Q4_K_M.gguf` | 4.68 GB | **상시 가동.** 에이전트·툴콜링 (툴 6/6). `llama-server` 가 이걸 물고 있다 |
| `Qwen3-30B-A3B-IQ2_M.gguf` | 9.71 GB | 짧은 대화 (~9K 깊이). 깊이가 깊어지면 8B 에 역전당한다 |
| `EXAONE-3.5-7.8B-Q4_K_M.gguf` | 4.44 GB | 한국어 문서 (토큰 19% 절약) |
| `Qwen3-4B-Q4_K_M.gguf` | 2.33 GB | 음성 에이전트 실측용 — 툴콜 2.45초로 8B(3.54초)보다 빠르다 |

> **8B 스택과 4B 는 동시에 못 올린다.** 가용 13.4 GB 안에서 공존이 안 되고 포트도
> 같다(8080). 둘 중 하나만 돌아간다 —
> [openclaw-agent.md §5](../docs/build/openclaw-agent.md) 참조.

### 재다운로드

```bash
cd models
curl -L -o Qwen3-8B-Q4_K_M.gguf \
  "https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf"
curl -L -o Qwen3-30B-A3B-IQ2_M.gguf \
  "https://huggingface.co/bartowski/Qwen_Qwen3-30B-A3B-GGUF/resolve/main/Qwen_Qwen3-30B-A3B-IQ2_M.gguf"
curl -L -o EXAONE-3.5-7.8B-Q4_K_M.gguf \
  "https://huggingface.co/LGAI-EXAONE/EXAONE-3.5-7.8B-Instruct-GGUF/resolve/main/EXAONE-3.5-7.8B-Instruct-Q4_K_M.gguf"
curl -L -o Qwen3-4B-Q4_K_M.gguf \
  "https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf"
```

> 다운로드 후 `head -c4 <파일>` 이 `GGUF` 인지, 크기가 서버 `x-linked-size` 와
> 일치하는지 확인할 것. 불완전 파일을 로드하면 원인 파악이 어렵다.

성능 비교는 [llm-models.md](../research/llm-models.md) 참조.

---

## 음성 (`speech/`, 약 1.4 GB)

전부 **CPU 로 돈다.** GPU 는 llama-server 가 통째로 쓰고 있어서, 음성 스택을 GPU 에
올릴 여지가 없다 — 그런데도 ASR 은 RTF 0.042 가 나왔다.

| 경로 | 크기 | 무엇 | 실측 |
|---|---|---|---|
| `speech/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/` | 1.1 GB | ASR (int8). 중·영·일·한·광둥어 | **RTF 0.042** — 1.3초 음성을 0.12초에 |
| `speech/sherpa-onnx-supertonic-3-tts-int8-2026-05-11/` | 139 MB | TTS (int8) | RTF 0.57~0.80 (실시간 1.5~1.8배속) |
| `speech/vits-mimic3-ko_KO-kss_low/` | 79 MB | 한국어 TTS (VITS, 저품질·경량) | 합성음 ASR 대조 실험용 |
| `speech/silero_vad.onnx` | 629 KB | 음성 구간 검출 (VAD) | 실마이크 측정에 사용 |

측정 방법·결론은 [speech-stack.md](../research/speech-stack.md) 와
[voice-agent-plan.md](../docs/plans/voice-agent-plan.md), 원본 wav 은
[`../results/speech/`](../results/speech/) 에 있다.

### 재다운로드

sherpa-onnx 계열은 릴리스 tarball 을 풀면 된다.

```bash
cd models/speech
# ASR — SenseVoice (int8)
curl -LO "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2"
tar xf sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2
# 한국어 TTS — mimic3 kss
curl -LO "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-mimic3-ko_KO-kss_low.tar.bz2"
tar xf vits-mimic3-ko_KO-kss_low.tar.bz2
# VAD
curl -LO "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"
```

Supertonic-3 는 배포처가 바뀔 수 있으니 폴더 안의 원본 `README.md` 를 먼저 본다
(상위 저장소 문서라 그 안의 상대 링크는 여기서 열리지 않는다).
