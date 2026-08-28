#!/usr/bin/env bash
# 백업을 **실제로 복원해 본다.** 월 1회.
#
# ★ 왜 필요한가
#   "백업이 있다" 와 "복원할 수 있다" 는 다른 주장이다. 이 저장소가 겪은 실패 4번
#   ("테스트 통과 ≠ 동작") 과 같은 부류다 — 산출물을 실제로 열어봐야 안다.
#   실제로 data/backup/ 에는 `.db-wal` 이 딸린 사본이 하나 있었다. 파일은 있었지만
#   동작 중인 WAL DB 를 복사한 것이라 복원 정합성이 없었다.
#
#   운영 DB 를 절대 건드리지 않는다. 임시 경로에만 쓴다.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
VENV=.venv/bin

SRC=${1:-$(ls -1t data/backup/lifetrainer-????-??-??.db 2>/dev/null | head -1)}
[ -n "$SRC" ] && [ -f "$SRC" ] || { echo "❌ 복원할 백업이 없다"; exit 1; }

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
echo "▶ 복원 시험 — $SRC"

# ── 1. 해시가 기록과 맞나 ────────────────────────────────────
stamp=$(basename "$SRC" .db); stamp=${stamp#lifetrainer-}
sums="data/backup/backup-$stamp.sha256"
if [ -f "$sums" ]; then
  ( cd data/backup && sha256sum -c --ignore-missing "$(basename "$sums")" ) >/dev/null 2>&1 \
    && echo "   해시      기록과 일치" || { echo "   ❌ 해시 불일치 — 파일이 변조·손상됐다"; exit 1; }
else
  echo "   ⚠️ 해시      기록이 없다 (스크립트 도입 이전 백업)"
fi

# ── 2. 복원 ─────────────────────────────────────────────────
cp "$SRC" "$tmp/restored.db"

# ── 3. 복원본이 실제로 쓸 수 있는가 ──────────────────────────
"$VENV/python" - "$tmp/restored.db" <<'PY'
import sqlite3, sys
db = sys.argv[1]
c = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
q = lambda s: c.execute(s).fetchone()[0]

assert q("PRAGMA quick_check") == "ok", "quick_check 실패"
fk = c.execute("PRAGMA foreign_key_check").fetchall()
assert not fk, f"외래키 위반 {len(fk)}건"

ver = c.execute("select value from meta where key='schema_version'").fetchone()
rows = {t: q(f"select count(*) from {t}")
        for t in ("aw_event", "slot", "slot_breakdown", "doc", "plan", "job")}
# 빈 DB 도 quick_check 는 ok 다. **내용이 있는지 따로 본다.**
for t in ("aw_event", "slot_breakdown", "doc"):
    assert rows[t] > 0, f"{t} 가 비었다 — 복원본이 쓸모없다"

# 실제 질의가 도는지 — 스키마만 맞고 조인이 깨진 경우를 잡는다
q("""select count(*) from slot_breakdown b
     join device d on d.id = b.device_id""")
print(f"   schema    v{ver[0] if ver else '?'}")
print("   내용      " + " · ".join(f"{k} {v:,}" for k, v in rows.items()))
print("   질의      slot_breakdown ⋈ device 정상")
PY
rc=$?
[ $rc -eq 0 ] || { echo "❌ 복원본 검증 실패"; exit 1; }

echo "▶ 통과. 이 백업은 복원 가능하다 ($(date '+%Y-%m-%d %H:%M'))"
echo "   ※ 이 시험은 DB 만 본다. config·openclaw.json·유닛 목록은 state-*.tar.gz 에 있고,"
echo "     그쪽 복원 절차는 docs/runbook-backup-restore.md 에 있다."
