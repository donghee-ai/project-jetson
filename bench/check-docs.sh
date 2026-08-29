#!/usr/bin/env bash
# 문서가 **운영 지표를 옮겨 적고 있는지** 검사한다.
#
# ★ 왜 이게 있나
#   이 저장소는 같은 숫자가 서로 다르게 적힌 사고를 네 번 겪었다
#   (테스트 수 두 번 · 분류 규칙 수 한 번 · 실데이터 한 번).
#   1차: "실제로 돌려서 나온 값을 쓴다"  → 실패
#   2차: "적을 자리를 하나로 줄인다"      → 두 커밋 만에 실패
#   3차: "명령으로 대체한다" (make status) → **강제하는 것이 없어서** 또 어긋났다
#   이 스크립트가 4차다. 규칙을 검사로 바꾼다.
#
#   숫자 일반을 막지 않는다. **운영 지표**만 막는다 — 토큰 수·메모리·속도 같은
#   측정값은 잰 시점이 있는 사실이라 문서에 적는 게 맞다. 여기서 막는 것은
#   `make status` 가 뽑아 주는, **오늘과 내일이 다른** 값이다.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

TARGET_LIST=${1:-}   # 비우면 git 추적 md 전체. 테스트는 파일 하나를 넘긴다

# ── 자기 검사 ───────────────────────────────────────────────
#   ★ 검사기를 믿지 말고 재 본다. 규칙만 있고 검사가 없어서 네 번 실패한 저장소다.
if [ "$TARGET_LIST" = "--self-test" ]; then
  fx=bench/fixtures/check-docs
  fail=0
  if bash "$0" "$fx/must-fail.md" >/dev/null 2>&1; then
    echo "  ❌ 자기검사: must-fail.md 를 통과시켰다 — 검사기가 헐거워졌다"; fail=1
  else echo "  ✅ 자기검사: must-fail.md 를 잡는다"; fi
  if bash "$0" "$fx/must-pass.md" >/dev/null 2>&1; then
    echo "  ✅ 자기검사: must-pass.md 를 통과시킨다"
  else
    echo "  ❌ 자기검사: must-pass.md 를 잡았다 — 오탐이다. 사람이 검사기를 끄게 된다"
    bash "$0" "$fx/must-pass.md"; fail=1
  fi
  exit $fail
fi

# ── 예외 ────────────────────────────────────────────────────
#   기록 문서는 **그날의 사실**이라 고치면 안 된다. check-links.sh 와 같은 목록이다.
#   maintenance-plan*.md 는 "문서가 이렇게 틀렸다" 를 인용하는 문서라 예외다.
#   bench/fixtures/ 는 검사기의 시험지다 — 여기를 세면 자기 시험지에 걸려 넘어진다.
EXCLUDE_RE='^(Life_Trainer/docs/progress/|Life_Trainer/HISTORY/|Life_Trainer/docs/issues/|Life_Trainer/docs/archive/|docs/archive/|bench/fixtures/|maintenance-plan)'

# ── 막는 것 ─────────────────────────────────────────────────
#   패턴 | 사람이 읽을 이름 | 대신 쓸 명령
PATTERNS=(
  '이벤트 [0-9][0-9,]{3,}|events [0-9][0-9,]{3,}|이벤트 총 [0-9][0-9,]{3,}|aw_event [0-9][0-9,]{3,}::이벤트 수::make status'
  '문서 [0-9][0-9,]{3,}(건|개)?|문서 총 [0-9][0-9,]{3,}::문서 수::make status'
  '요약 [0-9][0-9,]{3,}::요약 수::make status'
  '테스트 (수 )?[0-9],?[0-9]{3}(개)?|[0-9],[0-9]{3}(개)? 테스트|pytest[^\n]*[0-9],[0-9]{3} passed::테스트 개수::make test'
  '(done|failed|cancelled) [0-9][0-9,]{2,}::GPU 잡 수::make status'
  '(분류 )?규칙 [0-9]{2,}개::분류 규칙 수::make status'   # 한 자리는 부분집합 서술이라 뺀다
  '링크 [0-9]{3,}\s?개::문서 링크 수::make links'
  # ★ 2026-08-29 추가. 백업 타이머 1개와 감시 타이머 2개를 붙이자 문서 8곳이
  #   한꺼번에 낡았는데 **검사기가 통과시켰다** — 이것도 같은 성격의 운영 지표인데
  #   패턴에 없었다. "무엇이 운영 지표인가" 를 처음에 좁게 잡은 것이 원인이다.
  '타이머 [0-9]+\s?개::타이머 수::systemctl --user list-timers'
  '유닛( 파일)? [0-9]+\s?개::유닛 수::ls systemd/'
  '(상시 )?서비스 [0-9]+\s?개::서비스 수::make status'
)

# ── 스냅숏 예외 ─────────────────────────────────────────────
#   날짜가 붙은 스냅숏 블록은 허용한다. "2026-08-28 13:41 스냅숏" 같은 줄이
#   앞 5줄 안에 있으면 그 줄은 통과 — 참고값임이 표시돼 있기 때문이다.
SNAPSHOT_RE='스냅숏|snapshot|생성 시각|조회'

if [ -n "$TARGET_LIST" ]; then
  FILES="$TARGET_LIST"
else
  FILES=$(git ls-files '*.md' | grep -vE "$EXCLUDE_RE")
fi

bad=0 checked=0
for f in $FILES; do
  [ -f "$f" ] || continue
  checked=$((checked+1))
  for entry in "${PATTERNS[@]}"; do
    re=${entry%%::*}; rest=${entry#*::}; name=${rest%%::*}; cmd=${rest##*::}
    while IFS=: read -r lineno text; do
      [ -z "${lineno:-}" ] && continue
      # 앞 5줄 안에 스냅숏 표시가 있으면 통과
      from=$(( lineno > 5 ? lineno - 5 : 1 ))
      if sed -n "${from},${lineno}p" "$f" | grep -qE "$SNAPSHOT_RE"; then continue; fi
      # 인라인 예외 — 그 줄이나 앞줄에 표시가 있으면 통과.
      # ★ 사유를 같이 적게 한다. 표시만 남고 이유가 없으면 다음 사람이 못 지운다.
      if sed -n "$(( lineno > 1 ? lineno - 1 : 1 )),${lineno}p" "$f" | grep -qF 'check-docs: ok'; then continue; fi
      echo "  ❌ $f:$lineno  [$name] → 지우고 \`$cmd\` 를 가리킬 것"
      echo "        $(echo "$text" | cut -c1-90)"
      bad=$((bad+1))
    done < <(grep -nE "$re" "$f" 2>/dev/null)
  done
done

echo "  문서 $checked 개 검사 · 옮겨 적은 지표 $bad 건"
[ "$bad" -eq 0 ]
