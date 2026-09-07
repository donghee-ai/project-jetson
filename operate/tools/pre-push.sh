#!/usr/bin/env bash
# push 전 관문.
#
# ★ 2026-09-07 에 **테스트를 넣었다.** 그전까지는 `check-fast` 만 돌렸고, 근거는
#   이렇게 적혀 있었다 — *"6분짜리 테스트는 여기 안 넣는다. 그건 CI 몫이다."*
#
#   그 근거가 두 군데서 틀렸다:
#     ① **CI 가 실제로 안 돌았다.** 실사 시점에 안 올라간 커밋이 13개였고,
#        CI 가 마지막으로 본 커밋은 이틀 전 것이었다. push 를 안 하면 CI 는 없는 것과 같다
#     ② **6분이 아니다.** 실측 2분 42초다. 숫자가 낡아서 결정을 왜곡했다
#
#   그래서 테스트를 앞으로 당긴다. push 는 자주 하는 일이 아니고, 2분 42초는
#   *"틀린 걸 원격에 올리고 나중에 발견"* 보다 싸다.
#
# ★ 급할 때는 `--no-verify` 대신 `LT_FAST_PUSH=1` 을 쓴다. 전자는 **전부** 끄지만
#   후자는 링크·문서·shellcheck 은 그대로 돌린다 — 사람이 큰 망치를 집지 않게
#   작은 망치를 옆에 둔다.
#
# 설치: make hooks   (git 은 훅을 추적하지 않으므로 기기마다 한 번씩)
ROOT=$(git rev-parse --show-toplevel)

if [ -n "${LT_FAST_PUSH:-}" ]; then
  echo "  LT_FAST_PUSH=1 — 테스트를 건너뛴다 (CI 가 본다)"
  exec make -C "$ROOT" check-fast
fi

exec make -C "$ROOT" check
