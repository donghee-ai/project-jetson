# archive/ — 역할이 끝난 문서

**여기 있는 것은 지금의 계약이 아니다.** 왜 그 판단을 했는지 되짚을 때만 본다.
현재 상태를 알고 싶으면 [HANDOFF.md](../../HANDOFF.md) 부터 읽는다.

| 문서 | 무엇이었나 | 지금 |
|---|---|---|
| [contracts-v1.md](contracts-v1.md) | 모듈 계약서 v1 (2026-08-16) | [`../contracts.md`](../contracts.md) Part I |
| [contracts-planner.md](contracts-planner.md) | 계획 계층·웹 플래너 (2차) | 〃 Part II |
| [contracts-v2.md](contracts-v2.md) | 레퍼런스 흡수 (3차) | 〃 Part III |
| [verify-contracts.py](verify-contracts.py) | 계약서 시그니처를 코드와 대조하는 검증기 | 그대로 쓴다 (아래) |

## 왜 합쳤나

셋이 **시대순 누적**이었고 *"충돌하면 나중 것이 이긴다"* 였다. 즉 **현재 계약을
알려면 셋을 읽고 직접 무효화 판정을 해야 했다.** 2026-08-21 진행 기록이 이미
*"어느 것이 현행인지"* 를 문제로 적어 뒀다.

합치면서 세 문서의 시그니처 주장 **189건을 전부 코드와 대조했고, 틀린 계약은
하나도 없었다.** 실제 충돌은 `timeutil.py` 함수 4개뿐이었다 — 셋을 읽어야 했던
이유가 그 4개였다.

## 검증기 다시 돌리기

```bash
.venv/bin/python docs/archive/verify-contracts.py | python3 -m json.tool | head -40
```

기본 대상은 통합본이다. **원본 3종을 다시 재려면** 파일 안의 `DOCS` 를 바꾼다.

> 한계: `### ` 헤더에서 대상 파일을 물려받는 방식이라, 경로가 아닌 헤더
> (`### 마이그레이션 …`) 아래의 코드 블록은 **앞 헤더의 파일로 잘못 귀속된다.**
> 실제로 그 때문에 `migrate` 가 오탐으로 잡혔다. 결과를 볼 때 감안한다.
