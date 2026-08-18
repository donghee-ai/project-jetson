"""Life Trainer 플래너 — 계획 계층 (`docs/contracts-planner.md` §3).

`plan`/`plan_skip`/`plan_check` 은 사람이 선언한 **의도**를 담고,
`slot`/`slot_breakdown` (rollup 소유) 은 기계가 계측한 **실제**를 담는다.
이 패키지는 그 둘을 절대 섞어 저장하지 않는다 — 겹쳐 보는 것은 achieve.py 가
읽기 전용으로 계산할 뿐이다.
"""

from __future__ import annotations
