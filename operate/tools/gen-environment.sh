#!/usr/bin/env bash
# environment.md 의 측정 블록을 **생성**한다. 사람이 옮겨 적지 않는다.
#
# ★ 왜 이 스크립트가 생겼나 (2026-08-28)
#   Makefile 의 `verify` 는 "→ environment.md 갱신" 이라고 적어 놓고 실제로는
#   verify-jetpack.sh 를 **화면에 찍기만** 했다. 즉 environment.md 는 "자동 생성" 이라
#   적힌 채로 사람이 붙여넣고 있었다 — 이 저장소가 네 번 겪은 전사(轉寫) 사고와 같은 모양이다.
#   그 사이 verify-jetpack.sh 의 버그로 L4T 가 빈칸, CUDA 4종이 ❌, DLA 가 없음으로
#   실려 있었고 아무도 몰랐다.
#
#   마커 사이만 바꾼다 — "읽는 법" 같은 편집 문단은 사람의 것이라 건드리지 않는다.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

export TARGET=environment.md
export MARK_BEGIN='<!-- verify:begin — 이 블록은 `make verify` 가 생성한다. 손으로 고치지 말 것 -->'
export MARK_END='<!-- verify:end -->'
STAMP=$(date '+%Y-%m-%d %H:%M %Z')
export STAMP

grep -qF "$MARK_BEGIN" "$TARGET" || { echo "❌ $TARGET 에 마커가 없다" >&2; exit 1; }

# ANSI 색은 뗀다 — 파일에 들어가면 diff 가 읽히지 않는다.
BODY=$(bash measure/tools/verify-jetpack.sh 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g')
export BODY

python3 <<'PY'
import os, pathlib
t, b, e, stamp, body = (os.environ[k] for k in
                        ("TARGET", "MARK_BEGIN", "MARK_END", "STAMP", "BODY"))
p = pathlib.Path(t); s = p.read_text()
i, j = s.index(b), s.index(e)
p.write_text(s[:i] + f"{b}\n\n> 생성 시각: {stamp}\n\n```\n{body.rstrip()}\n```\n\n" + s[j:])
print(f"  {t} 갱신 — {stamp}")
PY
