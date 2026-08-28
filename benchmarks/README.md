# benchmarks/ — 모델 13종 실측 (2026-08-18)

**루트 README 의 ①②③ 중 ②(어떤 모델이 적합한가)와, 대역폭 해석 정정의 근거가 여기 있다.**

원래 [`Andyyyy64/whichllm`](https://github.com/Andyyyy64/whichllm) 에 넣을 대역폭 값을
검증하려고 돌린 측정인데, 결과가 **이 저장소의 대표 숫자 해석을 뒤집어서** 같이 들어왔다.

## 무엇이 나왔나

| | |
|---|---|
| **대역폭 102.4 GB/s** | Q4_K_M 4종(4.44~8.38 GiB)에서 **MAPE 1.0%, bias +0.2%**. 차선책은 32% 빗나감 |
| **Q8_0 트래픽 86.4 GB/s** | **데이터시트 피크의 84%.** → `membw.cu` 의 60.0 은 하드웨어 천장이 아니었다 |
| **소형 모델(<3 GiB)** | 대역폭 지배 영역 **밖**. 추정식이 +34~42% 과대예측 |
| **MoE 읽기비율 0.213** | dense 대조군으로 직접 측정 (가정 0.108 의 2.0배) |

두 번째 줄이 [`../research/performance.md §2`](../research/performance.md) 의
*"60 GB/s = 사양의 58%"* 서술을 무효로 만들었다. 자세한 것은
[benchmark-results.md §5-2](benchmark-results.md).

## 구성

```
benchmark-results.md      전체 분석. §3 대역폭 검증 · §5 달성 대역폭
bench/
  run-bench.sh              llama-bench 실행 + tegrastats 동시 수집
  telemetry.py              tegrastats 에서 VDD_IN·tj·RAM 요약
  analyze.py                whichllm 의 estimate_tok_per_sec() 를 **직접 호출**해 대조
  moe-control.py            MoE 읽기비율을 dense 대조군으로 역산
  collect-env.sh            측정 환경 스냅숏
  fetch-models*.sh          가중치 내려받기 (모델은 이 저장소에 없다)
results/
  raw/run-20260818T*/       모델별 llama-bench 출력 + *.tegrastats.txt (VDD_IN 1Hz)
  raw/sweep*.txt            후보 대역폭 적합 스윕 로그
  processed/analysis.json   집계 결과 — 그림이 이걸 읽는다
```

## 다시 돌리려면

```bash
bash bench/fetch-models.sh            # 가중치를 ~/models/ 로
bash bench/run-bench.sh               # llama-bench × 모델 + tegrastats
python3 bench/analyze.py <run_dir>    # → results/processed/analysis.json
python3 bench/telemetry.py <run_dir>  # 전력·온도 요약
```

## 한계 — 그대로 적어 둔다

- **측정 1회다.** 반복 측정으로 분산을 잡지 않았다
- **전력 모드는 MAXN(`pmode:0000`) 하나뿐이다.** 15W·25W 에서는 재지 않았다 —
  그래서 와트당 성능을 전력 모드별로 비교하는 그림은 아직 못 그린다
- `bench/analyze.py` 는 업스트림 `whichllm` 패키지를 import 한다.
  그 저장소가 없으면 안 돈다 (작업 사본은 `~/project/opensource/upstream-whichllm/`)

> whichllm 기여 트랙(패치·PR 본문·이슈 초안)은 **이 저장소에 없다.**
> `~/project/opensource/contribution/` 에 있고, 제출 여부는 별도 판단이다.
