# 남은 이슈 해결 계획

## Context

2026-09-03 에 폰(LT Phone) 쪽에서 데이터가 새는 곳을 여럿 잡아 고쳤다
(`aef585d` 커서 절단 · `57b35e0` 미디어 워처 자가복구 · `ecc2545` 쇼츠 ·
`908af6f`·`10503a4` 프라이빗 모드). 그 과정에서 **폰을 고쳐도 안 되는 것** 다섯 건이
드러나 `c:\jetsonapk\for_jetson\` 에 이슈로 적어 뒀다. 이 계획은 그것들을 닫는다.

**조사가 두 가지를 바꿨다.** 다섯 건 중 하나는 고치면 안 되는 것이었고, 적어 두지
않은 더 큰 것이 하나 나왔다. §0 이 그 내용이다.

---

## 0. 조사가 바꾼 것 — 먼저 읽을 것

### (a) ★ `bucket_type()` 에 `media` 분기를 넣으면 **안 된다** — 원 계획 취소

`for_jetson/h-0028` 은 "젯슨에 `if "media" in bid: return "media"` 한 줄이면 된다"고
적었다. **틀렸다.** 세 가지가 걸린다:

1. **그 부재를 지키는 테스트가 이미 둘 있다.** `tests/test_aw_sync.py` 의
   `test_bucket_type_android_media_is_android` 는 근거까지 적어 뒀다 — *"미디어 재생
   구간은 앱 세션과 겹치므로 `_union` 이 흡수한다. 실측으로 이중 계산이 없음을 확인했다."*
2. **`device_kind_for()` 가 `media` 를 모른다.** `_PHONE_BUCKET_TYPES = {"android","unlock"}`
   이라 `"media"` 는 **`laptop` 으로 떨어진다** — 폰이 노트북으로 등록될 수 있다.
3. **롤업은 이미 갈라내고 있다.** `rollup._bucket_kind` 가 `-media` 접미사로 나누고,
   docstring 이 그 판단 근거를 적어 뒀다.

→ **`w-0028` (watched) 로 내린다.** *"고칠 수 있지만 재 보니 지금이 낫다"* — `w-` 의
정의 그대로다. 대신 `android/docs/phone-titles.md` §F4-b 의 **잘못된 지시**만 고친다.

### (b) ★ 적어 두지 않은 더 큰 것 — 폰 브라우징 URL 이 슬롯에 하나도 안 붙는다

`config/rules.yaml` 의 `browser_apps` 가 **데스크톱 전용**이다 (`chrome.exe`, `firefox`,
`safari` … — 안드로이드 패키지가 없다). `rollup.py:566` 이
`if classifier.is_browser(app): url = _best_web_url(...)` 라 `com.android.chrome` 은
게이트를 못 넘는다. 익명화한 사본에서 웹 이벤트에는 URL이 있었지만 폰 브라우저
슬롯은 앱 이름만으로 뭉뚱그려져 있었다. 개인별 이벤트·슬롯 수는 공개본에서 제거했다.

★ 이전에 고쳤다고 적혀 있는 증상과 같다. `bucket_type` 에서 `web` 을 앞으로 옮겨
폰 브라우저 기록이 앱 이름만으로 뭉뚱그려지던 문제를 고쳤다는데,
**그건 절반이었다** — 이벤트가 `dev.buckets["web"]` 까지는 왔지만 `is_browser` 에서 막힌다.

제목보다 값이 크다. **§2 로 올려 제목 작업(§3)보다 먼저 한다.**

---

## 작업 순서

각 항목은 **독립적으로 커밋·검증 가능**하다. 순서는 위험이 낮고 검증이 쉬운 것부터다.

| # | 무엇 | 어디 | 왜 이 순서 |
|---|---|---|---|
| 1 | `/ingest/private` 본문 해석 | 젯슨 | 폰이 이미 맞춰져 있어 즉시 검증된다 |
| 2 | `browser_apps` 에 안드로이드 브라우저 | 젯슨 | 설정 한 곳. 값이 가장 크다 |
| 3 | `_title_from_web` (웹 제목 → 슬롯) | 젯슨 | 2번이 URL 을 살린 뒤라야 효과를 눈으로 본다 |
| 4 | `lt doctor` 버킷별 침묵 검사 | 젯슨 | 5번이 만드는 상태를 젯슨에서도 보게 된다 |
| 5 | 폰이 스스로 워처 침묵을 알아챈다 | 폰 | 빌드·설치·권한 토글이 필요해 가장 무겁다 |

**착수 전 `git pull`.** 젯슨 저장소는 다른 세션이 활발히 작업 중이다 (오늘 `e5bf944` 가
`aw_sync.py`, `d2cfa64` 가 `cli.py` 를 건드렸다). 이슈 번호도 이미 `0027` 까지 찼다.

---

## 1. `/ingest/private` 이 모르는 본문을 "켜기"로 읽는다

**파일**: `lifetrainer/web/app.py` (`ingest_private_set`, 1310~1339행) ·
`android/docs/private-mode.md` §3

```python
if body.get("off"): st = privacy.end_now(conn)
else:               st = privacy.begin(conn, _parse_minutes(body, ...), source="tile", ...)
```

`else` 가 **모든 나머지**를 받고 `_parse_minutes` 가 `minutes` 없으면 기본값 60을 쓴다.
그래서 `{}` · `{"off": false}` · `{"end": true}` · 오타 전부가 **60분 켜기**가 된다.
게다가 `privacy.begin` 은 이미 켜져 있으면 `end_ts` 를 `max()` 로 **연장**한다 —
끄려는 요청이 구간을 늘린다.

★ **`/api/private` 은 이 문제가 없다** — 켜기는 `POST`, 끄기는 `DELETE` 로 **동사**가
가른다. 모호함은 둘을 POST 하나에 몰아넣은 `/ingest/private` 에만 있다.

**할 일**
- `off` 와 `end` 를 **둘 다** 받는다 (이미 나간 폰 클라이언트를 안 깬다)
- **모르는 본문은 400.** `minutes` 가 명시됐거나 본문이 완전히 빈 경우만 `begin`.
  형태는 같은 파일의 `api_private_purge` 가 이미 보여 준다 — *"요청이 무엇을 하려는지
  **몸통이 말하게** 한다"*
- 문서의 `{"end": true}` → `{"off": true}`

**검증** (지금 이 경로에는 **테스트가 하나도 없다** — POST 하는 테스트가 전무하다)
- `tests/test_private.py` §5 에 서명 POST 헬퍼를 추가한다. 베낄 것은
  `tests/test_web_ingest.py:61-72` 의 `_post()`
- `off`·`end` 둘 다 꺼진다 / `{"typo":1}` 은 **400** / 빈 본문은 기존대로 켜기
- 실기기: 폰 타일로 껐을 때 젯슨이 실제로 꺼진다

---

## 2. `browser_apps` 에 안드로이드 브라우저를 넣는다

**파일**: `config/rules.yaml` (`browser_apps`)

`aw_event.data_json` 의 `browser` 값으로 **실제로 나온 패키지만** 넣는다
(확인된 것: `com.android.chrome`).

★ **`is_browser` 의 다른 쓰임을 먼저 본다.** 브라우저로 인정되면 URL 규칙이 앱 규칙보다
먼저 평가되므로 **기존 분류가 바뀐다.** 그게 목적이지만, 무엇이 어디로 옮겨갔는지는
눈으로 확인해야 한다.

**검증**
- 재롤업 **전후로 `slot_breakdown` 의 카테고리별 합**을 비교한다
- 폰 크롬 슬롯이 `browsing`/`away` 두 덩어리에서 URL 규칙대로 갈린다
- ★ **활동 시간 총합은 변하지 않아야 한다** — 분류만 바뀌는 변경이다

---

## 3. 웹 페이지 제목이 슬롯까지 오지 않는다

**파일**: `lifetrainer/rollup/rollup.py`

`_arbitrate_devices`(418행)의 486~490행에서 `dev.buckets["web"]` 은 `web_out` 으로만
가고, `contribs`(575·586행)에는 **window 이벤트만** 들어간다. `top_title` 은 그
`contribs` 에서 뽑히므로(835~841행) 웹 제목은 닿을 길이 없다.

`_title_from_media`(176행)와 대칭인 `_title_from_web` 을 만들어 같은 자리에 끼운다.

★ **`_title_from_media` 를 그대로 베끼면 안 된다.** 웹 이벤트는 **`app` 컬럼이 NULL**
이고 소유 앱이 `data_json["browser"]` 에만 있다 (미디어는 `app` 이 채워져 있다).
→ 롤업 질의(96~105행)가 `data_json` 을 뽑는지 **먼저 확인**하고, 안 뽑으면 매칭 키를
어디서 얻을지부터 정한다.

★ **미디어와 웹이 같은 세션을 둘 다 덮을 수 있다** (크롬으로 유튜브). 어느 쪽이 이길지는
**실데이터로 재고 정한다** — 지금은 근거가 없다.

**검증**
- `slot.top_title` 이 페이지 제목이 된다
- ★ **활동 시간이 변하지 않는다.** `slot_breakdown` 에는 title 컬럼이 없어 이 변경은
  **시각화 층만** 건드린다 (CLAUDE.md: *"집계 원천은 `slot_breakdown`"*)
- 노트북 슬롯의 `top_title` 이 하나도 안 변한다
- 테스트는 `tests/test_rollup_device.py` 의 미디어 제목 3종(352~397행) 옆에 붙인다

---

## 4. `lt doctor` 가 버킷별 침묵을 못 본다

**파일**: `lifetrainer/cli.py` (`cmd_doctor`, "롤업 신선도" 검사 옆)

웹과 미디어 워처가 장시간 멈춘 것을 아무도 못 봤다. 기존 "롤업 신선도"는 *"우리 쪽
파이프가 도나"* 를 보는데 이번 고장은 **파이프는 도는데 원료가 끊긴 것**이다.

★ **`aw_bucket.last_seen` 은 못 쓴다.** `upsert_bucket` 이 **이벤트가 없어도** 매 sync
마다 `now` 로 갱신한다 — 죽은 워처의 버킷도 계속 신선해 보인다.
→ `MAX(ts_end)` 를 버킷별로 본다. `idx_aw_event_bkt_end(bucket_id, ts_end)` 가 이미 있어
색인으로 덮인다.

### ★ 임계값은 깨끗한 사본 데이터로 정한다 (CLAUDE.md §1)

판정은 **세션 버킷이 신선한데 대상 버킷만 충분히 뒤처진 경우**다. 밤에는 모든 버킷이
같이 조용해져 조건이 성립하지 않는다. 공개본에는 개인별 표본 수·날짜별 발동 횟수·
임계값 산출표를 싣지 않는다. 웹은 정상 관측 구간이 충분히 모인 뒤 정한다.

### 안 울려야 하는 경우 (테스트로 같이 둔다)

- 밤새 폰을 안 씀 / 폰을 꺼 둠 → 세션 버킷도 같이 조용 → **안 울린다**
- 며칠 아무것도 안 틂 → 미디어만 조용, 세션은 옴 → 정상 사용이면 안 울린다
- 프라이빗 모드 → 세션·웹·미디어가 **같이** 끊긴다 → 안 울린다
- **이벤트가 아예 없는 새 DB → OK 지 WARN 이 아니다.**
  기존 `test_한_번도_롤업_안_한_DB_는_실패가_아니다` 와 같은 이유 —
  *"옳지만 아직 증명 못 한 상태를 실패로 세지 않는다"*

**테스트**: `tests/test_cli.py` 의 `_doctor_line(cfg, label, capsys)` 헬퍼와
`_cfg_with_slot` 패턴을 그대로 쓴다 (롤업 신선도 3종이 직접적인 본보기).

---

## 5. 폰이 스스로 워처 침묵을 알아챈다

**파일**: `BackgroundService.kt` (스케줄러 하나 추가) · 새 `watcher/WatcherHealth.kt`

앱을 업데이트하면 접근성·알림 접근이 **목록에는 남은 채 다시 바인딩되지 않는다**
(오늘 6/6 재현). 설정 화면에는 "켜짐"으로 보여 사람이 봐도 모른다.

### 조사로 정해진 제약

- **권한 API 로는 못 잡는다.** `isNotificationAccessGranted` 는 `Settings.Secure` 목록
  문자열 일치를, `isAccessibilityAllowed` 는 `getEnabledAccessibilityServiceList` 를 볼 뿐 —
  **둘 다 "목록에 있나"** 이고 이 사고는 *목록엔 있는데 안 붙은* 상태다
  → **권한이 아니라 결과(버킷)를 본다**
- **버킷 메타의 `last_updated` 는 항상 `null`** (서버가 하드코딩). 저장소의 기존 관행인
  `getEventsJSON(bucketId, limit = 1)` 을 쓴다. `InteractionWatcher.lastEventEnd()`
  (119~134행)가 가장 깔끔한 본보기다
- ★ **`getEventsJSON` 기본값 `limit = 0` 은 `SQL LIMIT 0` 이라 0건이 온다.** 반드시 명시
- **알림은 상시 알림(foreground service)의 문구를 바꾸는 쪽으로 한다.**
  `POST_NOTIFICATIONS` 는 **한 번만 요청하고 다시 안 묻는다** — 거절한 사용자에게는 새
  알림이 안 간다. 상시 알림은 권한이 필요 없고 사용자가 없앨 수도 없으며, 프라이빗
  타일과 같은 자리(알림 그늘)에 보인다.
  → *"위젯·알림 상주를 두 곳에 만들지 않는다"*(private-mode.md §5)와도 맞는다
- **판정 로직은 순수 함수로 뽑는다.** JVM 테스트에 Robolectric 이 없어 `Context` 를 받는
  것은 테스트할 수 없다. `(세션 마지막 ts, 대상 마지막 ts, now) → 경고 여부` 만 분리하면
  `BrowserSessionTrackerTest` 처럼 단위 테스트가 된다
- 임계값은 **§4 와 같은 숫자**를 쓴다 — 두 곳이 다른 답을 내면 안 된다

---

## 6. 며칠 뒤 재측정 (지금 하지 않는다 — 문서만 남긴다)

기존 기준선은 개인 시청 기록과 테스트 사용이 섞인 날짜별 값이라 공개본에서 제거했다.
워처가 안정된 이후 구간만 로컬에서 다시 재고, 같은 시점에 §4의 웹 임계값도 정한다.

---

## 젯슨 저장소 규칙 (반드시 지킬 것)

- **버그 수정** → `HISTORY/YYYY-MM-DD-슬러그.md`. *"고친 코드가 아니라 **깨진 가정**을 쓴다"*
  → §1(문서를 믿었다) · §0b(한 번 고쳤으니 됐다고 믿었다) · §5(권한은 켜면 켜져 있다)
- **기능 추가** → `docs/progress/YYYY-MM-DD.md` → §3 · §4
- **고치는 것은 `docs/issues/` 에 새로 만들지 않는다** (*"고치면 파일을 지운다"* ·
  *"이슈였던 적 없는 포스트모템이 있다"*). 파일로 남기는 것은 **`w-0028` 하나**뿐이다
  — 다음 빈 번호가 `0028` 이다 (내 `for_jetson` 번호 0027~0031 은 이미 충돌한다)
- **`make check-docs` 가 운영 수치를 문서에 적으면 실패시킨다.** 테스트 개수·이벤트 수를
  README·핸드북에 쓰지 않는다 (`docs/progress/`·`HISTORY/`·`issues/` 는 예외)
- 테스트: `cd life-trainer && .venv/bin/python -m pytest tests/ -q`
  — **이 디렉터리에서만 통과한다** (`p-0017`)
- 재시작: 편집 설치라 코드는 바로 먹지만 **`systemctl --user restart lifetrainer-web`** 필요
  (`create_app` 이 설정을 클로저에 못박는다). 롤업은 `.venv/bin/lt rollup --range …`
- **DB 를 건드리기 전에 백업**

---

## 검증 (전체를 통과해야 끝난 것으로 본다)

1. `cd life-trainer && .venv/bin/python -m pytest tests/ -q` 통과
2. `make check-fast` 통과 (링크·문서·shellcheck)
3. `.venv/bin/lt doctor` — 새 검사가 **정상 상태에서 조용하다**
4. 재롤업 전후 **하루 합이 24시간을 유지**하고 **활동 시간 총합이 안 변한다**
5. 실기기: 폰 타일로 켜고 끄면 젯슨이 따라오고, 워처가 재개된다
6. `.venv/bin/lt timeline --day $(date +%F)` — **PNG 를 실제로 연다**
   (CLAUDE.md 반복 실패 4번: *"테스트 통과 ≠ 동작"*)

---

## 산출물

- **`for_jetson/PLAN.md`** — 이 계획서 (요청하신 것)
- `for_jetson/` 정리 — 고친 것은 지우고, `h-0028` 은 `w-0028` 로 바꿔 젯슨에 옮긴다
- 젯슨: 작은 커밋 4~5개 + `HISTORY/` 3건 + `docs/progress/` 1건
- 폰(`lt-phone`): 커밋 1개
- `c:\jetsonapk\STATUS.md` 갱신
