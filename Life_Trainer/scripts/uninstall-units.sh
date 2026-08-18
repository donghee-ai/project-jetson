#!/usr/bin/env bash
# install-units.sh 를 되돌린다: 타이머/서비스를 멈추고 심링크를 지운다.
# DB(data/lifetrainer.db)나 설정(config/lifetrainer.toml)은 건드리지 않는다.
set -euo pipefail

UNIT_DST_DIR="$HOME/.config/systemd/user"

UNITS=(
    lifetrainer-sync.service
    lifetrainer-sync.timer
    lifetrainer-daily.service
    lifetrainer-daily.timer
    lifetrainer-weekly.service
    lifetrainer-weekly.timer
    lifetrainer-collect.service
    lifetrainer-collect.timer
    lifetrainer-digest.service
    lifetrainer-digest.timer
    lifetrainer-worker.service
    lifetrainer-slack.service
)

echo "== Life Trainer systemd 유닛 제거 =="

for unit in "${UNITS[@]}"; do
    systemctl --user disable --now "$unit" 2>/dev/null || true
done

for unit in "${UNITS[@]}"; do
    link="$UNIT_DST_DIR/$unit"
    if [ -L "$link" ] || [ -e "$link" ]; then
        rm -f "$link"
        echo "제거: $unit"
    fi
done

systemctl --user daemon-reload

echo
echo "완료. DB/설정 파일은 그대로 남아 있다."
echo "linger 를 되돌리려면(다른 사용자 서비스가 없을 때만): sudo loginctl disable-linger $USER"
