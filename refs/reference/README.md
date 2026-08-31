# reference/ — 타 프로젝트 클론 (이 폴더에는 없다)

> **2026-08-28: 클론을 `~/reference/` 로 옮겼다.** 531MB 였고 git 에서 제외돼 있어
> 저장소에는 원래 안 들어갔지만, **"폴더 좀 보자"는 사람에게는 보였다.**
> 이 파일은 무엇을 어느 시점으로 봤는지를 남긴다.

## 무엇을 봤나 — 문서가 인용하는 커밋

| 저장소 | 커밋 | 왜 봤나 |
|---|---|---|
| [`dusty-nv/jetson-containers`](https://github.com/dusty-nv/jetson-containers) | `70c149a` | 젯슨 컨테이너 표준. llama.cpp·ollama·vllm 빌드 레시피 · `packages/speech` |
| [`dusty-nv/jetson-inference`](https://github.com/dusty-nv/jetson-inference) | `45da40a` | NVIDIA Hello AI World. TensorRT 비전 추론 레퍼런스 |
| [`implyinfer/jetson-orin-nano-field-kit`](https://github.com/implyinfer/jetson-orin-nano-field-kit) | `a31a10a` | Orin LLM+비전+카메라 통합 킷 (조사 시점 최다 star) |
| [`2oby/llama-cpp-jetson`](https://github.com/2oby/llama-cpp-jetson) | `0d76587` | 젯슨용 llama.cpp 사전 빌드 바이너리 |
| [`blakeblackshear/frigate`](https://github.com/blakeblackshear/frigate) | `11f8786` | 비전 트랙 판단용 — [접었다](../docs/archive/vision-agent-plan.md) |

**커밋 해시를 적어 두는 이유**: 이 저장소가 세 번 데인 실패가
*"문서를 읽고 실물이라고 믿었다"* 다. 위 커밋들은 **소스를 직접 열어 확인한 시점**이다.

## 복원

```bash
mkdir -p ~/reference && cd ~/reference
git clone https://github.com/dusty-nv/jetson-containers.git        && git -C jetson-containers checkout 70c149a
git clone https://github.com/dusty-nv/jetson-inference.git         && git -C jetson-inference checkout 45da40a
git clone https://github.com/implyinfer/jetson-orin-nano-field-kit.git && git -C jetson-orin-nano-field-kit checkout a31a10a
git clone https://github.com/2oby/llama-cpp-jetson.git             && git -C llama-cpp-jetson checkout 0d76587
git clone https://github.com/blakeblackshear/frigate.git           && git -C frigate checkout 11f8786
```

전체 생태계 조사(704개 전수 스캔)는 [../research/reference-survey.md](../research/reference-survey.md).
