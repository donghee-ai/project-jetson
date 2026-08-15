# reference/

실제로 동작하는 타 프로젝트 클론. **git에서 제외됨** (각자 별도 저장소).

| 저장소 | 최신 갱신 | 참고 포인트 |
|---|---|---|
| `jetson-containers` (dusty-nv) | 2026-06 | 젯슨 컨테이너 표준. llama.cpp·ollama·vllm 빌드 레시피 |
| `jetson-inference` (dusty-nv) | 2025-10 | NVIDIA Hello AI World. TensorRT 비전 추론 레퍼런스 |
| `jetson-orin-nano-field-kit` | 2026-02 | Orin LLM+비전+카메라 통합 킷 (조사 시점 최다 star) |
| `llama-cpp-jetson` | 2026-07 | 젯슨용 llama.cpp 사전 빌드 바이너리 |

## 복원

```bash
cd reference
git clone --depth 1 https://github.com/dusty-nv/jetson-containers.git
git clone --depth 1 https://github.com/dusty-nv/jetson-inference.git
git clone --depth 1 https://github.com/implyinfer/jetson-orin-nano-field-kit.git
git clone --depth 1 https://github.com/2oby/llama-cpp-jetson.git
```

전체 조사 결과는 [../research/](../research/) 참조.
