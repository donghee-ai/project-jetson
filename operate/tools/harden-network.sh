#!/usr/bin/env bash
# 이 기기가 LAN 전체에 열어 둔 것을 닫는다. **sudo 필요. 되돌리는 법이 아래 있다.**
#
# ★ 판정 근거 (2026-08-28 실측)
#
#   | 포트 | 무엇 | 이 기기에서 쓰나 |
#   |------|------|------------------|
#   | 111  | rpcbind      | ❌ **NFS 마운트 0건.** 아무도 안 쓴다 |
#   | 631  | snap cupsd   | ❌ 프린터 없음. 헤드리스 서버다 |
#   | 5353 | avahi (mDNS) | ⚠️ 판단 필요 — 아래 참조 |
#
#   노출 범위는 인터넷이 아니라 **LAN 전체**다 (192.168.0.0/24 두 인터페이스).
#   방화벽은 없다 (ufw·nftables 둘 다 inactive). 즉 같은 공유기에 붙은 무엇이든
#   이 포트들에 닿는다. rpcbind 는 원격 취약점 이력이 길고, 이 기기에는
#   **개인 활동 기록과 창 제목이 들어 있는 DB** 가 있다.
#
#   ★ 안 쓰는 것을 끄는 것이 방화벽 규칙보다 먼저다 — 규칙은 잊히지만
#     꺼진 서비스는 잊혀도 안 열린다.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "sudo 로 실행할 것" >&2; exit 1; }

echo "▶ 끄기 전 상태 (되돌릴 때 필요하다)"
ss -tulnp 2>/dev/null | grep -E ":(111|631|5353)\b" | sed 's/^/   /' || true
echo

echo "▶ rpcbind — NFS 를 안 쓰므로 끈다"
systemctl disable --now rpcbind.socket rpcbind.service 2>&1 | tail -2 || true

echo "▶ snap cups — 프린터가 없는 헤드리스 서버다"
snap stop --disable cups 2>&1 | tail -1 || true

cat <<'NEXT'

▶ avahi(5353) 는 끄지 않았다 — 사람이 정할 몫

   mDNS 는 `ubuntu.local` 같은 이름으로 이 기기를 찾는 데 쓰인다. 지금 접속은
   Tailscale IP 와 LAN IP 로 하고 있으므로 없어도 되지만, **끄면 이름으로 못 찾는다.**
   쓰지 않는 것이 확실하면:
       sudo systemctl disable --now avahi-daemon.socket avahi-daemon.service

▶ 확인

   ss -tulnp | grep -E ':(111|631)\b'    # 아무것도 안 나와야 한다
   make host-status                       # 나머지는 그대로인지
   bash bench/verify-boot.sh              # 우리 서비스는 안 다쳤는지

▶ §rollback

   sudo systemctl enable --now rpcbind.socket
   sudo snap start --enable cups

▶ 그다음 — 방화벽은 별건이다

   서비스를 끄는 것은 "안 쓰는 것을 안 여는 것" 이다. 방화벽은 "쓰는 것을 아무나
   못 쓰게" 다. 우리 웹 플래너(:8770)는 이미 Tailscale IP 에만 바인딩돼 있고
   llama-server 는 127.0.0.1 이라, 지금 남는 것은 SSH(:22) 뿐이다.
   SSH 를 tailnet 으로만 제한할지는 **기기에 물리적으로 못 닿을 때 잠기는 위험**과
   같이 판단해야 한다 — 여기서 대신 정하지 않는다.
NEXT
