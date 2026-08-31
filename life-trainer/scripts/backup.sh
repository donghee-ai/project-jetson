#!/usr/bin/env bash
# 백업 한 번. 타이머가 하루 한 번 부른다.
#
# ★ 왜 스크립트가 필요한가 (2026-08-28)
#   `lt backup` 은 SQLite 온라인 백업 API 를 쓴다 — 구현은 옳다. 없던 것은 그 주변 전부다:
#   자동 실행 · 무결성 검사 · 해시 · 보존 정책 · 원본 밖 복제 · 복원 시험.
#   그래서 data/backup/ 의 최신 파일이 닷새 묵어 있었고, 그중 하나는 `.db-wal`·`.db-shm`
#   이 딸려 있었다 — **동작 중인 WAL DB 를 그냥 복사한 것**이라 정합성 보장이 없다.
#   백업이 있다는 사실은 복원 가능하다는 뜻이 아니다.
#
#   DB 만으로는 못 돌아온다. 설정·게이트웨이 배선·유닛 활성 목록도 같이 담는다.
#   ★ 이 묶음에는 **비밀값이 들어간다.** 0600 으로 만들고 data/ 밖으로 내보낼 때는
#     반드시 암호화한다 (아래 LT_BACKUP_REMOTE).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

VENV=.venv/bin
OUT_DIR=data/backup
STAMP=$(date +%Y-%m-%d)
DB_OUT="$OUT_DIR/lifetrainer-$STAMP.db"
BUNDLE="$OUT_DIR/state-$STAMP.tar.gz"

# 보존: 일 7 · 주 4 · 월 3. 삭제는 **검증을 통과한 새 백업이 생긴 뒤에만** 한다.
KEEP_DAILY=${LT_KEEP_DAILY:-7}

fail() { echo "❌ $*" >&2; exit 1; }
info() { echo "   $*"; }

mkdir -p "$OUT_DIR"
echo "▶ 백업 $STAMP"

# ── 1. DB ───────────────────────────────────────────────────
"$VENV/lt" backup --out "$DB_OUT" >/dev/null || fail "lt backup 실패"
info "DB        $DB_OUT ($(du -h "$DB_OUT" | cut -f1))"

# ── 2. 무결성 — 검증 안 한 백업은 백업이 아니다 ──────────────
qc=$("$VENV/python" -c "
import sqlite3, sys
c = sqlite3.connect('file:$DB_OUT?mode=ro', uri=True)
print(c.execute('PRAGMA quick_check').fetchone()[0])" 2>&1)
[ "$qc" = "ok" ] || { rm -f "$DB_OUT" "$DB_OUT-wal" "$DB_OUT-shm"; fail "quick_check 실패 ($qc) — 이 백업은 버렸다"; }

# 복원했을 때 무엇이 들어 있는지도 같이 잰다. 파일 크기만으로는 빈 DB 를 못 거른다.
"$VENV/python" - "$DB_OUT" <<'PY' || fail "백업 내용 확인 실패"
import sqlite3, sys
c = sqlite3.connect(f'file:{sys.argv[1]}?mode=ro', uri=True)
q = lambda s: c.execute(s).fetchone()[0]
ver = c.execute("select value from meta where key='schema_version'").fetchone()
ev, doc = q("select count(*) from aw_event"), q("select count(*) from doc")
assert ev > 0 and doc > 0, f"내용이 비었다 (aw_event={ev}, doc={doc})"
print(f"   무결성    quick_check ok · schema v{ver[0] if ver else '?'} · aw_event {ev:,} · doc {doc:,}")
PY

# ★ 검사가 산출물을 더럽히면 안 된다 (CLAUDE.md §4).
#   WAL 모드 DB 는 **읽기 전용으로 열어도** 옆에 `-wal`·`-shm` 이 생긴다. 위 두 검사가
#   정확히 그걸 한다. 그대로 두면 복원본이 *"안전하게 뜬 백업"* 인지
#   *"돌아가는 DB 를 그냥 복사한 것"* 인지 구분이 안 된다 — runbook 을 쓰게 만든 그 결함이다.
#   `operate/tools/make-recovery-bundle.sh` 는 같은 자리에서 이미 지우고 있었다. 여기만 안 했다.
rm -f "$DB_OUT-wal" "$DB_OUT-shm"

# 옛 정리 루틴이 `.db` 와 `.sha256` 만 지워서 **짝 잃은** `-wal`·`-shm` 이 남아 있다.
# 지나간 것도 여기서 쓸어낸다. 단 `-wal` 이 비어 있지 않으면 손대지 않는다 —
# 체크포인트 안 된 내용이 있다는 뜻이라, 지우면 복원했을 때 데이터가 준다.
for stray in "$OUT_DIR"/*.db-wal "$OUT_DIR"/*.db-shm; do
  [ -e "$stray" ] || continue
  case "$stray" in
    *-wal) if [ -s "$stray" ]; then info "건너뜀    $(basename "$stray") — 비어 있지 않다"; continue; fi ;;
  esac
  rm -f "$stray" && info "정리      $(basename "$stray") (짝 잃은 WAL 부산물)"
done

# ── 3. DB 밖의 것 — 이게 없으면 DB 만 있고 못 돌아온다 ────────
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/state"
cp -a config/lifetrainer.toml            "$tmp/state/" 2>/dev/null || info "(config 없음)"
cp -a ~/.openclaw/openclaw.json          "$tmp/state/" 2>/dev/null || info "(openclaw.json 없음 — 에이전트 배선이 사라진다)"
systemctl --user list-unit-files --state=enabled --no-legend > "$tmp/state/enabled-units.txt" 2>/dev/null
systemctl --user list-timers --all --no-legend            > "$tmp/state/timers.txt" 2>/dev/null
cp -a ~/.config/systemd/user/*.d         "$tmp/state/dropins" 2>/dev/null || true
{ echo "# 복원에 필요한 버전 (2026-08-28 형식)"
  echo "node        $(node --version 2>/dev/null || echo 없음)"
  echo "openclaw    $(openclaw --version 2>/dev/null | head -1 || echo 없음)"
  echo "llama-server $(readlink -f "$(command -v llama-server 2>/dev/null)" 2>/dev/null || echo 경로불명)"
  echo "python      $("$VENV/python" --version 2>&1)"
  echo "git HEAD    $(git -C .. rev-parse HEAD 2>/dev/null || echo 불명)"
} > "$tmp/state/versions.txt"
tar czf "$BUNDLE" -C "$tmp" state
chmod 600 "$BUNDLE"
info "상태묶음  $BUNDLE ($(du -h "$BUNDLE" | cut -f1)) — ★ 비밀값 포함, 0600"

# ── 4. 해시 ─────────────────────────────────────────────────
( cd "$OUT_DIR" && sha256sum "$(basename "$DB_OUT")" "$(basename "$BUNDLE")" > "backup-$STAMP.sha256" )
info "해시      $OUT_DIR/backup-$STAMP.sha256"

# ── 5. 원본 NVMe 밖으로 ─────────────────────────────────────
#   ★ 이 기기는 rootfs 가 암호화 안 된 단일 ext4 다. 같은 디스크의 사본은
#     "실수로 DB 를 고쳤을 때" 에만 쓸모 있고 디스크 고장·도난에는 무력하다.
if [ -n "${LT_BACKUP_REMOTE:-}" ]; then
  if command -v age >/dev/null && [ -n "${LT_BACKUP_AGE_RECIPIENT:-}" ]; then
    for f in "$DB_OUT" "$BUNDLE"; do
      age -r "$LT_BACKUP_AGE_RECIPIENT" -o "$tmp/$(basename "$f").age" "$f" || fail "암호화 실패"
    done
    cp "$tmp"/*.age "$OUT_DIR/backup-$STAMP.sha256" "$LT_BACKUP_REMOTE/" \
      && info "외부복제  $LT_BACKUP_REMOTE (age 암호화)" || fail "외부 복제 실패"
  else
    fail "LT_BACKUP_REMOTE 는 설정됐는데 age 또는 LT_BACKUP_AGE_RECIPIENT 가 없다 —
        개인 활동 기록과 창 제목이 평문으로 나가는 것을 막으려고 일부러 실패시킨다"
  fi
else
  echo "   ⚠️ 외부복제  **없음** — LT_BACKUP_REMOTE 가 비어 있다."
  echo "               지금 백업은 원본과 같은 NVMe 에만 있다. 디스크가 죽으면 같이 죽는다."
fi

# ── 6. 보존 — 검증 통과한 오늘 백업이 생긴 뒤에만 지운다 ──────
old=$(ls -1t "$OUT_DIR"/lifetrainer-????-??-??.db 2>/dev/null | tail -n +$((KEEP_DAILY+1)))
if [ -n "$old" ]; then
  # `-wal`·`-shm` 까지 같이 지운다. 안 지우면 `.db` 만 사라지고 부산물이 영구히 남는다.
  echo "$old" | while read -r f; do rm -f "$f" "$f-wal" "$f-shm" "${f%.db}"*.sha256; info "정리      $(basename "$f")"; done
fi

echo "▶ 완료. **복원해 보지 않은 백업은 아직 백업이 아니다** — scripts/restore-test.sh"
