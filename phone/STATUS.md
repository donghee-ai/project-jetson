# LT Phone — 인수인계 (2026-09-04)

> **이 문서 하나로 이어받을 수 있게 썼다.**
> 배경·규격 [fork-build.md](fork-build.md) · 빌드 환경 [BUILD.md](BUILD.md) ·
> 수동 절차 [NEXT-STEPS.md](NEXT-STEPS.md) · 젯슨에 넘긴 것 [for_jetson/](for_jetson/)
>
> ★ **2026-09-05부터 이 폴더의 문서는 젯슨 저장소 `phone/` 에도 있다** (`0b85a72`).
> **정본은 여기(`c:\jetsonapk\`)다.** 저기는 세션 끝에 올리는 사본이고, 저기서 직접
> 고치지 않는다 — 같은 문서가 두 곳에서 각자 자라면 어긋난다. 올리는 법:
> `scp STATUS.md BUILD.md NEXT-STEPS.md fork-build.md user:project/project-jetson/phone/`
> (`for_jetson/` 은 `phone/for_jetson/` 로). 올린 뒤 `make check-fast` 를 돌린다 —
> **낡은 운영 수치를 그 검사기가 잡는다.**
>
> 젯슨 쪽 최신은 저장소 [`donghee-ai/project-jetson`](https://github.com/donghee-ai/project-jetson)
> 의 `life-trainer/` — **이 폴더보다 그쪽이 앞서 있을 수 있다.**
> ★ **다른 세션이 그 저장소에서 동시에 작업 중이다.** 09-05 13:5x 기준 작업 트리는
> 깨끗하고 origin 까지 `cc51f58` 로 밀려 있지만, 어제는 25개 파일이 미커밋이었다.
> **손대기 전에 `git status` 로 남의 미커밋을 확인하고, `git commit -a` 대신 내 파일만
> `git add` 한다.** 남의 파일에 한 줄 보태야 하면 커밋하지 말고 작업 트리에만 둔다 —
> 09-04 에 그렇게 넘긴 색인 줄을 그 세션이 자기 것과 함께 커밋해 갔다.

## 한 줄 요약

폰 → 젯슨 수집이 08-22부터 돌고 있었는데 **여러 곳에서 조용히 새고 있었다.**
09-03~04 이틀에 걸쳐 폰 7건·젯슨 5건을 고쳤고, 새던 12일치를 메웠다.

★ 마지막 점검에서 **새 결함이 하나 나왔고 같은 날 저녁에 닫혔다** — 프라이빗 거르기가
**길이 0 이벤트를 통째로 지우고 있었다.** unlock 만이 아니라 미디어·웹·PC 창 워처의
순간 이벤트가 09-03 15:14 부터 젯슨에 하나도 안 들어왔다. 아래 "09-04 저녁에 한 것".
그걸 **09-04 에 넣은 "워처 침묵" 검사가 잡았다** — 검사는 제 일을 했다.

---

## 지금 상태

| | |
|---|---|
| 앱 | `net.lifetrainer.awphone` "LT Phone" 0.14.0b2 (versionCode 40) · release 서명 |
| 기기 | `phone-example` (Galaxy S24 Ultra `SERIAL-EXAMPLE`, Android 16 / SDK 36) — `kind='phone'` |
| 주소 | `https://lt.example.com` → Cloudflare Tunnel → `100.64.0.2:8770` |
| 터널 | `cloudflared-lt.service` · **`/ingest/` 경로만 공개** |
| 워처 | 앱세션 · afk · media(알림접근) · web(접근성) — 폰에서 5종 다 돌고 **5종 다 젯슨까지 온다** |
| 폰 aw-server | `127.0.0.1:5600` 루프백. 젯슨이 직접 못 닿는다 |
| 폰 저장소 | [`donghee-ai/lt-phone`](https://github.com/donghee-ai/lt-phone) 브랜치 `lifetrainer` · HEAD `6848888` |
| 젯슨 저장소 | `main` · 내 커밋 `1006ff0` · 09-05 13:5x 에 origin 까지 `cc51f58` 로 **push 됨** |
| 젯슨 접속 | `ssh user` → `~/project/project-jetson/life-trainer` (`sqlite3` CLI 는 없다 — `.venv/bin/python` 을 쓴다) |
| `lt doctor` | 09-04 20:15 기준 **OK 20 / WARN 0 / FAIL 0** |

### 폰 로컬 aw-server 에 붙는 법 — **측정의 출발점**

```bash
adb forward tcp:15600 tcp:5600
curl -H "Authorization: Bearer <키>" http://127.0.0.1:15600/api/0/buckets/
```

**API 키는 앱 → ☰ → API Authentication.** 젯슨 값이 맞는지는 **폰 원본과 같은 ts 로
대조해야** 알 수 있고, 그 길이 여기뿐이다. 이틀간 잡은 것 대부분이 이 대조에서 나왔다.

★ **`adb` 는 PATH 에 없다** — `c:/jetsonapk/platform-tools/adb.exe` 를 직접 부른다.
Git Bash 라면 `export MSYS_NO_PATHCONV=1` 을 먼저 해야 경로가 안 망가진다.

### ★ 앱을 업데이트하면 워처 권한을 다시 켜야 한다

`adb install -r` 뒤에 접근성·알림접근이 **목록에는 남은 채 다시 안 붙는다**
(09-03 에 6/6 재현). 설정 화면엔 "켜짐"으로 보여 사람이 봐도 모른다. QS 타일도 같이 죽는다.

```bash
C1=net.lifetrainer.awphone/net.activitywatch.android.watcher.WebWatcher
C2=net.lifetrainer.awphone/net.activitywatch.android.watcher.MediaWatcher
CT=net.lifetrainer.awphone/net.activitywatch.android.privacy.PrivateTileService
adb shell settings put secure enabled_accessibility_services '""'
adb shell settings put secure accessibility_enabled 0 && sleep 2
adb shell settings put secure enabled_accessibility_services "$C1"
adb shell settings put secure accessibility_enabled 1
adb shell cmd notification disallow_listener "$C2" && adb shell cmd notification allow_listener "$C2"
adb shell cmd statusbar remove-tile "$CT" && adb shell cmd statusbar add-tile "$CT"
```

앱이 이제 **스스로 알아채 상시 알림 문구로 말한다** (`652b6f8`). 그래도 업데이트 직후엔
사람이 확인하는 편이 빠르다.

---

## 남은 일

### 1. ★ 며칠 뒤 재측정 — 지금 재면 안 된다

절차·기준선 [`for_jetson/REMEASURE.md`](for_jetson/REMEASURE.md). **09-11 이후**, 젯슨만으로.

| 무엇 | 왜 지금 못 재나 |
|---|---|
| `lt doctor` 의 **웹 임계값** | 관측 기간 대부분 웹 워처가 실제로 죽어 있었다 — 깨끗한 구간이 없다 |
| **유튜브 제목 채움률** | 미디어 워처 정지 구간 + 내 테스트 사용이 섞여 있다 |
| **웹 커버리지** (아래 2번) | 〃 |

이전 유튜브 제목 채움률 기준선은 개인 시청 기록에서 나온 날짜별 값이고 워처 정지
구간까지 섞여 있어 공개본에서 제거했다. 새 기준선은 깨끗한 구간만 로컬에서 다시 잰다.

### 2. 웹 워처 커버리지가 낮다 — 원인 미측정

크롬을 쓴 시간 대비 웹 이벤트가 덮은 시간이다.

개인별 날짜·브라우징 시간·커버리지 값은 공개본에서 제거했다. 워처가 크래시로 죽어
있던 구간은 기준선에서도 제외한다.
제목 귀속 문제(`6848888`)와는 **별개**고, **왜 나머지가 안 잡히는지 아직 안 쟀다.**

### 3. URL 규칙이 실제로 보는 사이트를 안 덮는다

젯슨이 이제 폰 URL 을 슬롯에 붙이는데(`e0352b8`) **분류가 거의 안 바뀐다** — 규칙이
`arxiv·github·mail·op.gg·youtube·뉴스` 뿐이라서다.

```
실제:  m.example-forum.com · m.example-board.com · m.example-portal.com · example-stream.com · example-blog.net · google 검색
```

**"이 시간을 무엇으로 셀 것인가" 라는 사람의 결정**이라 손대지 않았다.
`config/rules.yaml` 의 URL 규칙에 넣으면 된다.

### 4. 무인 확인

배터리 관리가 워처를 죽이는지 확인한다. "제한 없음"을 실제로 적용한 뒤의 깨끗한
구간만 기준선에 넣고, 적용 전 구간은 Doze 면제가 아니므로 섞지 않는다.

### 5. 작은 것들

- **unlock 이벤트가 폰에서 중복 생성된다** — 09-05 에 재 봤다. 아래 "unlock 중복" 참조.
  **집계 피해는 없다** (젯슨 PK `(bucket_id, ts)` 가 같은 ts 를 하나로 접는다)
- **`slot_breakdown.device_id` 가 전부 NULL** — 젯슨 롤업 이슈. 실익 작음
- **플래너 봇 시간 계산 오류** — `build_turn_context` 가 남은 시간을 LLM 에 빼기 시킨다 (미적용)
- ★ **LT Phone 이 배터리 최적화 화이트리스트에 없다** — 아래 "배터리" 참조

### unlock 중복 — 익명화한 진단

폰 로컬에서 중복이 다수 발견됐다. 전체·고유 이벤트 개수는 잠금해제 습관을 드러내므로
공개본에서 제거했다.

원인으로 보는 것: `SessionEventWatcher.nextQueryStartTimestamp()` 가 조회 시작점을
**앱 세션 버킷 마지막 이벤트의 ts + 1ms** 로 잡는데, 그 ts 는 세션의 **시작**이고
진행 중인 세션은 일부러 안 내보낸다. 그래서 커서가 늘 뒤처지고, 그 사이의 unlock 이
매 수집마다 다시 `heartbeatHelper` 된다 (`pulsetime = 0` 이라 병합 안 되고 새 행).

방향은 데이터가 지지했지만 지연/주기만으로 중복 수를 전부 설명하지는 못했다.
특정 횟수의 중복이 유난히 많아 시간이 아니라 **고정된 경로 수**
냄새가 난다. 실제로 같은 수집을 부르는 곳이 둘이다:
`UsageStatsWatcher`(알람)와 `EventParsingWorker`(WorkManager). `ReentrantLock` 은 동시
실행만 막지 반복 실행은 안 막는다.

고치려면 **unlock 전용 커서**를 따로 두면 된다. 급하지 않다.

### 배터리 — 09-05 에 "제한 없음"으로 바꿨다

09-05 오전까지 **Doze 면제가 아니었다.** "09-04 기준 딥슬리핑 목록에 없다"는 맞았지만
그건 다른 목록이다 — 화이트리스트에는 안 들어 있었다.

```
바꾸기 전   dumpsys deviceidle whitelist   → 194개 중 없음      · standby-bucket 10 (ACTIVE)
바꾼 뒤     user,net.lifetrainer.awphone,10554                 · standby-bucket  5 (EXEMPTED)
```

사용자가 폰 설정 → 앱 → LT Phone → 배터리 → **제한 없음** 으로 바꿨고, 위 두 값으로
실제로 걸린 것을 확인했다. ★ **"3일 무인 확인"(남은 일 4)의 기준선은 여기서부터다** —
그 전 구간은 Doze가 알람을 밀던 상태라 섞으면 안 된다.

adb 로 되돌리려면: `adb shell dumpsys deviceidle whitelist -net.lifetrainer.awphone`

---

## 09-04 저녁에 한 것 — 프라이빗 거르기가 길이 0 이벤트를 지웠다

젯슨 커밋 `1006ff0` · 기록 `HISTORY/2026-09-04-the-clip-erased-what-it-could-not-cut.md`

### 무엇이었나

낮에 남겨 둔 "unlock이 더 들어오지 않는다"를 따라가니 **unlock 문제가
아니었다.** 젯슨이 **길이 0 이벤트를 전부 버리고 있었다.**

```python
# lifetrainer/privacy.py — clip_events (고치기 전)
for s, e in timeutil.subtract_spans([(ts, ts_end)], spans):
    out.append(...)
```

`subtract_spans` 는 마지막에 `p[1] > p[0]` 인 조각만 남긴다. 길이 있는 이벤트를 자르고
나온 빈 조각을 버리려는, 그 자리에서는 옳은 줄이다. 그런데 **입력이 처음부터 길이 0 이면
뺄 것이 없어도 결과가 빈 목록**이다. 잘린 게 아니라 없어진 것이다.

`upsert_events` 는 **구간이 하나라도 있으면**(끝난 것이라도) 거르기를 돌린다. 지난 구간도
계속 막아야 하니 그게 맞는데, 그래서 **첫 프라이빗 타일을 누른 순간 발현했다**:

첫 `private_span` 생성 직전까지만 unlock이 남고, 그 뒤부터 길이 0 이벤트가 사라졌다.
개인별 사용 시각은 공개본에서 제거했다.

### 범위가 unlock 만이 아니었다

| 버킷 | 발견 당시 길이 0 이벤트의 마지막 |
|---|---|
| `aw-watcher-android-unlock` | unlock은 **전부** duration 0 |
| `aw-watcher-android-media` | 순간 전환이 사라짐 |
| `aw-watcher-android-web` | 순간 전환이 사라짐 |
| `aw-watcher-window_DESKTOP-EXAMPLE` | **PC도 같이 새고 있었다** |

★ **두 번 숨었다.** 폰도 젯슨도 200 을 주고받았고 `aw_bucket.last_seen` 까지 갱신됐다 —
*버킷은 도착하는데 안이 비는* 모양이라 전송 실패로는 안 보였다. 그리고 unlock 은 활동
시간 집계에 안 들어가 어느 리포트도 안 흔들렸다.
잡은 것은 **"이 워처가 마지막으로 말한 게 언제냐"를 묻는 검사**였다.

### 어떻게 좁혔나

1. 폰 로컬 aw-server를 열어 대조 → 폰 원본에는 있었다. 폰은 무죄
2. 폰 logcat: `전송 성공: 버킷 3개 / 이벤트 2건` — **버킷 수보다 이벤트 수가 적다**
3. 젯슨 `aw_bucket.last_seen`이 갱신됨 → 보내고 있고 **저장만 0건**
4. `upsert_events` → `clip_events` → `subtract_spans` 의 `p[1] > p[0]` 에서 멈춤
5. 3줄짜리 재현으로 확인 (겹치지도 않는 과거 구간 하나만 있어도 0건이 된다)

### 고친 것

- `privacy.clip_events` — 길이 0 이벤트는 **점**으로 다룬다. 구간 안이면 버리고 밖이면
  그대로 둔다. 경계는 구간 빼기와 같은 반열림 `[s, e)`
- `rollup._clip_events_excluding` — 같은 결함이라 같이. 초를 안 물고 오므로 이중 계산은
  안 생기고, 대신 그 순간의 앱·제목이 살아남는다
- `tests/test_private.py` 에 3건 — **반환값이 아니라 `aw_event` 행**을 본다

### 복구

SQLite 온라인 백업을 만든 뒤 폰·PC 원본 export를 다시 가져왔다. 워처별 전후 이벤트
개수와 마지막 사용 시각은 공개본에서 제거했다.

폰은 `/api/0/export` 통째로, PC는 로컬 aw-server(`127.0.0.1:35600`)에서 영향 구간만
뽑아 **둘 다 `lt import`**로 넣었다. PK가 `(bucket_id, ts)`이고 upsert가 `MAX`
라 덮어쓰기가 아니라 메우기다. ★ **프라이빗 구간은 재수입해도 관문에서 다시 잘린다** —
되살아나지 않는다(합성 회귀 데이터로 확인).

★ PC 는 `lt sync` 커서를 되감는 게 정석이지만 `sync_state` 를 직접 쓰는 길이 막혀 있어
**폰과 같은 `lt import` 경로**를 썼다. 결과는 같고 커서는 안 건드린다.

영향 구간 재롤업 뒤 `lt doctor`가 전부 정상으로 돌아왔다.

### 확인

- PC: 수동 import 이후 길이 0 창 이벤트가 라이브 `lt sync` 경로로 다시 들어왔다.
- 폰: 테스트 기기에서 unlock 생성 → 수집 → push → 젯슨 적재를 끝단까지 확인했다.
- 하룻밤 무인 구간에서도 unlock 이벤트가 정상적으로 들어왔다. 개인별 횟수·시각은 제거했다.

### 남의 미커밋을 안 건드렸다 — 그리고 그게 통했다

`HISTORY/README.md` 색인 줄은 일부러 **커밋하지 않고** 작업 트리에만 뒀다. 다른 세션이
같은 파일을 고쳐 둔 상태라, 스테이징하면 그쪽 변경까지 내 커밋에 딸려 들어간다.

09-05 에 확인하니 **그 세션이 자기 줄과 함께 커밋해 갔다** — 색인 줄이 살아서
`HISTORY/README.md:71` 에 있다. 미커밋으로 넘긴 판단이 맞았다.

---

## 09-04 에 한 것

### 젯슨 5건 — `for_jetson/` 의 이슈가 전부 닫혔다

| 무엇 | 커밋 |
|---|---|
| 프라이빗 토글이 **모르는 본문을 켜기로** 읽었다 | `6accfb1` |
| **폰 브라우징 URL 이 슬롯에 하나도 안 붙었다** — `browser_apps` 가 데스크톱 전용 | `e0352b8` |
| 브라우저 슬롯의 `top_title` 을 페이지 제목으로 (표시용) | `9e05a40` |
| `lt doctor` 에 **버킷별 침묵** 검사 | `63b9008` |
| `bucket_type` 의 `media` 분기 — **고치면 안 되는 것이었다** → `w-0028` | `a2db832` |

★ **조사가 계획을 두 번 바꿨다.** `media` 분기는 넣으면 `device_kind_for` 가 폰을
`laptop` 으로 등록할 수 있었고, 그 부재를 지키는 테스트가 이미 둘 있었다. 반대로
**적어 두지 않았던 URL 게이트**가 더 컸다 — 08-23 에 고쳤다고 적힌 것이 절반이었다.

기록은 젯슨 규칙대로 `HISTORY/` 3건 + `docs/progress/2026-09-04.md` 에 있다.
`slot_breakdown` 에는 title 컬럼이 없어 위 변경 중 **활동 시간 총합을 바꾼 것은 없다.**

### 폰 3건

| 무엇 | 커밋 |
|---|---|
| 워처가 혼자 조용해지면 **앱이 스스로 알아챈다** (`WatcherHealth`) | `652b6f8` |
| 주소창 안내 문구가 URL 로 들어갔다 | `652b6f8` |
| **제목이 URL 보다 먼저 바뀌어 이전 페이지에 붙었다** | `6848888` |
| 상시 알림이 프라이빗을 몰랐다 (프라이빗 중에도 "수집 중") | `6848888` |

**제목 귀속**: 창 제목이 URL 보다 먼저 도착해 `news.example-portal.com` 칸에 example-forum 제목이
붙었다. 처음엔 "URL 이 안 바뀌었으면 승격" 으로 고쳤는데 **오차가 반대쪽으로 옮겨갔을
뿐**이었다. 지금은 **1초 동안 가만히 있는 제목만 확정한다**
(`BrowserSessionTracker`). 실기기에서 세 페이지를 넘겨 확인했다.

**워처 자기 감시**: 권한 API 로는 못 잡는다 — `isNotificationAccessGranted` 도
`isAccessibilityAllowed` 도 **"목록에 있나"** 를 볼 뿐인데 이 사고는 *목록엔 있는데 안
붙은* 상태다. 그래서 **결과(버킷의 마지막 이벤트)를 본다.** 밤에 안 울리게 하려고
절대 시간이 아니라 **앱 세션 버킷과 비교**한다 — 폰을 안 쓰면 세션도 같이 끊겨 조건이
성립하지 않는다. 임계값은 젯슨 `lt doctor` 와 **같은 숫자**다 (미디어 12시간).
★ **웹은 임계값이 없다** (`WEB_LAG_WARN_MS = null`) — 위 "남은 일 1" 참조.

---

## 09-03 에 한 것

### 1. ★ push 커서가 자라는 중인 이벤트를 잘랐다 — 가장 컸다

AW 는 하트비트로 **같은 timestamp 의 이벤트를 계속 늘린다** (재생 중인 곡, 이어지는
afk). `PushSender.fetchSince` 가 `ts > cursor` 로 잘라 **첫 스냅샷만** 갔다.

공개용 사본에서 push 경로로 들어온 장시간 afk·media 이벤트만 짧게 잘리는 패턴을
확인했다. **"첫 푸시가 언제 왔나"가 그대로 duration 이 됐다.** 개인별 시각·길이는
공개본에서 제거했다.

→ 경계를 `ts >= cursor` 로 바꾼다. 젯슨은 이미 `duration=MAX(...)` 라 긴 쪽을 남긴다.
자라지 않은 경계 이벤트는 **직전에 보낸 길이와 비교해 거른다**
(`LTSettings.getCursorDuration`) — 안 그러면 매 5분 같은 것을 보내 롤업만 깨운다.

**복구**: 폰 로컬을 통째로 `/api/0/export` 해서 젯슨에 `lt import --rollup`.
젯슨이 `MAX(duration)` 으로 받으므로 **덮어쓰기가 아니라 메우기**다. 사본에서 버킷별
합계와 개별 이벤트가 원본과 맞고 겹침 인플레이션이 없는지 확인했다. 개인별 버킷 합계,
날짜, 백업 파일명은 공개본에서 제거했다.

### 2. WebWatcher 가 장기간 죽어 있었다

`dumpsys accessibility` 의 **Crashed services** 에 있었다. 웹 버킷이 장기간 갱신되지
않았지만 아무도 몰랐다.

원인: `onAccessibilityEvent` 는 **메인 스레드**인데 거기서 `RustInterface` 의 블로킹
JNI/HTTP 를 직접 불렀다. MediaWatcher 는 같은 이유로 08-20 에 워커 스레드로 옮겨졌는데
(`ad935df`) WebWatcher 만 남아 있었다.
→ 버킷 초기화와 `heartbeatHelper` 를 `HandlerThread` 로 뺐다.

### 3. 웹 페이지 제목 0% → 90%

제목은 **노드가 아니라 창 속성**에 있었다.

```
android.webkit.WebView 노드   text=''   contentDescription=''
AccessibilityWindowInfo       title='Chrome: 인기글 목록 - 예시 커뮤니티'   ← 여기
```

활성 `TYPE_APPLICATION` 창의 `title` 에서 앱 라벨 접두사를 뗀다. `getWindows()` 는 IPC 라
500ms 로 죈다. ★ **제목이 앱 이름("Chrome")이면 버린다** — 전환 중·새 탭에서 그렇게
돌아오는데, 저장하면 그 칸이 아무것도 말하지 않는다.

### 4. MediaWatcher 가 **살아 있는 채로** 장시간 멈췄다 (`57b35e0`)

사용자가 Raw Data 화면에서 오래된 갱신 시각을 눈으로 보고 발견했다. 권한도 켜져
있었고 서비스도 바인딩돼 있었다.

크래시 로그가 이미 로테이션돼 **원인 후보 둘 중 어느 쪽인지 못 가렸다.** 둘 다 같은
증상을 내고 둘 다 고칠 수 있어 **둘 다 고쳤다**:

1. **폴링이 이미 아는 세션만 훑었다** — `activeControllers` 는 세션 변경 콜백으로만
   채워지는데 그 콜백이 한 번 끊기면 새 재생이 맵에 영영 안 들어온다
   → 매 폴링마다 `getActiveSessions()` 를 다시 읽어 **15초 안에 자가 복구**
2. **`onDestroy` 뒤 되살아난 인스턴스가 죽은 루퍼를 잡았다** — `HandlerThread` 가 프로퍼티
   초기화라 `quitSafely()` 뒤 `onCreate` 가 오면 죽은 루퍼에 핸들러를 건다
   → `HandlerThread` 를 `onCreate` 에서 만든다

★ 이 정지는 **반복됐다.** 재생 앱 세션은 있었는데 미디어 이벤트만 비는 구간이 여러 번
확인됐다. 화면을 켠 동안에도 안 잡혔으므로 Doze 로는 설명이 안 되고, 세션 콜백 유실이
맞다. 개인별 재생 횟수·날짜·시간대는 공개본에서 제거했다.

### 5. 쇼츠 — "Shorts" 한 덩어리로 (`ecc2545`)

유튜브가 쇼츠에는 MediaSession 을 **갱신하지 않는다.** "제목 필드가 없다"가 아니라
**값이 직전에 본 다른 영상 것으로 얼어붙어 있다**:

```
state    = STOPPED(1) 또는 NONE(0)      재생 중인데 안 재생 중이라고 한다
TITLE    = 아티스트 - 예시 곡 …   쇼츠가 아니라 아까 본 일반 영상
DURATION = 177000                       2분 57초. 재생 중인 쇼츠는 59초였다
VIDEO    = 1280x720                     가로. 쇼츠는 세로다
```

그대로 쓰면 **쇼츠 시간에 엉뚱한 제목이 붙는다 — 비는 것보다 나쁘다.**

→ WebWatcher(접근성)가 쇼츠 플레이어에만 있는 `reel_player_footer_container` 로 "쇼츠
화면"만 판정하고, 미디어 버킷에 `"Shorts"` 한 덩어리로 넣는다.
**개별 제목은 일부러 안 쫓는다** — 10분 칸에 열 개씩 들어가 그 칸을 대표하지 못하고,
뽑으려면 "컨테이너 안에서 가장 넓은 노드" 같은 배치 규칙에 기대야 해서 유튜브가 배치를
바꾸면 `"댓글 10개 보기"` 가 제목 자리에 들어간다. **얻는 것에 비해 위험이 컸다.**

덤: data 가 항상 같아 **하트비트가 계속 병합된다.** 쇼츠를 여러 개 넘긴 2분 33초가
**하나의 153.5초 구간**으로 들어왔다 (옛 방식은 영상마다 끊겨 0초짜리가 여럿 생겼다).

젯슨은 한 줄도 안 고쳐도 된다 — `rollup._title_from_media` 가 같은 패키지의 앱 세션에
얹어 주므로 MediaWatcher 와 같은 data 모양으로 넣었다. 감시 범위는 **유튜브 하나**다.

겪은 것: **유튜브 이벤트는 `event.packageName` 이 null 로 온다** (창의 패키지를 봐야
한다). **`findNode` 같은 클라이언트측 DFS 로는 못 찾는다** — uiautomator 가 같은 순간
보는 노드를 계속 null 로 냈고, `findAccessibilityNodeInfosByViewId` 로 바꾸니 즉시 잡혔다.

### 6. 기기 이름 힌트 (`phone-example` → `예: phone-example`)

힌트가 실제 입력값과 똑같아 채워진 것처럼 보였다.

---

## 프라이빗 모드

폰 절반이 09-03 에 완성됐다 (`908af6f`·`10503a4`). 젯슨 쪽은 09-01 에 이미 있었다.

| 무엇 | 어디 |
|---|---|
| 빠른 설정 **타일** | `privacy/PrivateTileService` — 상단 바 내리면 나온다 (사용자 확인) |
| 로컬 단일 원본 | `privacy/PrivateMode` — 끝나는 시각을 **절대 시각**으로 |
| 자동 만료 | `privacy/PrivateExpiryReceiver` + 워처가 매 수집 때 자체 확인 (이중 방어) |
| 젯슨 폴링·통지 | `privacy/PrivateClient` · `PrivateScheduler` |
| 지연분 정리 | `privacy/PrivateSweeper` — 앱이 자기 DB 에서 지운다 |
| 서명 공용화 | `push/LTAuth` — `LT1` 을 한 곳에 (두 벌이 되면 반드시 어긋난다) |

### 무엇을 막고 무엇을 남기나

| 워처 | 프라이빗 중 |
|---|---|
| 앱 세션 · unlock | **수집 자체를 안 한다** (`SessionEventWatcher` 한 곳이 둘을 같이 몬다) |
| 웹 URL·제목·쇼츠 · 미디어 | 이벤트 처리 첫 줄에서 막는다 |
| **afk** | **계속 기록한다** — 가리는 것은 *무엇을* 했는지지 *폰을 썼는지*가 아니다 |

접근성·알림 서비스를 **언바인드하지 않는다.** 가장 강한 해석은 그것인데, 이 기기는
한번 풀린 바인딩을 스스로 안 되돌린다 — 프라이빗 한 번에 워처가 며칠 죽는 것과
맞바꿀 수 없다.

상시 알림도 이제 같이 바뀐다: `프라이빗 — N분 남음 (기록 안 함)`.
**프라이빗이 워처 침묵 경고보다 위**다 — 프라이빗 중엔 워처가 멈춘 것이 정상이라
침묵 경고가 뜨면 헛경보다.

### ★ 전역이다. 기기별이 아니다

`private_span` 에 `device` 컬럼이 있지만 *"켠 기기 이름"* 이라는 기록용이고, 조회
(`load_spans`)는 `WHERE revoked = 0` 뿐이라 기기를 안 본다. **폰 타일로 켜면 젯슨 저장
관문이 PC 이벤트도 안 받는다.** 명세가 *"지금 이 시간대를 통째로"* 로 정의하므로 의도된
설계로 보이지만 **`private-mode.md` 어디에도 "전역"이라고 적힌 문장이 없다** — 코드를
봐야 안다.

| 켠 곳 | 폰이 멈추기까지 |
|---|---|
| 폰 타일 | **즉시** (로컬을 먼저 뒤집는다) |
| 젯슨 웹 | **최대 15초** (`poll_sec`, 젯슨이 응답에 실어 보낸다) |

★ **PC 헬퍼는 문서만 있고 실물이 없다** (`docs/private-mode-pc.md`). PC 는 로컬 AW 에
계속 쌓지만 젯슨 DB 에는 안 들어간다 — 저장 관문에서 막힌다.

### 만들면서 다섯 번 틀렸다 — 전부 실기기에서만 드러났다

1. **젯슨의 종료 키가 `end` 가 아니라 `off`** — 명세 문서가 틀렸고, 코드는 `body.get("off")`
   만 봐서 **`off` 가 없으면 전부 "켜기"로** 떨어졌다. 끄려던 것이 매번 새 60분 켜기가 됐다
2. **껐는데 폴링이 되살렸다** — 젯슨에 남은 "켜짐"을 `max` 규칙으로 다시 켰다
   → `pendingEnd` 로 서버 확인 때까지 서버 상태를 무시하고 종료를 재통지한다
3. **젯슨 웹에서 끄면 폰이 안 따라왔다** → 직전에 본 서버 상태와 비교해 "누가 껐다"를 판별
4. **커서 바닥값과 정리기가 서로를 무너뜨렸다** — 정리기가 청소 후 구간을 지우자 다음
   수집이 커서를 되돌렸다 → **단조 증가 바닥값**을 따로 뒀다
5. ★ **가드가 안 도는 경로에 있었다** — `UsageStatsWatcher.SendHeartbeatsTask` 는 세션
   모드에서 실행되지 않는다. 실사용 경로는 `SessionEventWatcher` 였고, **로그가 한 줄도
   안 찍히는 것으로** 드러났다

### 검증

```
16:18:18  프라이빗 ON (타일)
(19분 동안 크롬 2페이지 · SNS 앱 사용)
16:36:03  프라이빗 — 이번 수집을 건너뛴다      앱 세션 수집 차단
          웹 이벤트 0건 · WebWatcher 로그 0건
16:37:10  젯슨에서 껐다 — 폰도 푼다
16:37:10  지연분 정리: 5건 지움
16:37:26  종료 재통지 성공 → 젯슨이 종료를 확인했다

프라이빗 구간 안의 앱 세션: 0건   (마지막 세션 16:18:12 — 시작 6초 전)
끄고 나서도 되돌아오지 않았다
```

---

## 참고

### 상시 알림을 스와이프로 없애면 추적이 멈추나

**안 멈춘다.** 알림은 포그라운드 서비스의 표시일 뿐이고 지운다고 서비스가 죽지 않는다.
게다가 시스템이 `NO_CLEAR` 를 붙여 놨다 (`originalFlags` 에는 없다 — 시스템이 더한 것):

```
flags = ONGOING_EVENT|NO_CLEAR|FOREGROUND_SERVICE|SILENT
isForeground=true  foregroundId=1
```

★ **실제로 스와이프해 보지는 못했다** — adb 로 알림을 스와이프하는 방법이 없다. 미확인.

### 젯슨 쪽 변경 이력 (되돌릴 때)

| 파일 | 변경 | 백업 |
|---|---|---|
| `config/lifetrainer.toml` | `[ingest]` 섹션 추가 | `.bak-before-ingest` |
| `config/rules.yaml` | 안드로이드 규칙 9개 + `browser_apps` 에 안드로이드 패키지 | `.bak-before-android` |
| `data/lifetrainer.db` | 09-03 폰 전량 재import | `.bak-before-repair-20260903` |
| `~/.config/systemd/user/cloudflared-lt.service` | 신규 | — |

★ `rules.yaml` 의 `browser_apps` 는 폰 `WebWatcher.KNOWN_BROWSER_PACKAGES` 와 **같이
유지해야 한다.** 한쪽만 늘리면 URL 이 다시 조용히 사라진다.

**젯슨 작업 규칙**: 버그는 `HISTORY/`(**고친 코드가 아니라 깨진 가정을 쓴다**), 기능은
`docs/progress/`, 안 고친 것만 `docs/issues/`(고치면 파일을 **지운다**).
테스트는 `cd life-trainer && .venv/bin/python -m pytest tests/ -q` — **이 디렉터리에서만**
통과한다. 재시작은 `systemctl --user restart lifetrainer-web` (`create_app` 이 설정을
클로저에 못박는다). `make check-docs` 가 운영 수치를 문서에 적으면 실패시킨다.
**DB 를 건드리기 전에 백업.**

### ★ 비밀 취급

`c:\jetsonapk\keys\` 는 **저장소에 올리지 않는다** (`.gitignore` 가 `*` 로 막는다).

| 파일 | 잃으면 |
|---|---|
| `lifetrainer-release.jks` + `keystore-credentials.properties` | **복구 불가.** 앱을 영원히 업데이트 못 하고, 재설치하면 폰 기록이 날아간다 — 오프라인 사본을 둘 것 |
| `ingest-secret.txt` | 복구 가능. 젯슨에서 `data/ingestsecret` 지우고 재시작 |

플래너 UI·대시보드는 창 제목이 들어 있어 **테일넷 전용**으로 둔다.
터널은 `/ingest/` 만 뚫려 있다.

### 빌드 (WSL2 Ubuntu)

```bash
wsl -d Ubuntu
. ~/.lt-build-env && cd "$LT_REPO"
./gradlew testReleaseUnitTest assembleRelease
```

★ **`mobile/src/main/jniLibs` 심볼릭 링크가 사라져 있을 수 있다.** 실체
(`~/.lt-build/jniLibs`)는 멀쩡하고 `.so` 는 Makefile 이 하드링크하므로, 링크만 다시 걸면
`make` 를 다시 안 돌려도 된다:

```bash
ln -s "$HOME/.lt-build/jniLibs" mobile/src/main/jniLibs
python3 scripts/check-jnilibs.py    # ELF·16KB 정렬 검증
```

★ Git Bash 에서 `adb`/`wsl.exe` 를 부르면 MSYS 가 `/sdcard/…` 를 Windows 경로로 바꾼다.
`export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'` 를 먼저 한다.

★ Kotlin 은 **블록 주석이 중첩된다.** KDoc 안에 슬래시·별표 조합을 쓰면 거기서 주석이
새로 열려 파일 끝까지 먹는다 (`Unclosed comment`).
