#!/usr/bin/env bash
# life-trainer 의존성의 **버전 고정본**을 이 기기의 .venv 에서 다시 뽑는다.
#
# ★ 무엇을 설치할지는 `pyproject.toml` 이 정한다. 이 파일은 **어느 판을 쓸지**만 정한다 —
#   pip 의 `-c`(constraints)가 정확히 그렇게 동작한다. 둘을 한 파일에 합치면
#   "왜 이게 깔렸나" 와 "왜 이 판인가" 가 섞여서 다음 사람이 못 읽는다.
#
# 왜 필요했나: `pyproject.toml` 의 의존성이 전부 `>=` 라 CI(매번 새로 설치)와
#   이 기기(몇 주 전 설치)가 서로 다른 판을 쓸 수 있었다. 한쪽만 깨졌을 때
#   **버전이 용의선상에서 빠진다** — 이 저장소가 이미 겪은 부류다
#   (life-trainer/HISTORY/2026-08-27-the-suite-was-green-on-this-machine-only.md).
#
# ★ 정본은 **이 기기의 .venv** 다. 여기가 실제로 돌아가는 것이 확인된 조합이라서다.
#   손으로 고치지 않는다.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

OUT=life-trainer/constraints.txt
VENV=life-trainer/.venv/bin

[ -x "$VENV/pip" ] || { echo "❌ $VENV/pip 이 없다 — venv 부터 만들 것" >&2; exit 1; }

tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
{
  echo "# life-trainer 의존성 **버전 고정** — pip constraints 파일."
  echo "#"
  echo "# 무엇을 설치할지는 pyproject.toml 이 정한다. 여기는 **어느 판을 쓸지**만 정한다."
  echo "# 여기 없는 것은 안 깔리는 게 아니라, 판을 안 고정하는 것이다."
  echo "#"
  echo "# 손으로 고치지 않는다 — 이 기기의 .venv 를 정본으로 다시 뽑는다:  make lock"
  echo "# 왜 있는지는 operate/tools/lock-deps.sh 머리말에 있다."
  echo "#"
  echo "# 뽑은 시각: $(date '+%Y-%m-%d %H:%M %Z') · python $("$VENV/python" -V 2>&1 | cut -d' ' -f2)"
  echo
  "$VENV/pip" freeze --exclude-editable
} > "$tmp"

# 판이 하나도 안 나오면 덮어쓰지 않는다 — 빈 고정본은 고정이 아니라 해제다.
n=$(grep -cvE '^#|^$' "$tmp")
[ "$n" -gt 0 ] || { echo "❌ 고정할 것이 하나도 없다 — 덮어쓰지 않았다" >&2; exit 1; }

mv "$tmp" "$OUT"; trap - EXIT
echo "  $OUT — $n 개 고정"
git diff --stat -- "$OUT" | tail -1
