#!/usr/bin/env bash
# 문서 참조가 전부 실재하는지 검사한다. **두 종류**를 본다.
#
#   ① 마크다운의 상대경로 링크
#   ② ★ 코드가 주석·독스트링에서 가리키는 문서와 그 `§` 절 번호   (2026-08-31 추가)
#
# ── ②를 왜 붙였나 ───────────────────────────────────────────────────
#   문서를 개명·분할했는데 코드가 안 따라간 일이 **두 번** 있었고, 둘 다 몰랐다.
#     08-27  known-issues 를 issues/ 로 분할 → 코드 14곳의 `§N` 이 가리킬 절이 사라짐
#     08-28  openclaw-setup 을 개명          → 코드 14곳이 없는 파일을 가리킴
#   ①은 `*.md` 안의 링크만 봤다. **코드 주석은 검사 대상이 아니었다.**
#   깨진 가정은 "문서를 옮기면 참조도 따라온다" 였다 — 경위는
#   `HISTORY/2026-08-31-the-references-did-not-follow-the-file.md`.
#
# ── 이 검사는 언제 꺼지나 (CLAUDE.md §1) ────────────────────────────
#   가리키는 문서와 절이 실재하면 조용하다. 문서를 옮기면 **그 자리에서 한 번** 울고,
#   참조를 고치면 끝난다. 누적되지 않고, 고친 직후 바로 꺼진다.
#
# ── 무엇을 "이 저장소의 문서" 로 보나 ───────────────────────────────
#   헐거우면 못 잡고, 빡빡하면 사람이 꺼 버린다. 경계를 이렇게 그었다.
#
#   본다    · 폴더가 붙은 것 — 첫 칸이 이 저장소의 폴더 이름 (`operate/…` `docs/…`)
#           · `§` 절 번호가 붙은 것         (`operate/notes/agent-gateway.md §4-5`)
#           · 맨 이름이지만 같은 이름이 이 저장소에 있는 것 (`environment.md`)
#   안 본다 · 남의 트리의 파일 — OpenClaw 워크스페이스가 갖는 `AGENTS.md`·`SOUL.md`,
#             복구 묶음 안의 RESTORE 안내문. 이 저장소에 없는 게 정상이다
#           · 코드 안의 문자열 리터럴 — 테스트가 만드는 `tmp_path/"x.md"`
#           · 저장소 폴더 이름이 아닌 예시 경로 (`memo/x.md`)
#
#   ★ 확실히 검사받게 하려면 **폴더를 붙이거나 `§` 를 붙인다.**
#     맨 이름은 남의 트리와 구분할 수 없어서 있을 때만 확인한다 — 이건 한계이고
#     제외 목록이 아니다. 제외 목록은 두지 않는다 (CLAUDE.md §1).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

# ★ 파일시스템이 아니라 **git 기준**으로 본다.
#   워킹트리에만 있는 것(git mv 뒤 남은 빈 디렉토리 · gitignore 된 measure/results/speech/)을
#   실재로 세면 로컬은 통과하고 CI 는 깨진다. 실제로 그렇게 두 번 놓쳤다.
TRACKED=$(mktemp); git ls-files > "$TRACKED"
DIRNAMES=$(mktemp); awk -F/ '{for(i=1;i<NF;i++) print $i}' "$TRACKED" | sort -u > "$DIRNAMES"
trap 'rm -f "$TRACKED" "$DIRNAMES"' EXIT

# operate/tools/fixtures/ 는 검사기의 시험지다. 여기를 본 검사로 세면 자기 시험지에 걸려 넘어진다.
FIXTURES='operate/tools/fixtures/check-links'

exists() {  # $1 = 저장소 루트 기준 경로
  local t="${1%/}"
  grep -qxF "$t" "$TRACKED" && return 0          # 추적 중인 파일
  grep -q "^${t}/" "$TRACKED" && return 0        # 그 아래 추적 파일이 있는 디렉토리
  return 1
}

# ── 자기 검사 ───────────────────────────────────────────────────────
#   ★ 검사기를 믿지 말고 재 본다. 규칙만 있고 검사가 없어서 네 번 실패한 저장소다.
#   must-fail 은 반드시 잡고, must-pass 는 반드시 통과시켜야 한다.
#   오탐이 나오면 그 예제를 must-pass 에 **추가**한다 (CLAUDE.md §1).
if [ "${1:-}" = "--self-test" ]; then
  fail=0
  out=$(bash "$0" "$FIXTURES"/must-fail.py "$FIXTURES"/must-fail.sh 2>&1)
  # ★ 종료코드만 보면 안 된다. 다섯 예제 중 하나만 잡아도 0 이 아니게 나온다 —
  #   그러면 "검사기가 헐거워졌다" 를 못 잡는다. **두 층이 다 살아 있는지**를 본다.
  if ! grep -q '그런 문서가 없다' <<<"$out"; then
    echo "  ❌ 자기검사: **없는 문서**를 못 잡는다 — 파일 검사층이 죽었다"; fail=1
  elif ! grep -q '그 절이 없다' <<<"$out"; then
    echo "  ❌ 자기검사: **없는 절**을 못 잡는다 — 절 검사층이 죽었다"; fail=1
  else
    echo "  ✅ 자기검사: must-fail 을 잡는다 ($(grep -c '❌' <<<"$out") 건 · 두 층 다)"
  fi

  out=$(bash "$0" "$FIXTURES"/must-pass.py "$FIXTURES"/must-pass.sh 2>&1)
  n=$(sed -n 's/.*참조 \([0-9]*\) 개 검사.*/\1/p' <<<"$out")
  if grep -q '❌' <<<"$out"; then
    echo "  ❌ 자기검사: must-pass 를 잡았다 — 오탐이다. 사람이 검사기를 끄게 된다"
    grep '❌' <<<"$out"; fail=1
  elif [ "${n:-0}" -lt 1 ]; then
    # ★ 아무것도 안 세고 통과하는 것은 통과가 아니다. must-pass 안의 **살아 있는**
    #   참조까지 걸러 버렸다는 뜻이라, 오탐 대신 미탐으로 조용해진 상태다.
    echo "  ❌ 자기검사: must-pass 에서 참조를 하나도 안 셌다 — 통과가 공허하다"; fail=1
  else
    echo "  ✅ 자기검사: must-pass 를 통과시킨다 (살아 있는 참조 $n 개 확인)"
  fi
  exit $fail
fi

# ── ① 마크다운 링크 ────────────────────────────────────────────────
#   기록 문서(progress/ · HISTORY/)는 제외한다 — 그날의 사실을 적은 것이라
#   오늘 구조에 맞춰 고치면 안 된다. 대신 이정표 문서가 옛 경로를 받아준다.
md_links() {
  local broken=0 checked=0 f d t rel
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
           | grep -v '^life-trainer/docs/progress/' \
           | grep -v '^life-trainer/HISTORY/')
  echo "  링크 $checked 개 검사 · 깨진 것 $broken 개"
  return $(( broken > 0 ))
}

# ── ② 코드 → 문서 참조 ─────────────────────────────────────────────

# 정규식 메타문자를 막는다. 경로는 ASCII 라 LC_ALL=C 로 바이트 단위로 다룬다.
esc() { LC_ALL=C sed 's/[].[^$*+?(){}|\\]/\\&/g' <<<"$1"; }

# 참조를 실제 파일로 푼다. 성공하면 추적 경로를 찍는다.
#   ① 참조한 파일 기준 상대  ② 저장소 루트 기준  ③ 경로 접미사 일치
#   ③이 있어야 `docs/rag-plan.md` 가 `life-trainer/docs/rag-plan.md` 로 풀린다 —
#   코드는 자기 프로젝트 기준으로 짧게 쓴다.
resolve_doc() {
  local p="$1" d="$2" cand
  cand=$(realpath -m --relative-to=. "$d/$p")
  grep -qxF "$cand" "$TRACKED" && { printf '%s\n' "$cand"; return 0; }
  grep -qxF "$p"    "$TRACKED" && { printf '%s\n' "$p";    return 0; }
  cand=$(grep -m1 -E "(^|/)$(esc "$p")\$" "$TRACKED")
  [ -n "$cand" ] && { printf '%s\n' "$cand"; return 0; }
  return 1
}

# 이 참조가 **이 저장소의 문서**를 가리키는가. 위 머리말의 경계가 여기 있다.
is_repo_doc() {
  local p="$1" sec="$2" first
  [ -n "$sec" ] && return 0                      # `§` 가 붙었으면 문서를 가리킨 것이 분명하다
  case "$p" in
    ../*|./*) return 0;;                         # 상대 경로 — 위치를 명시했다
    */*) first=${p%%/*}
         grep -qxF "$first" "$DIRNAMES" && return 0
         return 1;;                              # 저장소 폴더 이름이 아니다 (예: `memo/x.md`)
    *)   grep -qE "(^|/)$(esc "$p")\$" "$TRACKED" && return 0
         return 1;;                              # 맨 이름이고 이 저장소에 없다 (예: `AGENTS.md`)
  esac
}

# `§N` · `§N-M` 이 그 문서에 실재하는가.
#   이 저장소는 두 가지로 쓴다 — `### 7-2.` 처럼 번호를 통째로 적기도 하고,
#   `§4-5` 처럼 「4장의 5번」 을 뜻하기도 한다. 둘 다 받는다.
section_ok() {
  local doc="$1" want="$2"
  awk -v want="$want" '
    function headnum(line,   lvl, rest, n) {
      lvl = 0; while (substr(line, lvl+1, 1) == "#") lvl++
      if (lvl == 0 || substr(line, lvl+1, 1) != " ") return ""
      rest = substr(line, lvl+2)
      sub(/^[ \t]+/, "", rest)
      sub(/^★[ ]*/, "", rest)                       # 「### ★ …」 형태
      if (match(rest, /^[0-9]+(-[0-9A-Za-z]+)?[.)]/)) {
        LVL = lvl
        return substr(rest, RSTART, RLENGTH-1)
      }
      return ""
    }
    BEGIN { i = index(want, "-"); if (i > 0) { A = substr(want,1,i-1); B = substr(want,i+1) } }
    /^#/ {
      num = headnum($0)
      if (num == "") next
      if (num == want) { ok = 1; exit }
      if (A != "") {
        if (inA && LVL <= Alvl) inA = 0
        if (num == A && !inA)  { inA = 1; Alvl = LVL; next }
        if (inA && num == B)   { ok = 1; exit }
      }
    }
    END { exit ok ? 0 : 1 }
  ' "$doc"
}

# ★ 성능. 이 검사는 pre-push 훅이 부른다 — 느리면 사람이 훅을 끈다 (CLAUDE.md §1).
#   그래서 두 가지를 한다: awk 가 `.md` 가 든 줄만 넘기고, 판정 결과를 캐시한다.
#   (줄마다 파이프라인을 돌렸더니 4분이었다.)
declare -A REPO_DOC_CACHE=() RESOLVE_CACHE=()

code_refs() {
  local broken=0 checked=0 f dir py lineno prose line raw cand path sec doc key
  for f in "$@"; do
    [ -f "$f" ] || continue
    grep -q '\.md' "$f" || continue
    dir=$(dirname "$f"); py=0; [ "${f##*.}" = "py" ] && py=1
    while IFS=$'\001' read -r lineno prose line; do
      # $VAR/ 로 시작하는 경로는 그 앞을 떼고 본다 — `$LT/docs/handbook.md` 의 뒷부분이 참조다.
      raw=$(printf '%s\n%s\n' "$prose" "$line" \
            | LC_ALL=C sed 's|\$[{]\{0,1\}[A-Za-z_][A-Za-z0-9_]*[}]\{0,1\}/||g' \
            | grep -oE '((\.\.?/)*[A-Za-z0-9_][A-Za-z0-9_./-]*\.md)( ?`? ?§ ?[0-9]+(-[0-9A-Za-z]+)?)?' \
            | sort -u)
      [ -z "$raw" ] && continue
      while IFS= read -r cand; do
        [ -z "$cand" ] && continue
        # 유닛의 `%h/project/project-jetson/…` 같은 절대경로는 저장소 안쪽만 남긴다.
        # 닫는 백틱이 끼어 있어도 절 번호로 읽는다 — `docs/contracts.md` §0 꼴이 흔하다.
        path=${cand%%§*}; path=${path%% }; path=${path%\`}; path=${path%% }
        path=${path##*project-jetson/}
        sec=${cand#*§}; [ "$sec" = "$cand" ] && sec="" || sec=${sec// /}
        # 산문 밖(코드 리터럴)에서는 `§` 가 붙은 것만 참조로 센다.
        if [ -z "$sec" ] && [ "${prose#*"$path"}" = "$prose" ]; then continue; fi

        key="$path"
        if [ -z "${REPO_DOC_CACHE[$key]+x}" ]; then
          if is_repo_doc "$path" ""; then REPO_DOC_CACHE[$key]=y; else REPO_DOC_CACHE[$key]=n; fi
        fi
        [ -n "$sec" ] || [ "${REPO_DOC_CACHE[$key]}" = y ] || continue

        checked=$((checked+1))
        key="$dir|$path"
        if [ -z "${RESOLVE_CACHE[$key]+x}" ]; then
          RESOLVE_CACHE[$key]=$(resolve_doc "$path" "$dir" || true)
        fi
        doc=${RESOLVE_CACHE[$key]}
        if [ -z "$doc" ]; then
          echo "  ❌ $f:$lineno → \`$path\` — 그런 문서가 없다"
          broken=$((broken+1)); continue
        fi
        if [ -n "$sec" ] && ! section_ok "$doc" "$sec"; then
          echo "  ❌ $f:$lineno → \`$path §$sec\` — 문서는 있는데 그 절이 없다 ($doc)"
          broken=$((broken+1))
        fi
      done <<<"$raw"
    # 파이썬 삼중따옴표 구역과 `#` 뒤만 "산문" 으로 넘긴다. 코드 안의 문자열 리터럴
    # (테스트가 만드는 임시 파일 이름)을 문서 참조로 세지 않기 위한 것이다.
    done < <(awk -v PY="$py" -v DQ='"""' -v SQ="'''" '
      {
        line = $0; prose = ""
        if (PY) {
          rest = line
          while (1) {
            if (inq) {
              p = index(rest, Q)
              if (p == 0) { prose = prose " " rest; break }
              prose = prose " " substr(rest, 1, p-1); rest = substr(rest, p+3); inq = 0
            } else {
              d = index(rest, DQ); s = index(rest, SQ)
              if (d > 0 && (s == 0 || d < s)) { p = d; Q = DQ }
              else if (s > 0)                 { p = s; Q = SQ }
              else break
              rest = substr(rest, p+3); inq = 1
            }
          }
        }
        h = index(line, "#")
        if (h > 0) prose = prose " " substr(line, h+1)
        if (index(line, ".md") > 0) printf "%d\001%s\001%s\n", NR, prose, line
      }' "$f")
  done
  echo "  코드→문서 참조 $checked 개 검사 · 끊긴 것 $broken 개"
  return $(( broken > 0 ))
}

# ── 실행 ────────────────────────────────────────────────────────────
#   인자를 주면 그 파일들만 ②로 본다 (자기검사가 쓴다).
if [ $# -gt 0 ]; then
  code_refs "$@"; exit $?
fi

rc=0
md_links || rc=1
# shellcheck disable=SC2046  # 파일 목록을 단어로 넘긴다 — 경로에 공백이 없는 저장소다
code_refs $(git ls-files '*.py' '*.sh' '*.mjs' '*.ps1' '*.service' '*.timer' \
                         '*.yml' '*.yaml' '*.toml' 'Makefile' '*/Makefile' \
            | grep -v "^$FIXTURES/") || rc=1
exit $rc
