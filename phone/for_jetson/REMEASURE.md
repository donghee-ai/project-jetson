# 며칠 뒤에 재는 것 세 가지

2026-09-04 에 여러 곳을 고쳤는데, **효과를 지금 재면 안 되는** 것이 셋 있다.
셋 다 같은 이유다 — 관측 기간 대부분 워처가 실제로 죽어 있었거나 테스트 사용이
섞여 있어 **깨끗한 구간이 없다.**

**언제**: 워처 수정 뒤 깨끗한 관측 구간이 충분히 쌓인 이후
**어디서**: 젯슨만 있으면 된다. 폰 연결 불필요.

---

## 1. `lt doctor` 의 웹 임계값

지금 `lifetrainer/cli.py` 의 "워처 침묵" 검사는 **미디어만** 본다.
웹은 임계값이 없어 빠져 있고, 코드 주석에 그 이유가 적혀 있다.

### 왜 못 정했나

이전 기간은 웹 워처가 실제로 죽어 있던 구간이 섞여 발동 횟수를 기준으로 쓸 수 없다.
개인별 날짜·표본 수·임계값 결과는 공개본에서 제거했다.

### 재는 법

```bash
ssh user
cd /home/user/project/project-jetson/life-trainer
python3 - <<'PY'
import sqlite3, bisect, datetime
c = sqlite3.connect('data/lifetrainer.db')
FROM = datetime.datetime.fromisoformat("YYYY-MM-DD").timestamp()  # 로컬 기준일로 교체
def series(b):
    return [r[0] for r in c.execute(
        'SELECT ts FROM aw_event WHERE bucket_id=? AND ts>=? ORDER BY ts', (b, FROM))]
S, W = series('aw-watcher-android'), series('aw-watcher-android-web')
if len(S) < 10 or len(W) < 10:
    raise SystemExit('아직 표본이 부족하다')
rows, t = [], max(S[0], W[0])
while t < min(S[-1], W[-1]):
    lag = lambda a: (t - a[bisect.bisect_right(a, t) - 1]) / 3600
    rows.append((t, lag(S), lag(W))); t += 1800
for TH in (6, 12, 24, 48):
    hit = [r for r in rows if r[1] <= 2 and r[2] >= TH]
    days = {datetime.datetime.fromtimestamp(r[0]).strftime('%m-%d') for r in hit}
    print('  웹 임계 %2dh → 발동 %4d표본 / %d일 %s' % (TH, len(hit), len(days), sorted(days)))
PY
```

### 어떻게 판정하나

**0일 발동하는 가장 작은 임계값**을 고른다. 그 기간에 웹 워처가 정상이었음을 먼저
확인해야 한다 (`lt doctor` 가 내내 조용했는지).

정했으면 `cli.py` 의 `MEDIA_LAG_WARN_H` 옆에 `WEB_LAG_WARN_H` 를 넣고, 폰
`WatcherHealth.WEB_LAG_WARN_MS` (지금 `null`)에 **같은 숫자**를 넣는다.
★ 두 곳이 다른 답을 내면 어느 쪽을 믿어야 하는지가 새 문제가 된다.

---

## 2. 유튜브 제목 채움률

### 이전 기준선

개인 시청 기록과 테스트 사용이 섞인 날짜별 기준선은 공개본에서 제거했다. 워처가
안정된 이후의 구간만 아래 쿼리로 로컬에서 다시 잰다.

### 재는 법

```bash
python3 - <<'PY'
import sqlite3
c = sqlite3.connect('data/lifetrainer.db')
tot = n = 0
for day, cnt, t in c.execute("""
    SELECT day, COUNT(*), SUM(CASE WHEN COALESCE(top_title,'') NOT IN ('','YouTube','Shorts')
                                   THEN 1 ELSE 0 END)
    FROM slot WHERE top_app='com.google.android.youtube' AND day>='2026-09-05'
    GROUP BY 1 ORDER BY 1"""):
    print('  %s %4d칸 중 %4d (%3.0f%%)' % (day, cnt, t, 100*t/cnt)); tot += cnt; n += t
print('  합계 %d/%d = %.0f%%' % (n, tot, 100*n/tot if tot else 0))
PY
```

★ `"Shorts"` 를 **제외**하고 센다. 쇼츠는 개별 제목을 일부러 안 쫓기로 했으므로
(폰 `ecc2545`) 그 칸이 비는 것은 고장이 아니다. 세고 싶으면 따로 센다.

### 무엇을 기대하나

09-04 이전 대비 오르면 `aef585d`(커서 절단)와 `57b35e0`(미디어 워처 자가복구)이
효과를 낸 것이다. **안 오르면 남은 원인이 따로 있다는 뜻이고, 그때 다시 판다.**
지금 남아 있는 후보는 광고 구간과 메타데이터를 안 주는 재생이다 — 미측정이다.

---

## 3. 웹 워처 커버리지

**크롬을 쓴 시간 대비 웹 이벤트가 덮은 시간.** 처음 재 보니 낮았다. 개인별 날짜·시간·
커버리지는 공개본에서 제거했고, 워처가 죽어 있던 구간은 측정에서 뺀다.

**원인을 아직 안 쟀다.** 제목 귀속 문제(폰 `6848888`)와는 별개다 — 그건 *어느 칸에*
붙느냐고 이건 *아예 안 들어오는* 쪽이다. 후보만 적어 둔다: 탭 전환 없이 스크롤만 하는
구간, `getWindows()` 500ms 스로틀, 주소창 안내 문구 필터(폰 `652b6f8`)가 지나치게
많이 거르는 경우.

### 재는 법

```bash
python3 - <<'PY'
import sqlite3
c = sqlite3.connect('data/lifetrainer.db')
print('  날짜        크롬세션   웹이벤트   커버리지')
for day, s in c.execute("""
    SELECT date(ts,'unixepoch','localtime'), SUM(duration)/60.0 FROM aw_event
    WHERE bucket_id='aw-watcher-android' AND app='com.android.chrome'
      AND ts >= strftime('%s','2026-09-05') GROUP BY 1 ORDER BY 1"""):
    w = c.execute("""
        SELECT COALESCE(SUM(duration),0)/60.0 FROM aw_event
        WHERE bucket_id='aw-watcher-android-web'
          AND date(ts,'unixepoch','localtime')=?""", (day,)).fetchone()[0]
    print('  %s  %7.1f분  %7.1f분   %3.0f%%' % (day, s, w, 100*w/s if s else 0))
PY
```

### 어떻게 판정하나

★ **100% 를 기대하면 안 된다.** 웹 이벤트는 URL 이 바뀔 때만 나므로, 한 페이지를 오래
보면 그 시간은 한 이벤트의 duration 으로 들어와야 정상인데 **하트비트 병합이 어디서
끊기는지 아직 모른다.** 먼저 **하루치 크롬 세션 하나를 골라 웹 이벤트와 눈으로 맞춰
보는 것**이 순서다 — 비율만 봐서는 원인을 못 가른다.

깨끗한 구간에서도 여전히 40%대면 폰 `WebWatcher` 를 판다. 크게 오르면 09-03~04 수치가
내 테스트 사용에 오염됐던 것이므로 이 항목을 닫는다.
