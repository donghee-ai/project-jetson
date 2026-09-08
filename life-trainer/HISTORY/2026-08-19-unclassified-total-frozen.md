# 규칙을 보강했는데 미분류 1위가 그대로였다

- **발견**: 게임 규칙을 넣고 재롤업한 직후, 미분류 목록을 눈으로 확인하다가.
  `slot_breakdown`에는 gaming이 제대로 들어갔는데 이미 분류된 게임 실행 파일들이
  여전히 미분류 상위를 차지했다. 개인별 게임명·사용량은 공개본에서 제거했다.
- **증상**: 이미 분류된 앱이 미분류 목록 상위를 차지한다. 사람이 보기에는
  "규칙이 안 먹었다"로 읽히고, **야간 배치의 `tag_activity` 는 이 목록 상위
  20개를 태깅 대상으로 집으므로 GPU 를 이미 분류된 앱에 쓴다.**

## 원인 — 무엇을 사실이라고 잘못 믿었나

`unclassified.seconds_total` 은 `unclassified_day` 합계로 **재계산**되는 파생값이다.
그런데 재계산 대상이 **이번 롤업에서 미분류로 떨어진 지문(`unclassified_acc`)뿐**이었다.

```python
conn.executemany("UPDATE unclassified SET seconds_total = (...) WHERE fingerprint = ?",
                 [(fp,) for fp in unclassified_acc])   # ← 이번에 본 것만
```

규칙이 보강되면 어떤 지문은 **더 이상 미분류가 아니게 된다.** 그 지문은
`unclassified_acc` 에 없다. 그래서:

1. `unclassified_day` 의 해당 날짜 행은 지워지고 (DELETE 는 날짜 단위라 잘 지워진다)
2. `unclassified.seconds_total` 은 **아무도 다시 계산해주지 않아** 옛날 값으로 굳는다

"이번에 본 것만 갱신하면 된다"가 깨진 가정이다. 그 전제는 **미분류 집합이 줄어들지
않을 때만** 성립한다. 규칙 보강은 정확히 그 집합을 줄이는 행위다.

## 수정

재계산 대상을 테이블 전체로 바꾸고, 어느 날짜에도 실적이 안 남은 지문은 지운다.

```python
conn.execute("UPDATE unclassified SET seconds_total = (...), hits = (...)")  # 전수
conn.execute("DELETE FROM unclassified WHERE seconds_total = 0 AND llm_category IS NULL")
```

- **전수 재계산 비용은 무의미하다.** 미분류 지문은 수십~수백 개 규모이고,
  `unclassified_day(fingerprint)` 에 인덱스가 있다.
- **LLM 태깅 결과가 있는 행은 남긴다.** 규칙으로 흡수됐는지 사람이 확인하기 전에
  지우면 태깅에 쓴 GPU 시간이 증발한다.
- 이미 굳어 있던 실기기 데이터는 재롤업 한 번으로 정리됐다 (미분류 32개 → 26개,
  상위가 게임 3종 → bambu-studio·SearchHost 로 바뀜).

## 방어

`tests/test_rollup.py` 에 2건:

- `test_unclassified_total_drops_when_rule_absorbs_it` — 규칙이 지문을 흡수하면
  목록에서 사라지고, 그 시간은 새 카테고리의 `slot_breakdown` 에 그대로 있다
- `test_absorbed_fingerprint_survives_if_llm_tagged` — 태깅된 지문은 0초가 되어도 남는다

둘 다 **실제 YAML 파일을 써서 규칙을 바꾼다.** dict 를 주입하면 파싱 단계를 건너뛰어
현실(사람이 rules.yaml 을 편집한다)과 달라진다.

또 `worker.tag_activity` 의 후보 쿼리에 `seconds_total > 0` 을 추가했다 — 굳은 값이
다시 생기더라도 배치가 그걸 태깅하지는 않게.

## 교훈

**파생값을 부분만 갱신하는 코드는 "집합이 줄어드는 날"에 조용히 틀린다.**
이 저장소가 이미 두 번 겪은 부류다(하루 경계 3곳 어긋남, 플래너 PNG 6시간 밀림).
갱신 범위를 좁히는 최적화는 그 좁힘이 **모든 변화 방향**에서 성립하는지 확인하고 넣는다.
증가만 보고 감소를 안 본 것이 이번 실수다.

또 하나: 이 버그는 **테스트가 아니라 목록을 눈으로 봐서** 나왔다. 재롤업 로그는
"미분류 지문 10개"라고 정상적으로 찍고 있었다 — 그 10개는 `unclassified_day` 기준이라
맞는 숫자였고, 틀린 것은 사람이 보는 목록이었다.
