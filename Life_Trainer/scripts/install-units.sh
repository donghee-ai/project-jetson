#!/usr/bin/env bash
# Life Trainer systemd 사용자 유닛을 등록·기동한다. (일반 사용자 권한, sudo 불필요)
#
# 이 저장소의 systemd/*.service, systemd/*.timer 파일을 ~/.config/systemd/user/ 에
# 심링크로 건다 — 저장소를 고치면 유닛도 같이 바뀐다 (openclaw-setup/install.sh 와 같은 패턴).
#
# 기동하는 것:
#   - lifetrainer-{sync,daily,weekly,collect,digest,nightly,nightly-stop}.timer
#   - lifetrainer-worker.service                            (상시 GPU 잡 워커)
#
# 기동하지 않는 것:
#   - lifetrainer-slack.service — 지금은 Slack mode="notify"(발송 전용)라 불필요하다.
#     심링크는 걸어두므로, mode="bolt" 로 바꾼 뒤 아래 명령으로 직접 켜면 된다:
#       systemctl --user enable --now lifetrainer-slack
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC_DIR="$REPO_DIR/systemd"
UNIT_DST_DIR="$HOME/.config/systemd/user"

TIMERS=(
    lifetrainer-sync
    lifetrainer-daily
    lifetrainer-weekly
    lifetrainer-collect
    lifetrainer-digest
    lifetrainer-nightly
    lifetrainer-nightly-stop
)

echo "== Life Trainer systemd 유닛 설치 =="
echo "저장소: $REPO_DIR"
echo "유닛 디렉터리: $UNIT_DST_DIR"
echo

mkdir -p "$UNIT_DST_DIR"

for unit_path in "$UNIT_SRC_DIR"/*.service "$UNIT_SRC_DIR"/*.timer; do
    name="$(basename "$unit_path")"
    ln -sf "$unit_path" "$UNIT_DST_DIR/$name"
    echo "심링크: $name"
done

echo
systemctl --user daemon-reload

echo
echo "-- 타이머 기동 --"
for base in "${TIMERS[@]}"; do
    systemctl --user enable --now "${base}.timer"
done

echo
echo "-- 상시 워커 기동 --"
systemctl --user enable --now lifetrainer-worker.service

echo
echo "-- lifetrainer-slack.service --"
echo "심링크만 걸었고 enable 하지 않았다 (mode=\"notify\" 인 동안은 불필요)."
echo "슬래시 커맨드가 필요해지면(config/lifetrainer.toml [slack].mode = \"bolt\" 로 바꾼 뒤):"
echo "  systemctl --user enable --now lifetrainer-slack"

echo
echo "-- linger 확인 --"
if loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q "Linger=yes"; then
    echo "linger 활성화됨 — 재부팅 후에도 타이머/워커가 뜬다."
else
    echo "linger 가 꺼져 있다. 이 상태로 재부팅하면 SSH 로 로그인하기 전까지"
    echo "사용자 유닛이 하나도 안 뜬다. 다음을 사람이 직접 실행해야 한다 (sudo 필요):"
    echo
    echo "    sudo loginctl enable-linger $USER"
fi

echo
echo "-- 현재 상태 --"
systemctl --user is-active "${TIMERS[@]/%/.timer}" lifetrainer-worker.service || true
