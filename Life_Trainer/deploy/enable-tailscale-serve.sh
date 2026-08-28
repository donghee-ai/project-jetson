#!/usr/bin/env bash
# Tailscale Serve 를 켠다 — 테일넷 안에서 HTTPS + 포트 없는 호스트명으로 접근.
#
#   bind=lan 만으로도  http://100.64.0.2:18081     ← 이미 동작
#   serve 적용 후      https://device.tailnet-name.ts.net  ← 포트 없음, 인증서 자동
#
# 선행 조건 (한 번만, root):
#   sudo tailscale set --operator=$USER
#
#
# ⚠️ gateway.tailscale.mode 는 건드리지 않는다.
#
#   openclaw 는 mode=serve 를 켜면 bind 가 loopback 이길 강제한다:
#
#     Config validation failed: gateway.bind must resolve to loopback when
#     gateway.tailscale.mode=serve
#
#   serve 를 openclaw 가 관리한다는 건 "테일넷으로만 접근한다"는 전제라서다.
#   우리는 LAN 도 열어둔 상태(bind=lan)이므로 그 전제가 안 맞는다.
#
#   그런데 serve 자체는 tailscaled 가 하는 일이고, 127.0.0.1:18081 로 프록시한다.
#   bind=lan(0.0.0.0)에는 loopback 이 포함되므로 serve 는 그대로 동작한다.
#   즉 openclaw 에게 알리지 않고 tailscaled 에 직접 걸면 LAN + HTTPS 가 공존한다.
#
#   대가: openclaw 가 serve 수명주기를 관리하지 않는다(resetOnExit 불가).
#   게이트웨이가 내려가 있으면 테일넷 쪽에서 502 가 뜬다. 라우트는 tailscaled 에
#   영구 저장되므로 재부팅해도 남는다. 지우려면 `tailscale serve reset`.
#
# 주의: serve 는 테일넷 전용이다. 공개 인터넷 노출은 funnel 이며 여기서 켜지 않는다.
set -euo pipefail

PORT="${GATEWAY_PORT:-18081}"

if ! tailscale serve status >/dev/null 2>&1; then
  echo "tailscale 이 실행 중이 아니다." >&2
  exit 1
fi

if ! tailscale serve --bg --https=443 "$PORT" 2>/dev/null; then
  cat >&2 <<'EOF'
tailscale serve 설정 권한이 없다. root 로 한 번만 실행하고 다시 시도할 것:

  sudo tailscale set --operator=$USER
EOF
  exit 1
fi

echo "==> serve 상태"
tailscale serve status

# HTTPS 오리진을 Control UI 허용 목록에 넣는다.
# 없으면 브라우저에서 origin 이 걸려 웹 UI 가 거부된다 (함정 10).
DNS_NAME="$(tailscale status --json \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"

python3 - "$DNS_NAME" <<'PY'
import json, sys, pathlib
dns = sys.argv[1]
p = pathlib.Path.home() / ".openclaw" / "openclaw.json"
cfg = json.loads(p.read_text())
ui = cfg.setdefault("gateway", {}).setdefault("controlUi", {})
origins = ui.setdefault("allowedOrigins", [])
added = [o for o in (f"https://{dns}",) if o not in origins]
origins.extend(added)
p.write_text(json.dumps(cfg, indent=2) + "\n")
print("추가된 오리진:", added or "(없음)")
PY

openclaw config validate
systemctl --user restart openclaw-gateway

echo
echo "완료 → https://${DNS_NAME}"
echo "토큰 URL:"
python3 -c "
import json, pathlib
t = json.loads((pathlib.Path.home()/'.openclaw'/'openclaw.json').read_text())['gateway']['auth']['token']
print('  https://${DNS_NAME}/#token=' + t)
"
