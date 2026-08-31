# shellcheck shell=bash
# desired-state.txt 를 읽어 "지금 켜져 있어야 하는 유닛" 목록을 낸다.
# install · uninstall · verify-boot 이 전부 이걸 source 한다 — 목록은 한 곳뿐이다.

# $1 = 저장소(Life_Trainer) 루트
lt_desired_units() {
  local root=$1 mode
  mode=$(grep -oP '^\s*mode\s*=\s*"\K[^"]+' "$root/config/lifetrainer.toml" 2>/dev/null | head -1)
  mode=${mode:-notify}
  awk -v mode="$mode" '
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    { unit = $1; cond = $2
      if (cond == "always") print unit
      else if (cond == "slack=bolt" && mode == "bolt") print unit
    }' "$root/systemd/desired-state.txt"
}

# 조건부라서 지금은 빠진 유닛 (사람에게 왜 빠졌는지 알려주려고)
lt_skipped_units() {
  local root=$1 mode
  mode=$(grep -oP '^\s*mode\s*=\s*"\K[^"]+' "$root/config/lifetrainer.toml" 2>/dev/null | head -1)
  mode=${mode:-notify}
  awk -v mode="$mode" '
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    { if ($2 == "slack=bolt" && mode != "bolt") print $1 "  (slack mode=" mode ")" }' \
    "$root/systemd/desired-state.txt"
}
