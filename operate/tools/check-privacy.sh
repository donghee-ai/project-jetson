#!/usr/bin/env bash
# 개인 열람·창 기록이 저장소에 들어오는 것을 막는다.
#
# ## 왜 생겼나 (2026-09-08)
#
# 세 가지가 커밋되어 GitHub 까지 올라갔다 — 저장소가 비공개라 유출은 아니었지만
# 남길 이유가 없는 것들이었다:
#
#   · 시안 목업 데이터에 실제 창 제목 11개 (학교 포털 · 비공개 프로젝트명 · SSH 호스트)
#   · 시안 스크린샷 2장의 "오늘의 기록" 패널 (대화 제목 · 소모임 이름)
#   · 문서·테스트 픽스처에 실제 방문 사이트 · 게시글 제목 · 게임 계정명
#
# 이력까지 지웠지만, **지운 것으로는 다시 들어오는 것을 못 막는다.**
#
# ## ★ 이 파일 안에 개인정보를 적지 않는다
#
# 이게 이 검사기의 제일 어려운 제약이다. 막을 값을 목록으로 적으면
# **그 목록이 곧 개인정보이고, 그게 저장소에 올라간다.** 그래서 값이 아니라
# **모양**으로 잡는다 — 실제 기록에만 나타나고 예시값에는 없는 형태다.
#
# 특정 낱말을 꼭 막아야 하면 `private-terms.local.txt` 에 적는다.
# 그 파일은 `.gitignore` 대상이라 저장소에 안 들어간다 (아래 ⑥).
#
# ## 언제 안 울리나 (저장소 규칙 §1)
#
# 규칙 전부 **예시값을 통과시키도록** 짜여 있다 — `[SSH: host]`,
# `op.gg/summoners/kr/example`, `m.example-forum.com`, `watch?v=%08d`.
# 도입 시점에 현재 트리 적중 0건을 확인했다. 새로 걸리면 그건 진짜다.
#
# 도메인 허용목록 방식은 **일부러 안 썼다.** 새 수집원을 넣을 때마다 울려서
# 결국 꺼진다 — 헐거워지는 것보다 빡빡해서 꺼지는 쪽이 이 저장소의 실패 이력이다.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 1

SELF_TEST=""
[ "${1:-}" = "--self-test" ] && SELF_TEST=1
HISTORY_MODE=""
[ "${1:-}" = "--history" ] && HISTORY_MODE=1

TERMS_FILE=operate/tools/private-terms.local.txt

scan() {   # scan <파일목록파일> → 위반을 stdout 으로
  local list=$1
  # ① VS Code 원격 창 제목의 호스트. 예시는 [SSH: host]
  xargs -a "$list" grep -nHP '\[SSH: (?!host\])[A-Za-z0-9_.-]+\]' 2>/dev/null \
    | sed 's/^/[SSH 호스트] /'
  # ② 게임 계정명. 예시는 .../example
  xargs -a "$list" grep -nHP 'op\.gg/summoners/[a-z]{2}/(?!example)[A-Za-z0-9%가-힣]+' 2>/dev/null \
    | sed 's/^/[게임 계정명] /'
  # ★ "URL 경로의 긴 숫자 = 실제 글 id" 규칙은 **넣었다가 뺐다** (2026-09-08).
  #   공공데이터 API 데이터셋 id(`data.go.kr/data/15125364/`), GPU 장치 경로
  #   (`17000000.gpu`), 1GiB 상수(`1073741824`)까지 잡아 9건이 울었다.
  #   정상 내용에 우는 규칙은 검사기 전체를 꺼지게 만든다 — 그리고 이 규칙이 잡던
  #   진짜 사건(`m.<커뮤니티>.com/best/<긴 숫자>`)은 아래 ⑤가 이미 잡는다.
  # ④ 유튜브 영상 id. 예시는 %08d 나 x
  xargs -a "$list" grep -nHP 'youtube\.com/watch\?v=(?![x%])[A-Za-z0-9_-]{8,}' 2>/dev/null \
    | sed 's/^/[영상 id] /'
  # ⑤ 모바일 사이트 주소 = 폰 열람 기록의 모양. 예시는 m.example-*
  xargs -a "$list" grep -nHP '\bm\.(?!example-)[a-z0-9-]+\.(com|net|kr|co\.kr)' 2>/dev/null \
    | sed 's/^/[폰 열람 기록] /'
  # ⑥ 곡·영상 메타데이터. 공개 예시는 예시/Example 으로 시작한다.
  xargs -a "$list" grep -niHP '"(artist|album)"\s*:\s*"(?!예시|example|unknown|없음|\.\.\.)[^"\r\n]{2,}"' 2>/dev/null \
    | sed 's/^/[미디어 메타데이터] /'
  # ⑦ 개인 행동을 실측/기준선이라고 밝히면서 수량까지 적은 줄.
  xargs -a "$list" grep -niHP '((실측|실기기|운영 DB|기준선|하루치).{0,100}(유튜브|youtube|게임|수면|잠금해제|unlock|폰|노트북).{0,60}[0-9]+([.:][0-9]+)?(분|시간|초|건|%))|((유튜브|youtube|게임|수면|잠금해제|unlock|폰|노트북).{0,100}(실측|실기기|운영 DB|기준선|하루치).{0,60}[0-9]+([.:][0-9]+)?(분|시간|초|건|%))' 2>/dev/null \
    | grep -viE '합성|예시|공개본|제거|첫 롤업' | sed 's/^/[개인 행동 집계] /'
  # ⑧ 미디어·잠금해제와 실제 시각이 한 줄에 붙은 기록.
  xargs -a "$list" grep -niHP '((유튜브|youtube|게임|수면|잠금해제|unlock).{0,80}[0-2]?[0-9]:[0-5][0-9])|([0-2]?[0-9]:[0-5][0-9].{0,80}(유튜브|youtube|게임|수면|잠금해제|unlock))' 2>/dev/null \
    | grep -viE '합성|예시|T[0-9]' | sed 's/^/[개인 행동 시각] /'
  # ⑨ 개인 홈 경로. 공개본은 /home/user 또는 일반적인 CI 계정만 쓴다.
  xargs -a "$list" grep -nHP '(?<![A-Za-z0-9_$])/home/(?!user(?:/|$|[^A-Za-z0-9._-])|runner(?:/|$|[^A-Za-z0-9._-])|ubuntu(?:/|$|[^A-Za-z0-9._-])|nvidia(?:/|$|[^A-Za-z0-9._-])|<user>)[A-Za-z0-9._-]+' 2>/dev/null \
    | sed 's/^/[개인 홈 경로] /'
  # ⑩ 실제 환경에서 쓰던 주소 모양. 낮은 번호는 문서·테스트 예시로 허용한다.
  xargs -a "$list" grep -nHP '\b100\.64\.0\.(?![0-5]\b)[0-9]{1,3}\b|\b192\.168\.0\.(?![01]\b)[0-9]{1,3}\b' 2>/dev/null \
    | sed 's/^/[개인 네트워크 주소] /'
  # ⑪ MAC 주소와 MAC 기반 인터페이스명. 예시는 wlan0 / wlxEXAMPLEMAC.
  xargs -a "$list" grep -nHPi '\b([0-9a-f]{2}:){5}[0-9a-f]{2}\b|\bwlx(?!EXAMPLEMAC)[0-9a-f]{12}\b' 2>/dev/null \
    | sed 's/^/[MAC 주소] /'
  # ⑫ 공인 IPv4. 사설·루프백·CGNAT·링크로컬·문서용 대역은 통과시킨다.
  #   테스트가 공인 주소 의미로 쓰는 두 값과 브라우저 버전도 명시적인 예시다.
  xargs -a "$list" grep -nHP '(?<![\d.])(?<!Chrome/)(?<!Firefox/)(?<!Version/)(?<!Edg/)(?!8\.8\.8\.8\b|93\.184\.216\.(?:34|35)\b|10\.|127\.|192\.168\.|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.|172\.(?:1[6-9]|2\d|3[01])\.|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.|169\.254\.|0\.|22[4-9]\.|23\d\.|24\d\.|25[0-5]\.)(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\d.])' 2>/dev/null \
    | sed 's/^/[공인 IP] /'
  # ⑬ 이 기기에만 있는 금지어 목록 (저장소에 안 들어간다)
  if [ -s "$TERMS_FILE" ]; then
    grep -vE '^\s*(#|$)' "$TERMS_FILE" | while IFS= read -r term; do
      xargs -a "$list" grep -nFH -- "$term" 2>/dev/null | sed 's/^/[로컬 금지어] /'
    done
  fi
}

if [ -n "$SELF_TEST" ]; then
  echo "▶ 개인정보 검사 — 자기시험"
  d=operate/tools/fixtures/check-privacy
  tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT

  echo "$d/must-fail.md" > "$tmp"
  n=$(scan "$tmp" | wc -l)
  if [ "$n" -ge 11 ]; then echo "  ✅ must-fail.md 를 잡는다 ($n건 · 개인정보 모양 11종)"
  else echo "  ❌ must-fail.md 에서 $n건만 잡았다 — 규칙이 헐거워졌다"; exit 1; fi

  echo "$d/must-pass.md" > "$tmp"
  n=$(scan "$tmp" | wc -l)
  if [ "$n" -eq 0 ]; then echo "  ✅ must-pass.md 를 통과시킨다 (예시값은 안 잡는다)"
  else echo "  ❌ must-pass.md 에서 $n건 오탐:"; scan "$tmp" | sed 's/^/      /'; exit 1; fi
  exit 0
fi

list=$(mktemp)
if [ -n "$HISTORY_MODE" ]; then
  echo "▶ 전체 Git 이력 개인정보 검사"
  history_dir=$(mktemp -d)
  trap 'rm -f "$list"; rm -rf "$history_dir"' EXIT
  declare -A seen=()
  while read -r oid path; do
    [ -n "${path:-}" ] || continue
    case "$path" in
      life-trainer/docs/design/refs/*|life-trainer/reference/*|refs/reference/*|operate/tools/fixtures/check-privacy/*) continue ;;
      *.md|*.py|*.js|*.mjs|*.json|*.yaml|*.yml|*.toml|*.txt|*.html|*.sh|*.ps1|*.service|*.conf) ;;
      *) continue ;;
    esac
    [ -z "${seen[$oid]+x}" ] || continue
    [ "$(git cat-file -t "$oid" 2>/dev/null)" = blob ] || continue
    seen[$oid]=1
    out="$history_dir/$oid.${path##*.}"
    git cat-file blob "$oid" > "$out"
    printf '%s\n' "$out" >> "$list"
  done < <(git rev-list --objects --all)
else
  echo "▶ 현재 추적 파일 개인정보 검사"
  trap 'rm -f "$list"' EXIT
  # 남의 저장소를 복사해 둔 참고 자료는 뺀다 — 우리 기록이 아니다.
  git ls-files '*.md' '*.py' '*.js' '*.mjs' '*.json' '*.yaml' '*.yml' '*.toml' '*.txt' '*.html' '*.sh' '*.ps1' '*.service' '*.conf' \
    | grep -vE '^(life-trainer/docs/design/refs/|life-trainer/reference/|refs/reference/)' \
    | grep -v '^operate/tools/fixtures/check-privacy/' > "$list"
fi

hits=$(scan "$list")
count=$(printf '%s' "$hits" | grep -c . || true)
if [ "$count" -gt 0 ]; then
  echo "$hits" | sed 's/^/  ❌ /'
  echo
  echo "  파일 $(wc -l < "$list") 개 검사 · 개인 기록으로 보이는 것 $count 건"
  echo "  → 실제 값 대신 예시값을 쓰세요. 이 저장소가 쓰는 예시:"
  echo "     [SSH: host] · op.gg/summoners/kr/example · m.example-forum.com · watch?v=%08d"
  echo "     창 제목·앱 이름은 lifetrainer/collect/synthetic.py 의 어휘를 가져다 씁니다."
  exit 1
fi
echo "  파일 $(wc -l < "$list") 개 검사 · 개인 기록 0 건"
