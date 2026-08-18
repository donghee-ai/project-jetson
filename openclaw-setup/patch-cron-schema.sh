#!/usr/bin/env bash
# cron 툴 스키마에서 앵커 없는 정규식을 제거한다 — llama.cpp 백엔드 호환 패치.
#
# 문제:
#   openclaw 의 cron 툴은 job.declarationKey 에 pattern: "\S" 를 갖는다.
#   llama.cpp 는 --jinja 로 툴 콜링을 할 때 툴 스키마를 GBNF 문법으로 변환하는데,
#   앵커(^ … $)가 없는 패턴을 거부한다. 하나라도 어긋나면 요청 '전체'가 400 이다.
#
#     400 Unable to generate parser for this template.
#         JSON schema conversion failed: Pattern must start with '^' and end with '$'
#
#   그래서 cron 이 실리는 경로(웹 UI = operator.admin 발신자)에서만 채팅이 깨진다.
#   CLI 는 cron 이 owner-only 로 자동 제외되어 멀쩡하다 — 증상이 반쪽만 나타난다.
#
# 왜 앵커를 붙이지 않고 지우는가:
#   실측 결과 앵커를 붙여도 안 된다. 앵커 검사는 통과하지만 그 다음 단계에서 깨진다.
#
#     pattern="\S"        → 400 Pattern must start with '^' and end with '$'
#     pattern="^\S+$"     → 400 Failed to initialize samplers: failed to parse grammar
#     pattern="^.*\S.*$"  → 400 Failed to initialize samplers: failed to parse grammar
#     pattern 제거         → 200 OK
#
#   llama.cpp 의 GBNF 변환기가 \S 자체를 못 다룬다. 제거가 유일한 해법이다.
#
# 안전한가:
#   그렇다. pattern 은 중복 검증이다. 공백 문자열 거부는 런타임 코드가 이미 한다.
#     dist/cron-BXksovqf.js    "declarationKey must not be blank"
#     dist/cron-tool-*.js      "declarationKey must be a non-empty string"
#   minLength/maxLength 도 그대로 남는다.
#
# 언제 다시 실행하나:
#   npm i -g openclaw 로 업그레이드할 때마다. dist 가 통째로 교체된다.
#   파일명 해시(cron-tool-XXXX.js)는 버전마다 바뀌므로 이 스크립트는 내용으로 찾는다.
set -euo pipefail

# `openclaw` 실행 파일은 패키지 디렉토리의 openclaw.mjs 로 심링크돼 있다.
# readlink -f 로 실체를 찾은 뒤 그 디렉토리가 곧 패키지 루트다.
PKG_DIR="${OPENCLAW_DIR:-$(dirname "$(readlink -f "$(command -v openclaw)")")}"
PKG_DIR="$(cd "$PKG_DIR" && pwd)"
DIST="$PKG_DIR/dist"

if [ ! -d "$DIST" ]; then
  echo "openclaw dist 를 찾을 수 없다: $DIST" >&2
  exit 1
fi

echo "대상: $DIST"

python3 - "$DIST" <<'PY'
import pathlib, re, sys

dist = pathlib.Path(sys.argv[1])
# `maxLength: 200,\n\t\t\tpattern: "\\S"` 형태에서 pattern 줄과 앞 콤마를 함께 지운다.
needle = re.compile(r',\s*\n\s*pattern:\s*"\\\\S"')

patched = 0
already = 0
for f in sorted(dist.glob("*.js")):
    try:
        src = f.read_text()
    except UnicodeDecodeError:
        continue
    if 'pattern: "\\\\S"' not in src:
        continue
    new, n = needle.subn("", src)
    if n:
        f.write_text(new)
        print(f"  패치: {f.name} ({n}곳)")
        patched += n
    else:
        print(f"  ⚠ 패턴은 있으나 형태가 달라 건너뜀: {f.name}", file=sys.stderr)

if patched == 0:
    # 이미 패치됐는지 확인
    hits = [f.name for f in dist.glob("*.js")
            if 'pattern: "\\\\S"' in f.read_text(errors="ignore")]
    if hits:
        print("  ⚠ 남아 있는 위치:", hits, file=sys.stderr)
        sys.exit(1)
    print("  이미 패치됨 (변경 없음)")
PY

echo
echo "확인: 남은 \\S 패턴"
if grep -rn 'pattern: "\\\\S"' "$DIST"/*.js 2>/dev/null; then
  echo "  ⚠ 아직 남아 있다" >&2
  exit 1
else
  echo "  없음"
fi

cat <<'EOF'

다음 단계 — cron 차단을 풀고 게이트웨이 재시작:

  openclaw config unset tools.deny
  systemctl --user restart openclaw-gateway

주의: cron 툴은 3,912 토큰이다(툴 전체의 44%). 되살리면 매 턴 약 13초가 더 든다.
EOF
