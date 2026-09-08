# 폰 사용 기록 — 실행 순서

> 작성 2026-08-19 / 젯슨 쪽(수신·롤업)은 [08-19 진행 기록](../../docs/progress/2026-08-19.md) 에서 끝났다.
> 여기는 **사람이 손으로 해야 하는 것**을 순서대로 적은 것이다.
> 설계와 근거는 [plan.md](plan.md), 앱 조사는 [research.md](research.md).

**게이트가 3개 있다** (③ ⑥ ⑦). 거기서 멈추고 결과를 보고 다음을 정한다.
앞 단계를 건너뛰고 뒤를 하면 안 되는 이유도 각 단계에 적었다.

---

## ★ 지금 어디에 서 있나 (2026-08-21)

**게이트 ①(③) 과 ②(⑥) 는 닫혔다. 답은 "포크가 필요하다" 였다.**

```
③ 게이트 1   ✅ a·b·c·d 전부 확정 (a·b·c 는 master 소스, d 는 실기기)
⑥ 게이트 2   ✅ 포크 범위 확정 — F0~F3 (§⑥ 참조)
⑦ 게이트 3   ⏳ 도메인 미정 (Cloudflare). 포크보다 뒤에 와도 된다
```

**REST 로 표본을 뽑는 경로는 막혔다** (API 키 화면이 공개 APK 에 없다 · 앱 내 Export 는
크래시). 그런데 **그 지름길은 원래 "포크를 뜰지 정하려고" 있던 것**이고, 실물이
"필요하다"고 답해버려서 값어치가 없어졌다.

> **자동 수신은 젯슨 쪽에 이미 다 있다** — `POST /ingest/aw`(서명·재전송 방지) ·
> `lt import` · 롤업 기기 차원·중재. **비어 있는 건 폰이 밀어주는 쪽뿐이고,
> 그건 포크 없이는 어차피 안 된다:**
>
> | 필요한 것 | 포크 없이 되나 |
> |---|---|
> | 5분 수집 | ❌ `UsageStatsWatcher` 의 `INTERVAL_HOUR` 를 고쳐야 한다 |
> | 5분 전송 | ❌ `PushWorker` 를 새로 넣어야 한다 |
> | 화면·터치 판정 | ❌ `InteractionWatcher` 를 새로 넣어야 한다 |
> | Export 크래시 | ❌ `DownloadListener` 를 붙여야 한다 |

**다음 할 일은 ⑨(포크 빌드)다.** ①②는 이미 끝났고, ③~⑤는 포크가 밀기 시작하면
자연스럽게 검증된다.

---

## ① 폰: 앱 설치 (15분)

### 어느 빌드인가 — **먼저 정하고 시작한다**

**v0.14.0dev 를 쓴다** (2026-08-19 결정).

| | v0.12.1 (F-Droid·Play 안정판, 2023-10) | **v0.14.0dev** (GitHub 프리릴리스, 2026-07) |
|---|---|---|
| 서비스 | `ChromeWatcher`(접근성) **하나뿐** | `BackgroundService`(**foreground**) + WebWatcher + MediaWatcher |
| `FOREGROUND_SERVICE` | **없음** → 알람이 Doze 에서 잘 밀린다 | 있음 |
| `POST_NOTIFICATIONS` | **없음** → 알림 권한 토글이 아예 안 뜬다 | 있음 |
| 이 계획이 읽은 코드 | ✗ 다른 앱에 가깝다 | ✓ 거의 일치 |

★ **F-Droid 빌드와 GitHub 빌드는 서명 키가 다르다.** 덮어쓰기 설치가 안 되므로 갈아타려면
**지우고 다시 깔아야 하고, 그때까지 쌓인 기록이 날아간다.** 그래서 데이터를 모으기 **전에**
정한다.

### 설치

이미 F-Droid/Play 판이 깔려 있으면 **먼저 지운다** (서명 충돌로 덮어쓰기가 안 된다).

APK: `https://github.com/ActivityWatch/aw-android/releases/download/v0.14.0dev20260723/aw-android.apk`
(180 MB 유니버설. 최신 dev 태그는 [releases](https://github.com/ActivityWatch/aw-android/releases) 에서 확인)

- **폰 브라우저로 직접 받는 경우**: 설치 시 "출처를 알 수 없는 앱" 허용을 물어본다
- **adb 로 넣는 경우**(③의 adb 를 먼저 깔았다면 이쪽이 깔끔하다):
  ```bash
  adb install -r aw-android.apk
  ```

> dev 빌드다. 이상하면 v0.12.1 로 되돌릴 수 있지만 그때도 **데이터는 날아간다**.

---

## ② 폰: 권한 (5분)

**켜야 하는 것:**

| 무엇 | 어디 | 왜 |
|---|---|---|
| **사용 정보 접근** | 설정 → 앱 → (⋮) **특별한 접근** → 사용 정보 접근 → ActivityWatch 허용 | ★ 이게 없으면 **아무것도 안 쌓인다.** 앱 첫 실행 때 이 화면으로 보내주기도 한다 |
| **알림 허용** | 첫 실행 팝업 (Android 13+) | v0.14.0dev 는 **foreground service + 상주 알림**으로 돈다. 막으면 서비스가 죽는다. **v0.12.1 에는 이 권한이 없어서 토글이 안 뜬다 — 그건 정상이다** |
| **배터리 제한 없음** | 설정 → 앱 → ActivityWatch → **배터리** → "제한 없음" | 삼성 스킨은 백그라운드 앱을 공격적으로 재운다. 재워지면 **데이터가 조용히 빈다** |

삼성은 **설정 → 배터리 → 백그라운드 사용 제한**의 "절전 앱 / 딥 슬리핑 앱" 목록에
ActivityWatch 가 들어가 있지 않은지도 확인한다. 들어가 있으면 뺀다.

> 메뉴 이름은 One UI 버전마다 다르다. 못 찾으면 설정 검색에 **"사용 정보"**,
> **"배터리 최적화"**, **"백그라운드 사용 제한"** 을 넣는다.

**주지 말아야 할 것 2개** — 앱이 물어봐도 거절한다:

- **접근성 권한** — `WebWatcher`(AccessibilityService)가 **브라우저 주소를 읽는다.**
  안 켜도 앱 사용 기록은 전부 잡힌다
- **알림 접근**(Notification Listener) — `MediaWatcher` 용. 우리 목적에 필요 없다

**확인**: 앱을 열어 오늘 사용한 앱이 목록에 뜨면 성공.

---

## ②-b ★ 지금 바로 확인 (5분) — 하루를 버리지 않기 위해

**하루 기다리기 전에 5분만 써서 "정말 쌓이고 있는지" 본다.** 권한이 안 먹었는데 하루를
기다리면 그 하루가 통째로 날아간다.

폰에서 먼저:

| 무엇 | 어디 |
|---|---|
| **개발자 옵션** | 설정 → 휴대전화 정보 → 소프트웨어 정보 → **빌드번호를 7번 탭** |
| **USB 디버깅** | 설정 → 개발자 옵션 → USB 디버깅 켜기 |

USB 로 연결하면 폰에 **"USB 디버깅을 허용하시겠습니까"** 팝업이 뜬다. 허용한다.

```bash
adb devices                                   # 'device' 로 보여야 한다
adb shell dumpsys package net.activitywatch.android | grep -m1 versionName   # 0.14.0dev 인지
adb forward tcp:5610 tcp:5600
curl -s http://127.0.0.1:5610/api/0/buckets/ | python3 -m json.tool
```

> ★ **젯슨 쪽 로컬 포트는 `5610` 이다 — `5600` 으로 되돌리지 마라.**
> 젯슨에서 `tcp:5600` 을 열면 VS Code Remote-SSH 가 그것을 윈도우 PC 의
> `127.0.0.1:5600` 으로 자동 포워딩하고, 윈도우는 `127.0.0.1` 바인딩을
> aw-server 의 `0.0.0.0` 바인딩보다 우선하므로 **PC 의 ActivityWatch 수집이
> 조용히 멈춘다** (2026-08-21 에 이렇게 9시간을 날렸다).
> **폰 쪽 포트는 그대로 `tcp:5600` 이다** — 바뀌는 것은 젯슨 쪽뿐이다.

| 보이는 것 | 뜻 |
|---|---|
| 버킷이 **하나 이상** 뜬다 | ✅ 서버가 살아 있고 워처가 등록됐다. 하루 두면 된다 |
| 버킷 목록이 **비어 있다** | 워처가 아직 한 번도 안 돌았다. 앱을 열어 화면을 한 번 보고 몇 분 뒤 다시 |
| curl 이 **연결 거부** | aw-server 가 안 떴다. 앱을 열어 두고 다시. 그래도 안 되면 알림·배터리 설정을 다시 본다 |
| `adb devices` 가 **unauthorized** | 폰의 허용 팝업을 못 누른 것. 케이블을 뽑았다 꽂으면 다시 뜬다 |

이때 버킷 ID 를 미리 봐두면 ③의 확인 **a**(`aw-watcher-android` 가 맞는가)를 지금 끝낼 수 있다.

---

## ②-c 하루 기다린다

한두 시간짜리 표본으로는 아무 판단도 못 한다. 기다리는 동안 ③의 준비(adb)를 하면 된다.

**수집은 하루 한 번이 아니다.** `UsageStatsWatcher` 가 `AlarmManager.INTERVAL_HOUR` 로
**1시간마다** 돌고, `EventParsingWorker`(24시간, 자정 시작)는 놓친 것을 메우는 백스톱이다.

그리고 **주기가 정확도를 좌우하지 않는다.** 안드로이드 OS 가 사용 통계를 자체적으로 계속
기록하고 있고, 워처는 `queryEvents(마지막으로_읽은_시점, MAX)` 로 **커서를 들고 따라잡는다.**
하루에 한 번 읽어도 그 안에 초 단위 타임스탬프가 다 들어 있다 — 노트북 쪽 `aw_sync` 와
같은 구조다. 늦는 것은 **도착**이지 기록이 아니다.

---

## ③ ★ 게이트 1 — 실물 표본 뽑기 (15분)

**폰이 aw-server 를 `127.0.0.1` 에만 열기 때문에** 밖에서 그냥 못 붙는다. adb 로 포트를
넘겨서 뽑는다. 이 단계에는 **Cloudflare 도 포크도 필요 없다.**

```bash
sudo apt install adb          # Ubuntu 22.04 arm64 의 패키지 이름은 `adb` (android-tools-adb 아님)
```

### ★ USB 가 안 잡히면 — **웜 재부팅이 아니라 전원을 껐다 켠다** (2026-08-19 실측)

폰을 꽂아도 `lsusb` 에 안 보이고 커널 로그에 이것이 반복되면:

```
usb 1-2: new full-speed USB device number 4 using tegra-xusb
usb 1-2: device not accepting address 4, error -71
usb usb1-port2: attempt power cycle
tegra-xusb 3610000.usb: Firmware timestamp: ...     ← 컨트롤러 리셋
```

`usb 1-2` 는 **온보드 VIA USB 2.0 허브**다. 이게 열거에 실패하면 그 아래 USB 2.0 기기가
전부 안 보인다 — 폰뿐 아니라 **마우스 동글·Wi-Fi 어댑터까지** 같이 사라진다.
(`lsusb` 에 `VIA Labs USB3.1 Hub` 만 있고 `USB2.0 Hub` 가 없으면 이 상태다.)

**`sudo reboot` 으로는 안 고쳐진다.** 웜 리셋은 허브 칩의 전원 레일(`VDD_AV10_HUB`)을
끊지 않아서 칩이 걸린 상태 그대로 올라온다. **전원을 완전히 뽑았다 켜야(콜드 부팅)**
칩이 리셋된다. 실측으로 콜드 부팅 한 번에 복구됐다.

**해보고 소용없었던 것 (다시 하지 말 것):**

- `usbcore.old_scheme_first=1` / `initial_descriptor_timeout` 조정 — 효과 없었다.
  `-71` 은 열거 방식 문제가 아니었다. **`extlinux.conf` 를 고칠 필요 없다**
- `tegra-xusb` **unbind/bind** — `bind` 가 `Input/output error` 로 실패해 USB 가 통째로
  내려갔고 재부팅으로만 복구됐다. **하지 말 것**
- `journalctl --since "$(uptime -s)"` 로 오류를 세는 것 — 부팅 시 RTC 가 어긋났다가 NTP 로
  교정되면 시각 필터가 빗나가 **0건으로 잘못 나온다.** `journalctl -k -b` 로 세라

> **USB-C 포트는 어차피 안 된다** — `nv-l4t-usb-device-mode` 가 활성이라 젯슨이 그 포트를
> **가젯(장치) 모드**로 쓴다. 호스트가 아니므로 폰을 인식할 수 없다. **Type-A 에 꽂는다.**

### 그래도 안 되면 — 무선 디버깅

USB 스택을 거치지 않으므로 위 문제와 무관하다. 폰과 젯슨이 같은 Wi-Fi 에 있으면 된다
(상시 경로는 어차피 셀룰러+Cloudflare 라 무관하다).

폰: 설정 → 개발자 옵션 → **무선 디버깅** → **페어링 코드로 기기 페어링**

```bash
adb pair <폰IP>:<페어링포트>      # 화면의 6자리 코드 입력
adb connect <폰IP>:<디버깅포트>    # 무선 디버깅 메인 화면의 포트 (페어링 포트와 다르다)
adb devices
```

### ★★ REST 는 막혔다 — 키를 얻을 방법이 없다 (2026-08-21 실측, 막다른 길)

```
$ curl -s http://127.0.0.1:5600/api/0/buckets/
{"message": "Missing or invalid API key. Set 'Authorization: Bearer <key>' header."}
```

**앱에서 API 키 화면을 찾지 마라. 없다.**

키 설정 화면(`AuthSettingsActivity`)은 **master 에만 있고 공개된 APK 에는 안 들어 있다.**
`v0.14.0dev20260723` 이 공개된 최신 APK 가 맞고, master 는 그보다 18커밋 앞서 있다
(이미 `0.14.0b1`/versionCode 39). **더 최신 APK 로 갈아타서 풀 수 있는 문제가 아니다.**

> 계획을 세울 때 읽은 것이 master 소스였고, 폰에 깔린 것은 릴리스 APK 였다.
> **이 프로젝트 반복 실패 1번("조사 문서를 사실로 믿었다")과 같은 형태다** —
> 이번엔 "소스에 있으니 앱에도 있다"고 믿었다.

**시도했고 전부 막힌 것 (다시 하지 말 것):**

| 방법 | 결과 |
|---|---|
| `adb shell run-as` 로 앱 내부 파일 | `package not debuggable` — 릴리스 빌드 |
| 외부 저장소 (`/sdcard/Android/data/...`) | 키 없음 |
| `logcat` 에서 키 문자열 | 안 찍힌다 |
| `Authorization` 헤더 위장 | 재현 불가. 앱이 실제 키를 주입한다 |
| WebView 디버깅 (`webview_devtools_remote`) | 소켓 없음 (앱 WebView 는 디버깅 꺼짐) |
| **앱 내 Export 버튼** | **앱이 죽는다** — 아래 참조 |

### 앱 내 Export 도 막혔다 — 크래시한다

```
android.content.ActivityNotFoundException:
  No Activity found to handle Intent { act=android.intent.action.VIEW dat=blob: }
```

WebView 에 `DownloadListener` 가 없어 `blob:` 다운로드가 외부 인텐트로 넘어가고,
그걸 받을 앱이 없어 죽는다. **업스트림 이슈 #228("Export 가 조용히 실패")이 이 버전에서
크래시로 악화됐다.** 데이터 자체는 만들어졌고(blob 생성 성공) 전달만 실패한다.

→ **포크의 F0 이 정확히 이것이다.** 몇 줄이고, 스택트레이스까지 확보했으니
업스트림 PR 로도 바로 낼 수 있다.

### 인증 없이 되는 것 하나 — `/api/0/info`

```bash
curl -s http://127.0.0.1:5610/api/0/info | python3 -m json.tool
```

```json
{"hostname": "localhost", "version": "v0.14.0 (rust)", "device_id": "1b88de93-…"}
```

★ **`hostname` 이 기기명이 아니라 `localhost` 다.** `lt import` 에서 `--device` 를
**반드시 명시**해야 한다 — 자동 추론에 맡기면 기기 이름이 `localhost` 가 된다.

```bash
KEY=<앱에서 확인한 키>
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:5610/api/0/buckets/ | python3 -m json.tool
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:5610/api/0/export -o ~/phone.json
```

연결된 뒤부터는 유선·무선이 완전히 동일하다:

```bash
adb forward tcp:5610 tcp:5600

curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:5610/api/0/buckets/ | python3 -m json.tool | head -40
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:5610/api/0/export -o ~/phone.json
```

### ✅ 게이트 1 은 2026-08-21 에 닫혔다 — a·b·c·d 전부 확정

**REST 가 막혔는데도 답은 다 나왔다.** a·b·c 는 **master 소스**에서, d 는 실기기에서.

> **릴리스 APK 를 재는 것보다 master 소스가 더 정확하다** — 우리가 빌드할 것이
> 그 소스이기 때문이다. 실행 시점 검증은 포크가 밀기 시작할 때 자연스럽게 된다.

| | 확인된 값 | 근거 |
|---|---|---|
| **a** | 버킷 ID = `aw-watcher-android` · `aw-watcher-android-unlock` | master `SessionEventWatcher.kt` |
| **b** | 타입 = `currentwindow` · `os.lockscreen.unlocks` | 같음 |
| **c** | `data.app` = **앱 이름**("카카오톡") / `data.package` = 패키지명 | master `refs/models/Event.kt` |
| **d** | 화면·터치 이벤트 **실제로 나온다** | 실기기 `dumpsys usagestats` (아래) |

**c 는 우리 코드에 영향이 있었고 이미 고쳤다.** 그대로 뒀으면 분류 규칙이 `"카카오톡"`
같은 **로케일 의존 라벨**에 걸려, 폰 언어를 바꾸거나 앱이 이름을 바꾸면 조용히 안 맞는다.
데스크톱과 같은 배치로 맞췄다 — **`app` = 패키지명(규칙용), `title` = 앱 이름(표시용)**,
`app=Code.exe / title=창 제목` 과 같은 구조다 (`collect/ingest.py`, 테스트 2개).

### 아래는 게이트가 열려 있던 시절의 확인 절차다 (기록용)

**d 확인 방법** — 이건 export 가 아니라 안드로이드 자체에서 본다:

```bash
adb shell dumpsys usagestats | grep -iE "USER_INTERACTION|SCREEN_INTERACTIVE|SCREEN_NON|KEYGUARD" | head -20
```

- ✅ **2026-08-19 실측: 나온다.** S24 기준 하루치에 `SCREEN_INTERACTIVE` 234 ·
  `SCREEN_NON_INTERACTIVE` 234 · `USER_INTERACTION` 192 · `KEYGUARD_SHOWN/HIDDEN` 각 192 ·
  `ACTIVITY_RESUMED/PAUSED` 각 958. 포크의 `InteractionWatcher` 를 `UsageStatsManager`
  만으로 만들 수 있다 — BroadcastReceiver 도 접근성 권한도 필요 없고 **과거분까지 조회된다**.
  ★ `SCREEN_INTERACTIVE`(234) > `USER_INTERACTION`(192) 이라 **"화면 켬 ≠ 사용 중"이
  실제로 구분된다** — "폰 키고 터치" 판정 기준이 의미가 있다
- **안 나오면**: 기기·OEM 이 그 이벤트를 안 주는 것이다. 포크에 `SCREEN_ON/OFF`
  BroadcastReceiver 를 대신 넣는다 (foreground service 에 런타임 등록해야 한다)
- 출력 형식은 안드로이드 버전마다 다르다. grep 이 비면 **원문을 그대로 보고** 판단한다

> **여기서 멈추고 결과를 알려주면** 포크에 무엇이 들어갈지 확정된다.

---

## ④ 젯슨에 넣고 **눈으로** 본다 (10분)

```bash
cd ~/project/project-jetson/life-trainer
.venv/bin/lt import --from ~/phone.json --device phone-example --rollup
```

`--device` 이름은 **한 번 정하면 계속 쓴다.** 나중에 바꾸면 같은 폰이 두 기기로 갈라진다.
이 명령이 `device` 테이블에 폰을 **등록**하는 역할도 한다 — 네트워크 경로(`POST /ingest/aw`)는
등록된 기기만 받으므로, 이 단계를 거쳐야 나중에 상시 전송이 된다.

확인:

```bash
.venv/bin/lt timeline --day 2026-08-XX          # PNG 를 실제로 열어본다
```

- 폰 사용이 **제 시간대에** 찍혔는가
- 노트북 작업 시간이 폰에 먹히지 않았는가 (겹친 구간은 마지막 상호작용이 이긴다)
- 하루 합이 24시간을 넘지 않는가 — 테스트로 고정해 뒀지만 **실데이터로 다시 본다**

> 합성 데이터로 만든 가정 위에서 자기를 검증한 것이
> [08-17 겹침 부풀림](../../HISTORY/2026-08-17-aw-overlap-inflation.md)의 원인이었다.
> 기기 간 겹침은 정확히 같은 부류라 **실데이터로만 확인된다.**

---

## ⑤ 분류 규칙 넣기 (30분)

실측에서 폰 앱이 **전부 미분류**로 나온다 — `config/rules.yaml` 에 안드로이드가 하나도 없다.

```bash
sqlite3 data/lifetrainer.db \
  "select app, seconds_total/60 from unclassified order by 2 desc limit 20"
```

상위부터 `rules.yaml` 에 넣고 재롤업한다 (③-c 에서 본 형태에 맞춰 패키지명 또는 앱 이름으로):

```bash
.venv/bin/lt rollup --day 2026-08-XX
```

게임 카테고리를 넣을 때와 같은 절차다. **색을 새로 더한다면 `palette.yaml` 의 검증기로
계산한다 — 눈대중 금지.**

---

## ⑥ ✅ 게이트 2 — 포크 범위 확정 (2026-08-21)

**"포크를 뜰지 말지"는 실물이 답했다 — 필요하다.** 수동 export 로 끝내는 선택지는
사라졌다: REST 는 키가 없어 막혔고, 앱 내 Export 는 크래시한다(§③).
그리고 애초에 **상시 자동 전송은 포크 없이는 성립하지 않는다.**

| | 변경 | 성격 |
|---|---|---|
| **F0** | WebView 에 `setDownloadListener` — Export 크래시 수정 | 기존 파일 몇 줄. **업스트림 PR 감** |
| **F1** | `PushWorker.kt` — 5분 HMAC POST → 젯슨 `/ingest/aw` | 새 파일. WorkManager 는 최소 15분이라 못 쓴다 → 코루틴 루프 |
| **F2** | `InteractionWatcher.kt` — 화면·터치 → afk 버킷 | 새 파일. **게이트 d 통과로 `UsageStatsManager` 만으로 가능** — BroadcastReceiver·접근성 권한 불필요, 과거분까지 커서로 조회됨 |
| **F3** | `UsageStatsWatcher` 간격 `INTERVAL_HOUR` → 설정값(5분), `allowBackup="true"` → `false` | 기존 파일 2곳 |

**F0 을 먼저 하는 것이 낫다.** 몇 줄이고, 고치면 API 키 없이도 표본이 나와 지금 막혀
있는 경로가 같이 풀린다. 크래시 스택트레이스는 §③ 에 있다.

★ **`allowBackup="true"` 는 그냥 정리 항목이 아니다.** 앱 데이터가 구글/삼성 백업으로
**클라우드에 올라간다** — 이 프로젝트 전제("데이터는 기기 밖으로 안 나간다")와 정면
충돌한다. F3 에서 반드시 끈다.

---

## ⑦ ★ 게이트 3 — 도메인 결정 (Cloudflare)

**상시 터널은 도메인이 필요하다.** Cloudflare 계정에 zone 으로 등록된 도메인이 있어야
`lt-ingest.내도메인.com` 같은 고정 주소를 받는다.

| | |
|---|---|
| 도메인 **있음** | named tunnel. 고정 주소, 무료 |
| 도메인 **없음** | Quick Tunnel 은 계정·도메인이 필요 없지만 **주소가 재시작마다 바뀌고 SLA 가 없다** → 폰에 URL 을 박는 상시 경로로는 못 쓴다. 도메인을 사거나, 이 항목을 미루고 ⑥의 "adb 로 가끔" 으로 간다 |

---

## ⑧ 젯슨: cloudflared (도메인이 정해진 뒤)

```bash
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install cloudflared
```

arm64 지원 확인함 (v2026.8.2 에 `cloudflared-linux-arm64.deb`).

```yaml
# ~/.cloudflared/config.yml
tunnel: <터널 UUID>
credentials-file: /home/user/.cloudflared/<UUID>.json
ingress:
  - hostname: lt-ingest.내도메인.com
    path: ^/ingest/.*
    service: http://100.64.0.2:8770
  - service: http_status:404       # ★ 나머지는 전부 404
```

★ **ingress 를 `/ingest/` 로 한정하지 않으면 플래너 전체가 인터넷에 열린다.**
배선 전에 규칙이 의도대로 걸리는지 확인한다:

```bash
cloudflared tunnel ingress validate
cloudflared tunnel ingress rule https://lt-ingest.내도메인.com/planner   # 404 로 떨어져야 한다
cloudflared tunnel ingress rule https://lt-ingest.내도메인.com/ingest/aw # 8770 으로 가야 한다
```

그 다음 `config/lifetrainer.toml` 에:

```toml
[ingest]
enabled = true
secret  = "<무작위 32바이트 — 이 값을 폰에도 넣는다>"
```

**세션 비밀키와 다른 값을 쓴다.** 폰이 들고 있는 값이라, 새더라도 플래너 세션까지 열리면 안 된다.

---

## ⑨ 포크 빌드 — Windows PC에서

> ★ **상세 지시서가 따로 있다: [fork-build.md](fork-build.md).**
> 툴체인 설치부터 F0~F3 구현·HMAC 규격·검증까지 그 문서 하나로 끝나게 썼다.
> 아래는 요약이다.

젯슨(arm64, 가용 4.5GB — 08-23 ctx 축소 전 1.5~2.8GB)에서는 Gradle·NDK·Rust·npm 3스택을
동시에 돌릴 수 없다.

★ **이 시점에 확정하고 다시는 안 바꾸는 것 4개** — 나중에 바꾸면 안드로이드가 **다른 앱으로
취급해 재설치가 되고 폰에 쌓인 기록이 전부 날아간다**:

1. **applicationId** (`net.activitywatch.android` → 우리 것)
2. **서명 키** — 만들자마자 백업
3. 앱 이름·아이콘 (상표 회피. MPL §2.3 은 상표를 라이선스에 포함하지 않는다)
4. 새 파일에 Exhibit A 헤더

**커밋에 시크릿·개인 데이터를 넣지 않는다** — 언젠가 공개하면 히스토리까지 공개된다.

폰에는 원본 ActivityWatch 와 **공존 설치**된다(applicationId 가 다르므로). 원본에 쌓인
데이터를 포크로 옮길 방법은 없으니, 포크로 갈아탈 때 원본 데이터는 ④의 `lt import` 로
미리 뽑아 두고 원본을 지운다.

---

## ⑩ 상시 가동 + 점검

- 3일 무인 동작 후 **빈 구간이 없는지** 확인 (배터리 관리가 워처를 죽였는지)
- `lt doctor` 에 폰 데이터 빈 구간 점검을 추가하는 것이 남아 있다

---

## 지금 당장 할 것만 요약 (2026-08-21 기준)

**①~⑥ 은 끝났다. 남은 것은 포크 빌드다.**

```
Windows PC   aw-android master 클론 (Rust nightly + NDK r28 + npm)
             ★ 젯슨에서는 못 한다 — 가용 메모리 3.9GB
             applicationId · 서명 키 · 앱 이름/아이콘을 먼저 확정하고 다시 안 바꾼다
             (바꾸면 안드로이드가 다른 앱으로 취급 → 재설치 → 쌓인 기록 소멸)

F0  WebView 에 setDownloadListener        Export 크래시. 업스트림 PR 로도 낼 것
F1  PushWorker.kt                         5분 HMAC POST → 젯슨 /ingest/aw
F2  InteractionWatcher.kt                 화면·터치 → afk 버킷 (UsageStatsManager 만으로)
F3  UsageStatsWatcher 간격 → 5분 설정값 · allowBackup="true" → false

젯슨        [ingest] enabled = true + secret 생성  ← 지금은 꺼져 있다
            (도메인이 정해지면 ⑦⑧ 의 Cloudflare 터널. 같은 Wi-Fi 라면 당장은 불필요)
```

### 이미 확인돼서 다시 안 해도 되는 것

| | |
|---|---|
| 앱 버전 | `v0.14.0dev20260723` — 공개된 최신 APK 가 맞다 |
| 젯슨 adb | 설치됨. USB 2.0 허브 정상 (막히면 **콜드 부팅**, 웜 재부팅 아님) |
| 게이트 a·b·c·d | 전부 확정 (§③) |
| `data.app` 처리 | 패키지명으로 바꾸는 코드 + 테스트 반영 완료 |
