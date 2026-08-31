#!/usr/bin/env bash
# Life Trainer systemd 사용자 유닛을 등록·기동한다. (일반 사용자 권한, sudo 불필요)
#
# 무엇을 켜는지는 **여기 안 적는다** — systemd/desired-state.txt 하나가 정본이고
# uninstall-units.sh · ../operate/tools/verify-boot.sh 도 같은 파일을 읽는다.
# 전에는 세 곳이 각자 목록을 들고 있어 서로 달랐다 (2026-08-28).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC_DIR="$REPO_DIR/systemd"
UNIT_DST_DIR="$HOME/.config/systemd/user"
# shellcheck source=_desired-state.sh
. "$REPO_DIR/scripts/_desired-state.sh"

echo "== Life Trainer systemd 유닛 설치 =="
echo "저장소: $REPO_DIR"
echo

mkdir -p "$UNIT_DST_DIR"
for unit_path in "$UNIT_SRC_DIR"/*.service "$UNIT_SRC_DIR"/*.timer; do
    ln -sf "$unit_path" "$UNIT_DST_DIR/$(basename "$unit_path")"
done
echo "심링크: $(ls -1 "$UNIT_SRC_DIR"/*.service "$UNIT_SRC_DIR"/*.timer | wc -l)개"
systemctl --user daemon-reload

echo
echo "-- 기동 (desired-state.txt) --"
mapfile -t WANT < <(lt_desired_units "$REPO_DIR")
for u in "${WANT[@]}"; do
    systemctl --user enable --now "$u" >/dev/null 2>&1 && echo "  ✅ $u" || echo "  ❌ $u"
done

skipped=$(lt_skipped_units "$REPO_DIR")
if [ -n "$skipped" ]; then
    echo
    echo "-- 조건이 안 맞아 안 켠 것 --"
    echo "$skipped" | sed 's/^/  ⏭  /'
    echo "  (조건은 desired-state.txt 에 있다. 설정을 바꾸면 이 스크립트를 다시 돌린다)"
fi

echo
echo "-- linger --"
if loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q "Linger=yes"; then
    echo "  ✅ 활성 — 재부팅 후에도 뜬다"
else
    echo "  ❌ 꺼져 있다. 이 상태로 재부팅하면 SSH 로그인 전까지 유닛이 하나도 안 뜬다:"
    echo "       sudo loginctl enable-linger $USER"
fi

echo
echo "-- 대조 --"
bash "$REPO_DIR/../operate/tools/verify-boot.sh" 2>/dev/null | tail -5 || \
  echo "  (verify-boot.sh 로 desired-state 와 실제를 대조할 것)"
