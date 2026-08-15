# models/

GGUF 가중치 저장 위치. **git에서 제외됨** (`.gitignore`).

## 현재 보유

| 파일 | 크기 | 용도 |
|---|---|---|
| `Qwen3-8B-Q4_K_M.gguf` | 4.68 GB | 에이전트·툴콜링 (툴 6/6) |
| `Qwen3-30B-A3B-IQ2_M.gguf` | 9.71 GB | 짧은 대화 (~9K 깊이) |
| `EXAONE-3.5-7.8B-Q4_K_M.gguf` | 4.44 GB | 한국어 문서 (토큰 19% 절약) |

## 재다운로드

```bash
cd models
curl -L -o Qwen3-8B-Q4_K_M.gguf \
  "https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf"
curl -L -o Qwen3-30B-A3B-IQ2_M.gguf \
  "https://huggingface.co/bartowski/Qwen_Qwen3-30B-A3B-GGUF/resolve/main/Qwen_Qwen3-30B-A3B-IQ2_M.gguf"
curl -L -o EXAONE-3.5-7.8B-Q4_K_M.gguf \
  "https://huggingface.co/LGAI-EXAONE/EXAONE-3.5-7.8B-Instruct-GGUF/resolve/main/EXAONE-3.5-7.8B-Instruct-Q4_K_M.gguf"
```

> 다운로드 후 `head -c4 <파일>` 이 `GGUF` 인지, 크기가 서버 `x-linked-size` 와
> 일치하는지 확인할 것. 불완전 파일을 로드하면 원인 파악이 어렵다.

성능 비교는 [../research/llm-models.md](../research/llm-models.md) 참조.
