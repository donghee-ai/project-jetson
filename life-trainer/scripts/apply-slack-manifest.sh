#!/usr/bin/env bash
# Slack 앱 매니페스트를 **실제 앱에 적용**한다.
#
# ★ 왜 이 스크립트가 필요한가 (2026-08-19)
#
#   `config/slack-app-manifest-merged.json` 을 고쳐도 Slack 은 아무것도 안 바뀐다.
#   슬래시 명령의 사용법 힌트(입력창에 뜨는 회색 글씨)는 **Slack 쪽 앱 설정**에
#   저장돼 있고, 저장소의 JSON 은 그 사본일 뿐이다. 실제로 예시를 대학생 기준으로
#   바꿨는데 Slack 입력창에는 며칠 전 예시가 그대로 떠 있었다.
#
#   같은 부류를 하루에 두 번 밟았다 — systemd 서비스도 파일만 고치고 재기동을 안 해
#   옛 코드가 돌고 있었다. **파일 ≠ 실물.**
#
# ── 준비: 설정 토큰(xoxe-) ────────────────────────────────────────────
#
#   봇 토큰(xoxb-)으로는 앱 설정을 못 바꾼다. 별도의 **App Configuration Token**
#   이 필요하다:
#
#     https://api.slack.com/apps  →  맨 아래 "Your App Configuration Tokens"
#     →  Generate Token  →  xoxe-... 를 복사 (유효기간 12시간)
#
#   토큰을 저장소나 설정 파일에 넣지 않는다. 이 명령 한 번에만 쓴다:
#
#     SLACK_CONFIG_TOKEN=xoxe-... bash scripts/apply-slack-manifest.sh
#
# ── 되돌리기 ──────────────────────────────────────────────────────────
#
#   적용 전 매니페스트를 data/backup/ 에 내려받아 둔다. 문제가 생기면 그 파일로
#   같은 명령을 다시 돌리면 된다:
#
#     MANIFEST=data/backup/slack-manifest-<날짜>.json bash scripts/apply-slack-manifest.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="${MANIFEST:-$REPO_DIR/config/slack-app-manifest-merged.json}"
APP_ID="${SLACK_APP_ID:-A0123456789}"   # Example_Workspace 워크스페이스의 OpenClaw 앱
API="https://slack.com/api"

if [[ -z "${SLACK_CONFIG_TOKEN:-}" ]]; then
    echo "SLACK_CONFIG_TOKEN 이 없다. https://api.slack.com/apps 하단에서" >&2
    echo "'Your App Configuration Tokens' → Generate Token (xoxe-…, 12시간 유효)" >&2
    echo >&2
    echo "  SLACK_CONFIG_TOKEN=xoxe-... bash scripts/apply-slack-manifest.sh" >&2
    exit 2
fi
if [[ ! -f "$MANIFEST" ]]; then
    echo "매니페스트 파일이 없다: $MANIFEST" >&2
    exit 2
fi
if ! python3 -c "import json,sys; json.load(open(sys.argv[1], encoding='utf-8'))" "$MANIFEST"; then
    echo "매니페스트가 올바른 JSON 이 아니다: $MANIFEST" >&2
    exit 2
fi

echo "== Slack 앱 매니페스트 적용 =="
echo "앱: $APP_ID"
echo "매니페스트: $MANIFEST"
echo

# 1) 현재 매니페스트를 먼저 받아 백업한다 (되돌릴 길을 만들어 두고 바꾼다)
BACKUP_DIR="$REPO_DIR/data/backup"
mkdir -p "$BACKUP_DIR"
BACKUP="$BACKUP_DIR/slack-manifest-$(date +%Y-%m-%d-%H%M).json"
if curl -sS -X POST "$API/apps.manifest.export" \
        -H "Authorization: Bearer $SLACK_CONFIG_TOKEN" \
        -H "Content-Type: application/x-www-form-urlencoded" \
        --data-urlencode "app_id=$APP_ID" \
    | python3 -c "
import json,sys
d = json.load(sys.stdin)
if not d.get('ok'):
    sys.exit('현재 매니페스트를 못 받았다: ' + str(d.get('error')))
json.dump(d['manifest'], open(sys.argv[1], 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
" "$BACKUP"; then
    echo "적용 전 매니페스트 백업: $BACKUP"
else
    echo "백업에 실패했다. 되돌릴 수 없는 상태로는 적용하지 않는다." >&2
    exit 1
fi

# 2) 적용
echo
RESULT=$(curl -sS -X POST "$API/apps.manifest.update" \
    -H "Authorization: Bearer $SLACK_CONFIG_TOKEN" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    --data-urlencode "app_id=$APP_ID" \
    --data-urlencode "manifest@$MANIFEST")

echo "$RESULT" | python3 -c "
import json, sys
d = json.load(sys.stdin)
if d.get('ok'):
    print('적용 완료.')
    if d.get('permissions_updated'):
        print('★ 권한이 바뀌었다 — 워크스페이스에서 앱을 **재설치**해야 적용된다.')
    else:
        print('권한 변화 없음 — 재설치 불필요. 슬래시 힌트는 바로 반영된다.')
    print()
    print('확인: Slack 입력창에 \"/\" 를 치면 새 사용법이 보인다 (클라이언트 캐시 때문에')
    print('      안 보이면 Ctrl+R 로 새로고침).')
else:
    print('실패:', d.get('error'), file=sys.stderr)
    for e in d.get('errors', []) or []:
        print('  -', e, file=sys.stderr)
    sys.exit(1)
"
