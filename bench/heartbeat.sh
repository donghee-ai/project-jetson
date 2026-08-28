#!/usr/bin/env bash
# 바깥으로 "나 살아 있다" 를 보낸다. **없으면 바깥이 알아챈다.**
#
# ★ 왜 이게 따로 필요한가
#   같은 기기가 보내는 Slack 알림은 **그 기기의 죽음을 못 알린다.**
#   전원 단절 · 네트워크 단절 · 부팅 실패 · user manager 미기동 · Slack 경로 자체 장애 —
#   전부 "알림이 안 온다" 로만 나타나고, 안 오는 것은 눈에 안 띈다.
#   그래서 판정을 **바깥에 둔다**: 신호가 끊기면 바깥이 알린다 (dead-man switch).
#
#   URL 은 healthchecks.io 같은 무료 서비스의 ping 주소면 된다. 개인 정보는 안 나간다 —
#   나가는 것은 "이 기기가 방금 정상이었다" 는 사실 하나다.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

URL="${JETSON_HEARTBEAT_URL:-}"
if [ -z "$URL" ]; then
  echo "JETSON_HEARTBEAT_URL 이 비어 있다 — 바깥에서 이 기기의 죽음을 알아챌 방법이 없다." >&2
  echo "  systemd/jetson-heartbeat.service 의 Environment 에 ping 주소를 넣는다." >&2
  exit 78   # EX_CONFIG — 실패가 아니라 미설정
fi

# ★ 살아 있음을 **증명한 뒤에만** 보낸다. 프로세스가 도는 것과 서빙되는 것은 다르다
#   (모델 id 를 안 보는 헬스체크가 1시간 무중단 다운을 낸 적 있다 — CLAUDE.md).
fail=""
curl -sf --max-time 5 http://127.0.0.1:8080/health >/dev/null 2>&1 || fail="$fail llama-server"
for u in lifetrainer-worker lifetrainer-web; do
  [ "$(systemctl --user is-active "$u" 2>/dev/null)" = active ] || fail="$fail $u"
done

if [ -n "$fail" ]; then
  # ★ 아플 때는 **보내지 않는다.** 보내면 바깥이 "정상" 으로 읽는다.
  #   dead-man switch 는 침묵이 곧 경보라, 아픈 채로 뛰는 심장이 제일 나쁘다.
  echo "정상이 아니므로 heartbeat 를 보내지 않는다:$fail" >&2
  curl -sf --max-time 5 "$URL/fail" >/dev/null 2>&1 || true   # 지원하면 즉시 알린다
  exit 1
fi

curl -sf --max-time 10 "$URL" >/dev/null && echo "heartbeat 보냄" || {
  echo "heartbeat 발송 실패 — 네트워크? (기기는 정상)" >&2; exit 1; }
