#!/usr/bin/env bash
# 검사기가 **반드시 통과시켜야 하는** 예제 (셸).
#
# 살아 있는 참조: research/performance.md §6 · runtime/README.md
#
# ① 셸 변수로 뿌리를 잡은 경로 — 앞을 떼고 뒷부분만 본다.
#      $LT/docs/handbook.md      → docs/handbook.md 로 풀린다
#      $REPO/environment.md      → environment.md 로 풀린다
# ② 유닛이 쓰는 절대 경로 — 저장소 안쪽만 남긴다.
#      %h/project/project-jetson/runtime/README.md
# ③ 복구 묶음 **안**의 파일. 이 저장소에 없는 게 정상이다.
#      되살리는 순서: 이 묶음의 RESTORE.md — 5-2 절
# ④ 스크립트가 만드는 산출물 이름. 문서가 아니다.
#      결과: results/ 아래 모델별 로그 + summary.md
