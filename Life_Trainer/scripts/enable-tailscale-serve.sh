#!/usr/bin/env bash
# Life Trainer 플래너 웹 서버(`lt web`)를 Tailscale Serve 로 노출한다.
#
# openclaw-setup/enable-tailscale-serve.sh 와 같은 방식이다(이 기기에서 이미
# 검증됨): tailscaled 에 직접 `tailscale serve` 를 걸어 테일넷 전용 HTTPS +
# 포트 없는 호스트명으로 접근하게 한다. 인증은 Tailscale 이 담당하므로 앱에는
# 로그인을 만들지 않는다(계약서 §4 P2 제약 참고).
#
#   서버는 127.0.0.1:8770 (lt web 기본 포트) 에서만 듣는다
#   serve 적용 후      https://device.tailnet-name.ts.net:8443   ← 테일넷 전용, 인증서 자동
#
# openclaw 게이트웨이가 별도로 https:443 을 쓸 수 있으므로(openclaw-setup 쪽
# 스크립트 참고) 충돌을 피하려고 이 스크립트는 기본적으로 다른 HTTPS 포트
# (8443)를 쓴다. 필요하면 LT_WEB_SERVE_HTTPS_PORT 로 바꿀 수 있다.
#
# 선행 조건 (한 번만, root):
#   sudo tailscale set --operator=$USER
#
# ⚠️ 이 기기는 sudo 에 비밀번호가 필요하다 — 이 스크립트는 sudo 를 절대
#   직접 실행하지 않는다. 권한이 없으면 안내 메시지만 출력하고 종료한다.
#
# 주의: serve 는 테일넷 전용이다. 공개 인터넷 노출은 funnel 이며 여기서 켜지 않는다.
set -euo pipefail

LOCAL_PORT="${LT_WEB_PORT:-8770}"
SERVE_HTTPS_PORT="${LT_WEB_SERVE_HTTPS_PORT:-8443}"

if ! tailscale serve status >/dev/null 2>&1; then
  echo "tailscale 이 실행 중이 아니다." >&2
  exit 1
fi

if ! tailscale serve --bg --https="$SERVE_HTTPS_PORT" "$LOCAL_PORT" 2>/dev/null; then
  cat >&2 <<EOF
tailscale serve 설정 권한이 없다. root 로 한 번만 실행하고 다시 시도할 것:

  sudo tailscale set --operator=\$USER

(이 스크립트는 sudo 를 대신 실행해 주지 않는다 — 이 기기는 sudo 에 비밀번호가
필요하기 때문이다. 위 명령을 직접 실행한 뒤 이 스크립트를 다시 돌려라.)
EOF
  exit 1
fi

echo "==> serve 상태"
tailscale serve status

DNS_NAME="$(tailscale status --json \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"

BASE_URL="https://${DNS_NAME}:${SERVE_HTTPS_PORT}"

echo
echo "완료 → ${BASE_URL}"
echo
echo "config/lifetrainer.toml 의 [web] 섹션에 다음을 넣어라 (Slack '플래너 열기' 링크에 쓰인다):"
echo
echo "  [web]"
echo "  base_url = \"${BASE_URL}\""
echo
echo "되돌리려면 (이 포트만 끄기):"
echo "  tailscale serve --https=${SERVE_HTTPS_PORT} off"
echo
echo "이 노드의 serve 설정을 전부 지우려면 (다른 서비스가 serve 를 쓰고 있다면 함께 지워지니 먼저 "
echo "'tailscale serve status' 로 확인할 것):"
echo "  tailscale serve reset"
