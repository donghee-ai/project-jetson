#!/usr/bin/env bash
# 지금 이 기기의 현황을 뽑는다. **문서에 숫자를 옮겨 적지 말고 이걸 돌린다.**
#
# 이 저장소는 같은 숫자가 서로 다르게 적힌 사고를 네 번 겪었다
# (테스트 수 두 번 · 분류 규칙 수 한 번 · 실데이터 한 번).
# 원인은 규칙이 아니라 **옮겨 적을 자리가 있다는 것**이었다.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
LT=Life_Trainer

echo "▶ $(date '+%Y-%m-%d %H:%M %Z') · $(uptime -p)"
echo

echo "▶ 서비스"
for u in llama-server llama-embed openclaw-gateway lifetrainer-web lifetrainer-worker lifetrainer-slack; do
  printf "   %-20s %s\n" "$u" "$(systemctl --user is-active "$u" 2>&1)"
done
printf "   %-20s %s개\n" "lifetrainer 타이머" "$(systemctl --user list-timers --no-legend 2>/dev/null | grep -c lifetrainer)"
echo

echo "▶ 실데이터"
"$LT/.venv/bin/python" - <<'PY'
import sqlite3, datetime as dt
c = sqlite3.connect('file:Life_Trainer/data/lifetrainer.db?mode=ro', uri=True)
q = lambda s: c.execute(s).fetchone()
ev, lo, hi = q("select count(*), min(ts), max(ts) from aw_event")
doc, summ = q("select count(*), sum(summary is not null and summary<>'') from doc")
job = dict(c.execute("select state,count(*) from job group by 1").fetchall())
ver = q("select value from meta where key='schema_version'")
dev = ", ".join(r[0] for r in c.execute("select name from device order by 1"))
print(f"   기간      {dt.datetime.fromtimestamp(lo):%Y-%m-%d} ~ {dt.datetime.fromtimestamp(hi):%m-%d}")
print(f"   이벤트    {ev:,}")
print(f"   문서      {doc:,} (요약 {summ:,})")
print(f"   GPU 잡    " + " · ".join(f"{k} {v:,}" for k, v in sorted(job.items())))
print(f"   schema    v{ver[0] if ver else '?'}")
print(f"   기기      {dev}")
PY
echo
echo "▶ 코드"
printf '   테스트 파일  %s개 (개수는 make test)\n' "$(git ls-files "$LT/tests/*.py" | wc -l)"
printf "   분류 규칙    %s개\n" "$("$LT/.venv/bin/python" -c "
import yaml
# ★ rules 만 센다. categories(12)·browser_apps(14) 를 같이 더하면 73 이 나오는데
#   그건 분류 규칙이 아니다 — 실제로 그렇게 잘못 세서 맞는 문서를 고칠 뻔했다.
print(len(yaml.safe_load(open('$LT/config/rules.yaml'))['rules']))" 2>/dev/null || echo '?')"
printf "   문서 링크    %s\n" "$(bash bench/check-links.sh 2>/dev/null | tail -1 | sed 's/^ *//')"
