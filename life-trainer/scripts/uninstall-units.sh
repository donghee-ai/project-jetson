#!/usr/bin/env bash
# install-units.sh 를 되돌린다: 유닛을 멈추고 심링크를 지운다.
# DB(data/lifetrainer.db)나 설정(config/lifetrainer.toml)은 건드리지 않는다.
#
# ★ 목록을 여기 안 적는다. 전에는 install 과 서로 다른 목록을 들고 있어서
#   "자동화 전체 끄기" 가 web·llama-embed·nightly 를 남겨 뒀다 (2026-08-28).
#   심링크가 걸린 것은 **전부** 내린다 — desired-state 에서 빠진 것도 포함해야
#   "전체 끄기" 가 실제로 전체가 된다.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC_DIR="$REPO_DIR/systemd"
UNIT_DST_DIR="$HOME/.config/systemd/user"

echo "== Life Trainer systemd 유닛 제거 =="

for unit_path in "$UNIT_SRC_DIR"/*.timer "$UNIT_SRC_DIR"/*.service; do
    u=$(basename "$unit_path")
    if systemctl --user is-enabled "$u" >/dev/null 2>&1 || \
       systemctl --user is-active  "$u" >/dev/null 2>&1; then
        systemctl --user disable --now "$u" >/dev/null 2>&1 && echo "  ⏹  $u"
    fi
    rm -f "$UNIT_DST_DIR/$u"
done

systemctl --user daemon-reload
echo
left=$(systemctl --user list-units --all --no-legend 'lifetrainer-*' 'llama-embed*' 2>/dev/null | wc -l)
echo "  남은 유닛: ${left}개 (0 이어야 한다)"
echo "  ※ llama-server 는 Life Trainer 소유가 아니다 — operate/ 에서 따로 관리한다"
