#!/usr/bin/env bash
# Life Trainer 를 OpenClaw 의 에이전트 하나로 등록한다.
#
#   ① 워크스페이스 프롬프트 설치 (AGENTS.md 생성 · 범용 인격 파일 비우기)
#   ② `lifetrainer` 에이전트 등록 (Qwen3-8B, 전용 워크스페이스)
#   ③ MCP 서버 `lt` 등록 (stdio, 우리 venv 의 파이썬)
#   ④ 툴 정책 — 이 에이전트는 `lt__*` 만, main 은 `lt__*` 금지
#
# **여러 번 돌려도 안전하다.** 이미 있으면 갱신한다.
#
# 왜 스크립트인가: 이 배선은 `~/.openclaw/openclaw.json` 안에만 존재한다.
# 그 파일은 저장소 밖이고 gitignore 대상도 아닌 남의 파일이다 —
# 손으로 친 명령으로만 남기면 기기를 다시 세울 때 재현이 안 된다.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$REPO_DIR/.venv/bin/python"
LT="$REPO_DIR/.venv/bin/lt"
AGENT_ID="lifetrainer"
MCP_NAME="lt"

# 시스템 Node 를 앞에 둔다 — nvm 셸에서 돌려도 게이트웨이와 같은 런타임을 쓴다
# (`docs/build/openclaw-agent.md §4-10`).
export PATH="/usr/local/bin:$PATH"

command -v openclaw >/dev/null || { echo "openclaw CLI 가 없습니다."; exit 1; }
[ -x "$VENV_PY" ] || { echo "$VENV_PY 가 없습니다. venv 를 먼저 만드세요."; exit 1; }

WORKSPACE="$("$VENV_PY" - <<'PY'
from lifetrainer.agent.config import load_settings
from lifetrainer.config import load_config
from pathlib import Path
cfg = load_config(); s = load_settings(cfg)
p = Path(s.workspace)
print(p if p.is_absolute() else cfg.root / p)
PY
)"

echo "== ① 워크스페이스 프롬프트"
mkdir -p "$WORKSPACE"
"$LT" agent prompt

echo "== ② 에이전트 등록 ($AGENT_ID)"
if openclaw agents list 2>/dev/null | grep -q "^- $AGENT_ID"; then
  echo "   이미 있음 — 워크스페이스·모델만 맞춘다"
else
  openclaw agents add "$AGENT_ID" --non-interactive --json \
    --workspace "$WORKSPACE" --model llamacpp/qwen3-8b >/dev/null
fi

# `agents add` 가 워크스페이스에 범용 인격 파일을 다시 깔았을 수 있다.
# **등록 뒤에 한 번 더 돌린다** — 순서가 중요하다 (prompt.py 의 SEEDED_FILES 주석).
"$LT" agent prompt

echo "== ③ MCP 서버 등록 ($MCP_NAME)"
openclaw mcp add "$MCP_NAME" \
  --command "$VENV_PY" --arg -m --arg lifetrainer.agent.mcp_server \
  --cwd "$REPO_DIR" --timeout 120 --no-probe >/dev/null 2>&1 || \
  openclaw mcp add "$MCP_NAME" \
    --command "$VENV_PY" --arg -m --arg lifetrainer.agent.mcp_server \
    --cwd "$REPO_DIR" --timeout 120 >/dev/null

echo "== ④ 툴 정책"
# `agents.list` 는 배열이라 인덱스로 지정해야 한다. id 로 찾아 인덱스를 구한다.
IDX="$(openclaw config get agents.list | "$VENV_PY" -c '
import json, sys
for i, a in enumerate(json.load(sys.stdin)):
    if a.get("id") == "'"$AGENT_ID"'":
        print(i); break
else:
    raise SystemExit("에이전트를 찾지 못했습니다")
')"
MAIN_IDX="$(openclaw config get agents.list | "$VENV_PY" -c '
import json, sys
for i, a in enumerate(json.load(sys.stdin)):
    if a.get("id") == "main":
        print(i); break
else:
    print(-1)
')"

# 절대 허용목록 — 프로필이 주는 기본 툴(exec·write·cron…)을 **대체한다.**
# 파일 접근은 우리 감옥을 지나는 `lt__read_file`·`lt__write_file` 만 남는다.
openclaw config set "agents.list.$IDX.tools.allow" '["lt__*"]' --strict-json >/dev/null
openclaw config set "agents.list.$IDX.description" \
  'Life Trainer — 활동 기록·계획·문서 검색 (슬래시 우선)' >/dev/null
openclaw config set "agents.list.$IDX.skills" '[]' --strict-json >/dev/null
# ★ localModelLean 은 **켜지 않는다.** 툴 13개를 메타툴 3개(tool_call/tool_search/
#   tool_describe)로 바꾸는 기능이라, 8B 가 그 간접층을 못 넘고 `tool_call` 을
#   툴 이름으로 착각해 호출이 실패했다 (실측). 우리 툴은 이미 작다.
openclaw config set "agents.list.$IDX.experimental.localModelLean" false >/dev/null

# main 에이전트가 우리 툴 스키마 값을 내지 않게 한다 (턴마다 약 2,900 토큰).
if [ "$MAIN_IDX" != "-1" ]; then
  openclaw config set "agents.list.$MAIN_IDX.tools.deny" '["lt__*"]' --strict-json >/dev/null
fi

systemctl --user restart openclaw-gateway 2>/dev/null || true

echo
echo "== 확인"
openclaw mcp probe 2>&1 | sed 's/^/   /'
"$LT" agent budget | tail -3 | sed 's/^/   /'
echo
echo "써 보기:  openclaw agent --agent $AGENT_ID --message '오늘 계획이 뭐야?'"
echo "채점하기: $VENV_PY scripts/eval_agent.py"
