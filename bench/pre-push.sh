#!/usr/bin/env bash
# push 전에 값싼 검사만 돌린다.
#
# ★ 훅이 느리면 사람이 --no-verify 를 붙이게 되고, 그러면 없는 것과 같다.
#   6분짜리 테스트는 여기 안 넣는다 — 그건 CI 몫이다.
#   ★ shellcheck 은 즉시 끝나므로 여기 넣는다. CI 에만 뒀다가 **다섯 번 push 하도록
#     빨간불인 걸 몰랐다** — 로컬에서 못 도는 검사는 없는 것과 같다.
#   설치: make hooks   (git 은 훅을 추적하지 않으므로 기기마다 한 번씩)
exec make -C "$(git rev-parse --show-toplevel)" check-fast
