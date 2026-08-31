#!/usr/bin/env bash
# §A-4 — journal 을 영속화한다. **sudo 가 필요해 에이전트가 못 했다.**
#
# 왜: /var/log/journal 이 없어 journald 가 메모리에만 쓴다. 재부팅하면 전부 사라지고,
#     HANDOFF §10 이 안내하는 journalctl 명령이 빈손이 된다.
#     이 프로젝트가 겪은 사고(모델 id 미확인 1시간 다운, 야간 배치 277건 유실)는
#     전부 "언제부터 그랬나" 를 물어야 풀리는 종류였다.
set -euo pipefail

echo "▶ 1. 용량 상한을 먼저 건다 (rootfs 가 단일 파티션이다)"
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/persist.conf >/dev/null <<'CONF'
[Journal]
Storage=persistent
SystemMaxUse=500M
RuntimeMaxUse=100M
MaxRetentionSec=30day
CONF

echo "▶ 2. 저장 디렉토리 생성"
sudo mkdir -p /var/log/journal
sudo systemd-tmpfiles --create --prefix /var/log/journal

echo "▶ 3. 적용"
sudo systemctl restart systemd-journald
sudo journalctl --flush

echo "▶ 4. 확인"
journalctl --disk-usage
echo
echo "★ 재부팅 뒤에 아래가 이전 부팅을 보여주면 끝난 것이다:"
echo "    journalctl --user -u llama-server -b -1 -n 20"
echo "    bash bench/verify-boot.sh        # §6 항목이 초록불이 된다"
