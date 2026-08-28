#!/usr/bin/env bash
# 마크다운의 상대경로 링크가 전부 실재하는지 검사한다.
#
# 기록 문서(progress/ · HISTORY/)는 제외한다 — 그날의 사실을 적은 것이라
# 오늘 구조에 맞춰 고치면 안 된다. 대신 이정표 문서가 옛 경로를 받아준다.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# ★ 파일시스템이 아니라 **git 기준**으로 본다.
#   워킹트리에만 있는 것(git mv 뒤 남은 빈 디렉토리 · gitignore 된 results/speech/)을
#   실재로 세면 로컬은 통과하고 CI 는 깨진다. 실제로 그렇게 두 번 놓쳤다.
TRACKED=$(mktemp); git ls-files > "$TRACKED"
trap 'rm -f "$TRACKED"' EXIT

exists() {  # $1 = 저장소 루트 기준 경로
  local t="${1%/}"
  grep -qxF "$t" "$TRACKED" && return 0          # 추적 중인 파일
  grep -q "^${t}/" "$TRACKED" && return 0        # 그 아래 추적 파일이 있는 디렉토리
  return 1
}

broken=0 checked=0
while IFS= read -r f; do
  d=$(dirname "$f")
  while IFS= read -r t; do
    case "$t" in http*|mailto:*|'') continue;; esac
    checked=$((checked+1))
    rel=$(realpath -m --relative-to=. "$d/$t")
    exists "$rel" || { echo "  ❌ $f → $t"; broken=$((broken+1)); }
  # ★ 앵커(#...)는 sed 말고 cut 으로 뗀다 — 이 로케일의 sed 는 `.` 가 한글을 못 넘어서
  #   `s/#.*$//` 가 한글 앵커에 조용히 실패한다 (LC_ALL=C 로도 되지만 cut 이 분명하다).
  done < <(grep -oE '\]\([^)]+\)' "$f" | sed -E 's/^\]\(//; s/\)$//' | cut -d'#' -f1)
done < <(git ls-files '*.md' \
         | grep -v '^Life_Trainer/docs/progress/' \
         | grep -v '^Life_Trainer/HISTORY/')

echo "  링크 $checked 개 검사 · 깨진 것 $broken 개"
[ "$broken" -eq 0 ]
