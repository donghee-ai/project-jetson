#!/usr/bin/env bash
# 이 기기를 **처음부터 다시 세우는 데 필요한 것 전부**를 하나로 묶는다.
#
# ★ 왜 저장소 백업만으로는 부족한가
#   `life-trainer/scripts/backup.sh` 는 DB 와 앱 설정을 담는다. 그런데 이 기기를
#   되살리려면 **저장소 밖에 있는 것들**이 더 필요하다 — 실제로 조사해 보니:
#
#     ~/.openclaw/          에이전트 배선. 없으면 증상이 "툴을 안 부른다" 뿐이다
#     ~/.cloudflared/       터널 자격증명. 폰 수집 경로가 여기로 온다
#     ~/project/wifi/       ★ MT7601U 드라이버. **커널 hold 의 이유**이고,
#                             재플래시하면 이게 없으면 WiFi 가 안 붙는다
#     ~/.config/systemd/    유닛 심링크·드롭인·활성 목록
#     ~/.config/jetson/     heartbeat URL
#
#   반대로 **큰 것은 넣지 않는다** — 되살릴 수 있는 것이기 때문이다:
#     ~/models/     5.3G  → 파일명 + sha256 만. 다시 받으면 된다
#     ~/llama.cpp/  1.5G  → commit + 빌드 옵션만. 다시 빌드하면 된다
#     .venv         → requirements 로 다시 만든다
#
#   기준은 크기가 아니라 **"다시 만들 수 있는가"** 다.
#
# ★★ 산출물에는 **토큰·API 키·터널 자격증명·개인 활동 기록**이 들어간다.
#    0600 으로 만들고, 옮길 때 암호화한다. 공개 저장소나 채팅에 올리지 말 것.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1
REPO=$PWD
LT=$REPO/life-trainer

STAMP=$(date +%Y%m%d-%H%M)
OUT="$REPO/recovery-bundle-$STAMP.tar.gz"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
B="$TMP/recovery-$STAMP"; mkdir -p "$B"

say() { printf "  %-34s %s\n" "$1" "$2"; }
echo "▶ 복구 묶음 만들기 — $STAMP"
echo

# ── 1. DB (검증까지 하고 넣는다) ─────────────────────────────
mkdir -p "$B/db"
"$LT/.venv/bin/lt" backup --out "$B/db/lifetrainer.db" >/dev/null
qc=$("$LT/.venv/bin/python" -c "
import sqlite3;print(sqlite3.connect('file:$B/db/lifetrainer.db?mode=ro',uri=True).execute('PRAGMA quick_check').fetchone()[0])")
[ "$qc" = ok ] || { echo "❌ DB 백업이 quick_check 실패 — 묶음을 만들지 않는다" >&2; exit 1; }
# ★ 검사하려고 연 것만으로 `-wal`·`-shm` 이 옆에 생긴다. 그대로 묶으면
#   **"동작 중인 WAL DB 를 복사한 것"** 과 구분이 안 된다 — data/backup/ 에 그런
#   사본이 하나 있었고 그게 이 runbook 을 쓰게 만든 결함이다. 검증이 산출물을
#   더럽히면 안 된다.
rm -f "$B/db/lifetrainer.db-wal" "$B/db/lifetrainer.db-shm"
say "DB" "$(du -h "$B/db/lifetrainer.db" | cut -f1) · quick_check ok"

# ── 2. 저장소 밖 설정 — 못 되살리는 것들 ────────────────────
mkdir -p "$B/home"
copy() { [ -e "$1" ] && cp -a "$1" "$B/home/$2" 2>/dev/null && say "$2" "$(du -sh "$B/home/$2" | cut -f1)" || say "$2" "(없음)"; }

copy "$HOME/.cloudflared"        cloudflared
copy "$HOME/.config/jetson"      config-jetson
copy "$LT/config/lifetrainer.toml" lifetrainer.toml

# ★ .openclaw 는 통째로 넣지 않는다 — npm 캐시 40M 과 대화 원문 세션 30M 이 있다.
#   배선(json·에이전트 정의)만 가져간다. 세션은 대화 원문이라 보존 기간 문제이기도 하다.
mkdir -p "$B/home/openclaw"
cp -a "$HOME/.openclaw/openclaw.json" "$B/home/openclaw/" 2>/dev/null || true
for d in agents workspace credentials identity devices; do
  [ -d "$HOME/.openclaw/$d" ] && cp -a "$HOME/.openclaw/$d" "$B/home/openclaw/" 2>/dev/null || true
done
rm -rf "$B/home/openclaw"/agents/*/sessions 2>/dev/null || true
say "openclaw 배선" "$(du -sh "$B/home/openclaw" | cut -f1) (npm 캐시·세션 원문 제외)"

# ── 3. ★ WiFi 드라이버 — 커널 hold 의 이유 ───────────────────
#   재플래시하면 이게 없으면 WiFi 가 안 붙는다. 소스와 빌드 스크립트를 같이 넣는다.
if [ -d "$HOME/project/wifi" ]; then
  cp -a "$HOME/project/wifi" "$B/wifi-driver"
  say "wifi-driver" "$(du -sh "$B/wifi-driver" | cut -f1) ★ 커널 hold 의 이유"
else say "wifi-driver" "(없음 — 재플래시 후 WiFi 가 안 붙을 수 있다)"; fi

# ── 4. systemd 실제 상태 ─────────────────────────────────────
mkdir -p "$B/systemd"
cp -a "$HOME/.config/systemd/user" "$B/systemd/user" 2>/dev/null || true
# 심링크는 대상이 없으면 무의미하다. 어디를 가리켰는지 텍스트로도 남긴다.
find "$HOME/.config/systemd/user" -type l -printf '%p -> %l\n' 2>/dev/null > "$B/systemd/symlinks.txt"
systemctl --user list-unit-files --state=enabled --no-legend > "$B/systemd/enabled.txt" 2>/dev/null || true
systemctl --user list-timers --all --no-legend           > "$B/systemd/timers.txt"  2>/dev/null || true
say "systemd" "$(wc -l < "$B/systemd/enabled.txt") enabled · 심링크 $(wc -l < "$B/systemd/symlinks.txt")"

# ── 5. 다시 만들 수 있는 것 — 실물 대신 재현 정보 ────────────
mkdir -p "$B/rebuild"
{
  echo "# 다시 만들 수 있는 것들. 실물은 안 넣었다 (크기가 아니라 재현 가능성이 기준이다)."
  echo
  echo "## 모델 가중치  (~/models · $(du -sh "$HOME/models" 2>/dev/null | cut -f1))"
  for f in "$HOME"/refs/models/*.gguf; do
    [ -e "$f" ] && echo "  $(basename "$f")  $(stat -c%s "$f") bytes  sha256=$(sha256sum "$f" | cut -c1-16)…"
  done
  echo
  echo "## llama.cpp  (~/llama.cpp · $(du -sh "$HOME/llama.cpp" 2>/dev/null | cut -f1))"
  echo "  commit  $(git -C "$HOME/llama.cpp" rev-parse HEAD 2>/dev/null)"
  echo "  빌드    $(grep -hE 'GGML_CUDA:BOOL|CMAKE_CUDA_ARCHITECTURES|CMAKE_BUILD_TYPE:' "$HOME/llama.cpp/build/CMakeCache.txt" 2>/dev/null | tr '\n' ' ')"
  echo "  절차    operate/notes/llm-runtime.md · measure/tools/build-llamacpp.sh"
  echo
  echo "## 런타임 버전"
  echo "  node(system)  $(/usr/local/bin/node --version 2>/dev/null)"
  echo "  node(nvm)     $(node --version 2>/dev/null)"
  echo "  openclaw      $(openclaw --version 2>/dev/null | head -1)"
  echo "  python        $("$LT/.venv/bin/python" --version 2>&1)"
  echo "  cloudflared   $("$HOME/bin/cloudflared" --version 2>/dev/null | head -1)"
  echo
  echo "## 저장소"
  echo "  origin  $(git -C "$REPO" remote get-url origin 2>/dev/null)"
  echo "  HEAD    $(git -C "$REPO" rev-parse HEAD)"
  echo "  더러운 파일 $(git -C "$REPO" status --porcelain | wc -l) 개"
  echo
  echo "## OS / BSP"
  echo "  $(lsb_release -ds 2>/dev/null) · $(uname -r)"
  echo "  L4T      $(dpkg-query -W -f='${Version}' nvidia-l4t-core 2>/dev/null)"
  echo "  JetPack  $(dpkg-query -W -f='${Version}' nvidia-jetpack 2>/dev/null)"
  echo "  hold     $(apt-mark showhold 2>/dev/null | tr '\n' ' ')"
  echo "  DKMS     $(dkms status 2>/dev/null | tr '\n' ' ')"
  echo
  echo "## 직접 설치한 apt 패키지"
  apt-mark showmanual 2>/dev/null | tr '\n' ' ' | fold -sw 100 | sed 's/^/  /'
} > "$B/rebuild/versions.txt"
cp "$REPO/environment.md" "$B/rebuild/" 2>/dev/null || true
say "재현 정보" "rebuild/versions.txt"

# ── 6. 어떻게 되살리나 ───────────────────────────────────────
cp "$LT/docs/runbook-backup-restore.md" "$B/RESTORE.md" 2>/dev/null || true
cat > "$B/README.txt" <<TXT
젯슨 복구 묶음 — $STAMP

★★ 이 안에는 Slack 토큰 · API 키 · Cloudflare 터널 자격증명 ·
   개인 활동 기록(창 제목 포함)이 들어 있다. 공개된 곳에 올리지 말 것.

들어 있는 것 (다시 만들 수 없는 것들)
  db/lifetrainer.db     활동·계획·문서·잡 (quick_check 통과 확인함)
  home/lifetrainer.toml 앱 설정 — 토큰·키가 여기 있다
  home/openclaw/        에이전트 배선 (npm 캐시·대화 세션은 뺐다)
  home/cloudflared/     터널 자격증명 — 폰 수집 경로
  home/config-jetson/   heartbeat URL
  wifi-driver/          ★ MT7601U 드라이버. 커널 hold 의 이유이고
                          재플래시 후 이게 없으면 WiFi 가 안 붙는다
  systemd/              유닛 심링크·활성 목록·타이머

안 들어 있는 것 (다시 만들 수 있는 것들 — rebuild/versions.txt 에 재현 정보)
  ~/models/     5.3G  모델 가중치. 파일명 + sha256 로 대조해 다시 받는다
  ~/llama.cpp/  1.5G  commit + 빌드 옵션으로 다시 빌드한다
  .venv               pyproject 로 다시 만든다
  저장소 자체         git clone (HEAD 는 versions.txt 에)

되살리는 순서: 이 묶음의 RESTORE.md — 5-2 절
TXT
say "안내" "README.txt · RESTORE.md"

# ── 7. 묶기 ─────────────────────────────────────────────────
tar czf "$OUT" -C "$TMP" "recovery-$STAMP"
chmod 600 "$OUT"
sha256sum "$OUT" > "$OUT.sha256"
echo
say "산출물" "$OUT"
say "크기" "$(du -h "$OUT" | cut -f1)"
say "해시" "$(cut -c1-16 < "$OUT.sha256")…"
echo
echo "  ★ 0600 이고 **암호화돼 있지 않다.** 옮긴 뒤 이 기기에서는 지운다:"
echo "      rm $OUT $OUT.sha256"
