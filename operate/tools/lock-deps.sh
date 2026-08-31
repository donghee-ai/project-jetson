#!/usr/bin/env bash
# 의존성의 **버전 고정본**을 이 기기에서 다시 뽑는다.
#
#   life-trainer/constraints.txt   pip  — 앱의 직접·전이 의존성
#   operate/npm-globals.txt        npm  — 전역 CLI (openclaw · playwright)
#
# ★ apt 는 여기서 안 다룬다. `measure/tools/plot.py` 의 matplotlib 은 apt 가 깐 것이라
#   pip 으로 고정하면 dist-packages 를 가린다. 경위는 measure/README.md 의 표.
#
# ★ 정본은 **이 기기** 다. 여기가 실제로 돌아가는 것이 확인된 조합이라서다.
#   손으로 고치지 않는다.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

# ── pip ─────────────────────────────────────────────────────────────
#   무엇을 설치할지는 pyproject.toml 이 정한다. 이 파일은 **어느 판을 쓸지**만 정한다 —
#   pip 의 `-c`(constraints)가 정확히 그렇게 동작한다. 둘을 한 파일에 합치면
#   "왜 이게 깔렸나" 와 "왜 이 판인가" 가 섞여서 다음 사람이 못 읽는다.
#
#   왜 필요했나: pyproject 의 의존성이 전부 `>=` 라 CI(매번 새로 설치)와
#   이 기기(몇 주 전 설치)가 서로 다른 판을 쓸 수 있었다. 한쪽만 깨졌을 때
#   **버전이 용의선상에서 빠진다** — 이 저장소가 이미 겪은 부류다
#   (life-trainer/HISTORY/2026-08-27-the-suite-was-green-on-this-machine-only.md).
PIP_OUT=life-trainer/constraints.txt
VENV=life-trainer/.venv/bin
[ -x "$VENV/pip" ] || { echo "❌ $VENV/pip 이 없다 — venv 부터 만들 것" >&2; exit 1; }

tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
{
  echo "# life-trainer 의존성 **버전 고정** — pip constraints 파일."
  echo "#"
  echo "# 무엇을 설치할지는 pyproject.toml 이 정한다. 여기는 **어느 판을 쓸지**만 정한다."
  echo "# 여기 없는 것은 안 깔리는 게 아니라, 판을 안 고정하는 것이다."
  echo "#"
  echo "# 손으로 고치지 않는다 — 이 기기를 정본으로 다시 뽑는다:  make lock"
  echo "# 왜 있는지는 operate/tools/lock-deps.sh 머리말에 있다."
  echo "#"
  echo "# 뽑은 시각: $(date '+%Y-%m-%d %H:%M %Z') · python $("$VENV/python" -V 2>&1 | cut -d' ' -f2)"
  echo
  "$VENV/pip" freeze --exclude-editable
} > "$tmp"
n=$(grep -cvE '^#|^$' "$tmp")
[ "$n" -gt 0 ] || { echo "❌ pip: 고정할 것이 없다 — 덮어쓰지 않았다" >&2; exit 1; }
mv "$tmp" "$PIP_OUT"
echo "  $PIP_OUT — $n 개 고정"

# ── npm ─────────────────────────────────────────────────────────────
#   ★ `npm ls -g` 를 쓰지 않는다. 이 기기에는 node 가 둘이고, 사용자 셸에서는
#     nvm 이 NPM_CONFIG_PREFIX 를 깔아 `npm -g` 가 **nvm 쪽**을 가리킨다.
#     그런데 게이트웨이가 실제로 실행하는 openclaw 는 **시스템 쪽**에 있다.
#     한 줄을 믿으면 엉뚱한 트리를 기록한다 — 실제로 그렇게 한 번 잘못 셌다.
#     그래서 **경로를 직접 읽는다** (CLAUDE.md §2: 경로가 아니라 실물을 본다).
NPM_OUT=operate/npm-globals.txt
tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
{
  echo "# 전역 npm 패키지 — **prefix 별로** 적는다. 이 기기에는 node 가 둘이다."
  echo "#"
  echo "# 왜 나눠 적나: 사용자 셸의 \`npm -g\` 는 nvm 을 가리키는데(NVM 이 prefix 를 깐다)"
  echo "# 게이트웨이가 실행하는 openclaw 는 시스템 쪽에 있다. 한쪽만 보면 틀린다."
  echo "# node 가 번들로 갖고 오는 npm·corepack 은 고른 것이 아니라 안 적는다."
  echo "#"
  echo "# 새 기기에 세울 때:  sudo npm install -g <아래 그대로>"
  echo "# 다시 뽑기:         make lock"
  echo "#"
  echo "# 뽑은 시각: $(date '+%Y-%m-%d %H:%M %Z')"
  for prefix in /usr/local "$HOME/.nvm/versions/node"/*; do
    root="$prefix/lib/node_modules"
    [ -d "$root" ] || continue
    nodever=$("$prefix/bin/node" --version 2>/dev/null || echo "?")
    echo
    echo "[$prefix]  node $nodever"
    # 스코프 패키지(`@scope/name`)는 한 칸 더 깊다. 글롭 두 개로 나눠 잡는다 —
    # `find -maxdepth 2` 로는 `@playwright/mcp` 를 못 본다 (실제로 놓쳤다).
    for d in "$root"/*/ "$root"/@*/*/; do
      [ -f "${d}package.json" ] || continue
      case "${d#"$root"/}" in npm/|corepack/) continue;; esac
      "$VENV/python" - "${d}package.json" <<'PYJSON'
import json,sys
try:
    m=json.load(open(sys.argv[1]))
    if m.get("name") and m.get("version"): print(f'{m["name"]}@{m["version"]}')
except Exception: pass
PYJSON
    done | sort -u
  done
} > "$tmp"
n=$(grep -cE '@' "$tmp" || true)
[ "$n" -gt 0 ] || { echo "❌ npm: 전역 패키지를 하나도 못 찾았다 — 덮어쓰지 않았다" >&2; exit 1; }
mv "$tmp" "$NPM_OUT"; trap - EXIT
echo "  $NPM_OUT — $n 개 기록"

git diff --stat -- "$PIP_OUT" "$NPM_OUT" | tail -3
