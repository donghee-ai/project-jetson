# aw-android 포크 — 빌드 지시서 (Windows PC)

> **이 문서 하나만 읽고 시작할 수 있게 썼다.** 젯슨 저장소에 접근하지 못하는
> 다른 세션·다른 에이전트에게 그대로 넘겨도 된다. 필요한 규격은 전부 여기 옮겨 놨다.
>
> 작성 2026-08-21 / 선행 조사: [research.md](research.md) · [plan.md](plan.md) ·
> 전체 순서: [runbook.md](runbook.md)

---

## 0. 무엇을 만드는가

**젯슨(NVIDIA Orin NX)에서 24시간 도는 개인 활동 로깅 에이전트 "Life Trainer" 가 있다.**
노트북 활동은 ActivityWatch 로 이미 자동 수집된다. **폰이 빠져 있다.**

젯슨 쪽 수신부는 **이미 완성돼 있다** — `POST /ingest/aw`(HMAC 서명·재전송 방지),
`lt import`, 롤업의 기기 차원·중재까지. **비어 있는 것은 폰이 밀어주는 쪽뿐이다.**

```
폰 (aw-android 포크)  ──5분마다 HMAC POST──▶  젯슨 /ingest/aw  ──▶ SQLite ─▶ 플래너·리포트
```

### 왜 포크인가 — 원본으로는 안 되는 것

| 필요한 것 | 원본 aw-android |
|---|---|
| 5분 주기 수집 | ❌ `UsageStatsWatcher` 가 `AlarmManager.INTERVAL_HOUR` 하드코딩 |
| 5분 주기 전송 | ❌ 전송 기능 자체가 없다 |
| 화면·터치 판정 | ❌ 워처가 없다 |
| 데이터 내보내기 | ❌ **버튼을 누르면 앱이 죽는다** (아래 F0) |
| 프라이버시 | ❌ `allowBackup="true"` — 앱 데이터가 클라우드 백업으로 나간다 |

### 이미 확정된 사실 (다시 조사하지 말 것)

2026-08-19~21 에 master 소스와 안드로이드 테스트 기기로 닫았다.

| | 확정된 값 | 근거 |
|---|---|---|
| 버킷 ID | `aw-watcher-android` · `aw-watcher-android-unlock` | master `SessionEventWatcher.kt` |
| 버킷 타입 | `currentwindow` · `os.lockscreen.unlocks` | 같음 |
| 이벤트 `data` | `app` = **앱 이름**("카카오톡") / `package` = 패키지명 / `classname` | master `refs/models/Event.kt` |
| 화면·터치 이벤트 | **실제로 나온다** — 화면 켜짐과 사용자 상호작용 수가 달라 둘을 구분할 수 있다. 개인별 이벤트 개수는 공개본에서 제거 | 테스트 기기 `dumpsys usagestats` |

★ **`SCREEN_INTERACTIVE` > `USER_INTERACTION`** — "화면은 켜졌는데 안 만졌다"가
실제로 구분된다. 그래서 F2 를 `UsageStatsManager` 만으로 만들 수 있다:
**BroadcastReceiver 도 접근성 권한도 필요 없고, 과거분까지 커서로 조회된다.**

> `data.app` 이 앱 이름이라는 것은 **젯슨 쪽에서 이미 처리했다** — 수신 시
> `app`←패키지명, `title`←앱 이름으로 바꿔 넣는다. **포크에서 건드리지 마라.**
> 원본 형식 그대로 보내면 된다.

### ★ 버킷 목록 — 젯슨의 판별 순서와 짝이다 (2026-08-23 추가)

**여기에 버킷을 추가하면 젯슨의 `aw_sync.bucket_type()` 판별 순서를 반드시 같이
확인해야 한다.** 폰 버킷은 전부 `"android"` 를 포함하므로, 구체적인 판별이 먼저
오지 않으면 통째로 `android` 로 뭉개진다.

| 버킷 ID | 젯슨 타입 | 무엇을 주나 | 상태 |
|---|---|---|---|
| `aw-watcher-android` | `android` | 앱 세션 (`app`=패키지, `title`=앱 이름) | 주력 |
| `aw-watcher-android-afk` | `afk` | `{"status": "afk"｜"not-afk"}` | 지속 수집 |
| `aw-watcher-android-unlock` | `unlock` | 잠금해제 시각 (duration 0) | 지속 수집 |
| `aw-watcher-android-media` | `android` | 재생 중인 앱 | 추가 워처 |
| `aw-watcher-android-web` | **`web`** | 브라우저 URL | 표본이 적어 재측정 필요 |

**판별 순서: `unlock` → `afk` → `web` → `android` → `window`.**
`media` 는 전용 분기를 두지 않고 `android` 로 받는다 — 재생 구간이 앱 세션과
겹치므로 롤업의 `_union`이 흡수한다(합성 회귀 데이터로 이중 계산 없음 확인).

#### 이 표가 없어서 생긴 사고 (2026-08-23)

`media`/`web` 두 워처를 08-22 에 붙이면서 이 문서를 안 거쳤다. 그래서 젯슨의
판별 순서를 갱신할 계기가 없었고, `aw-watcher-android-web` 이 `"android"` 에 먼저
걸려 **`web` 분기에 영원히 도달하지 못했다.** 롤업은 URL 을 `buckets["web"]` 에서만
꺼내므로 폰의 URL 이 통째로 죽었고, `rules.yaml` 의 URL 규칙이 폰 브라우징에
하나도 걸리지 않았다. **에러는 나지 않았다.**

회귀 테스트로 못박아 뒀다 — `tests/test_aw_sync.py::test_bucket_type_order_is_specific_before_general`
이 폰 버킷 다섯 개의 타입을 표로 검사한다. **새 워처를 붙이면 이 테스트에 한 줄
추가하는 것이 절차다.**

#### `aw-watcher-android-web` 은 아직 못 쓴다

9건을 08-22 13:20~14:04 에 찍고 멈췄다. 데이터 품질도 낮다 — 방문 기록이 아니라
**주소창 텍스트 스냅숏**이다:

```
(0.7s) "Google 검색 또는 URL 입력"   ← 빈 주소창 안내문구
(0.0s) "국"                          ← 타이핑 중간 글자
(2.0s) "google.com/search?q=..."     ← 이건 쓸 만하다
```

안드로이드 크롬 URL 은 `UsageStatsManager` 로 안 잡히고 접근성 서비스가 필요하다.
**우선순위를 낮게 둔다** — 같은 목적이라면 노트북에 브라우저 확장을 까는 쪽이
5분이고 데이터 품질도 낫다.

---

## 1. ★ 시작 전에 확정하고 다시는 바꾸지 않는 것 4가지

**바꾸면 안드로이드가 다른 앱으로 취급해 재설치가 되고, 폰에 쌓인 기록이 전부 날아간다.**

| | 예시 | 왜 되돌릴 수 없나 |
|---|---|---|
| **1. `applicationId`** | `net.lifetrainer.awphone` | 앱의 정체성. 바꾸면 별개 앱 |
| **2. 서명 키(keystore)** | `lifetrainer-release.jks` | 키가 다르면 덮어쓰기 설치 불가 |
| **3. 앱 이름·아이콘** | `LT Phone` | 상표 회피 (아래 §3) |
| **4. 원본 제거 시점** | — | 원본과 **공존 설치**된다. 원본 데이터는 옮길 방법이 없다 |

```
★ keystore 를 만들자마자 백업한다. 잃으면 그 앱은 영원히 업데이트할 수 없다.
★ keystore·비밀번호·HMAC 시크릿을 커밋하지 않는다. 언젠가 공개하면 히스토리까지 공개된다.
```

`applicationId` 는 `app/build.gradle` 의 `defaultConfig` 에서 바꾼다.
`namespace`(코틀린 패키지)까지 같이 바꿀 필요는 없다 — **`applicationId` 만 바꾸는 것이
훨씬 안전하다.** 소스 전체 리네이밍은 병합 충돌만 늘린다.

---

## 2. 윈도우 툴체인

aw-android 는 **코틀린 + Rust + 웹** 세 스택을 한 번에 빌드한다. 이게 젯슨에서
못 하는 이유다(가용 메모리 4.5GB — 08-23 ctx 축소 후에도 세 스택 동시 빌드는 못 든다).

| | 버전 | 확인 |
|---|---|---|
| **JDK** | 17 (Temurin 권장) | `java -version` |
| **Android Studio** | 최신 | SDK Manager 를 여기서 쓴다 |
| **Android SDK** | Platform 35 이상 | SDK Manager → SDK Platforms |
| **NDK** | **r28** | SDK Manager → SDK Tools → "Show Package Details" → NDK |
| **Rust** | **nightly** | `rustup toolchain install nightly` |
| **Node.js** | 20 이상 | `node -v` — `aw-webui` 빌드용 |
| **Git** | 최신 | |

```powershell
# Rust — 안드로이드 타깃 4종
rustup toolchain install nightly
rustup default nightly
rustup target add aarch64-linux-android armv7-linux-androideabi i686-linux-android x86_64-linux-android

# 환경변수 (시스템 속성 → 환경 변수, 또는 PowerShell 프로필)
setx ANDROID_HOME "%LOCALAPPDATA%\Android\Sdk"
setx ANDROID_NDK_HOME "%LOCALAPPDATA%\Android\Sdk\ndk\28.0.xxxxx"
```

> **경로에 공백·한글이 없는 곳에 클론한다** (`C:\dev\aw-android`).
> Rust/NDK 툴체인이 공백 경로에서 조용히 실패하는 사례가 흔하다.
>
> **Windows Defender 실시간 검사에서 빌드 폴더를 제외**하면 빌드가 눈에 띄게 빨라진다.

---

## 3. 클론 → **수정 전에 원본이 빌드되는지 먼저 확인**

```powershell
cd C:\dev
git clone --recurse-submodules https://github.com/ActivityWatch/aw-android.git
cd aw-android
git submodule update --init --recursive
.\gradlew.bat assembleDebug
```

**여기서 반드시 멈춘다.** 원본이 안 빌드되는데 우리 코드를 얹으면, 실패 원인이
툴체인인지 우리 변경인지 구분할 수 없다. 이 프로젝트가 반복해서 데인 실패다.

- 빌드 산출물: `app/build/outputs/apk/debug/app-debug.apk`
- Rust 쪽이 깨지면 `aw-server-rust/` 서브모듈과 NDK 버전을 먼저 의심한다
- **master 를 쓴다** (릴리스 태그가 아니라). 위 §0 의 확정 사실이 master 기준이다

### MPL-2.0 준수 — 클론 직후에 정한다

aw-android 는 **MPL-2.0** 이다. AGPL 같은 네트워크 조항은 없지만:

- **파일 단위 카피레프트다.** 기존 파일을 고치면 **그 파일**을 공개해야 한다
- **새로 만든 파일**은 우리 라이선스로 둘 수 있다. 다만 이 프로젝트는 결국 공개할
  계획이므로 **처음부터 준수해서 만든다** — 새 파일 맨 위에 Exhibit A 헤더를 넣는다:

```kotlin
/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */
```

- **의무는 "배포" 시점에 생긴다.** 내 폰에만 설치하면 공개 의무는 없다
- **상표는 라이선스에 포함되지 않는다**(MPL §2.3). 그래서 §1-3 의 앱 이름·아이콘 교체가
  선택이 아니라 필수다

---

## 4. F0 — Export 크래시 수정 ★ 먼저 한다

### 왜 먼저인가

**지금 이 앱은 데이터를 밖으로 꺼낼 방법이 아예 없다.**

- REST API 는 API 키를 요구하는데 **키 설정 화면(`AuthSettingsActivity`)이 master 에만
  있고 배포된 APK 에는 없다**
- 앱 안의 Export 버튼은 **누르면 앱이 죽는다**

F0 는 몇 줄이고, 고치면 **표본이 나와 젯슨에 기기를 등록할 수 있다**(§8이 이것에 의존).
그리고 **업스트림 PR 로도 그대로 낼 수 있다** — 이슈 #228.

### 원인 (스택트레이스 확보됨)

```
android.content.ActivityNotFoundException:
  No Activity found to handle Intent { act=android.intent.action.VIEW dat=blob: }
```

앱은 화면을 `WebView`(aw-webui)로 그린다. 웹 UI 가 export 를 만들 때 `blob:` URL 로
다운로드를 트리거하는데, **`WebView` 에 `DownloadListener` 가 없어서** 안드로이드가
외부 인텐트로 넘긴다. `blob:` 을 열 수 있는 앱은 없으므로 죽는다.

**데이터 자체는 만들어져 있고 전달만 실패한다.**

### 고치는 곳

`WebView` 를 만드는 곳(`MainActivity` 또는 `WebViewFragment` 계열)에 붙인다.

```kotlin
webView.settings.javaScriptEnabled = true          // 이미 켜져 있다

// blob: 은 자바스크립트로 읽어야 한다. 인텐트로 넘기면 죽는다.
webView.addJavascriptInterface(BlobSaver(this), "AndroidBlobSaver")
webView.setDownloadListener { url, _, contentDisposition, mimeType, _ ->
    if (url.startsWith("blob:")) {
        webView.evaluateJavascript(blobReaderJs(url, mimeType), null)
    } else {
        // http(s) 는 기존 DownloadManager 경로로
        val req = DownloadManager.Request(Uri.parse(url)).apply {
            setMimeType(mimeType)
            addRequestHeader("User-Agent", webView.settings.userAgentString)
            setNotificationVisibility(
                DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
            setDestinationInExternalPublicDir(
                Environment.DIRECTORY_DOWNLOADS,
                URLUtil.guessFileName(url, contentDisposition, mimeType))
        }
        (getSystemService(DOWNLOAD_SERVICE) as DownloadManager).enqueue(req)
    }
}
```

`blobReaderJs` 는 `FileReader` 로 blob 을 base64 로 읽어 자바스크립트 인터페이스에
넘기는 짧은 스크립트다. `BlobSaver` 쪽에서 디코드해 **`MediaStore` 또는 앱의
`getExternalFilesDir(DIRECTORY_DOWNLOADS)`** 에 쓴다.

> **앱 전용 폴더(`getExternalFilesDir`)에 쓰는 쪽이 권한이 안 필요해서 간단하다.**
> 경로가 `/sdcard/Android/data/<applicationId>/files/Download/` 라 adb 로 바로 꺼낼 수 있다.
> 공용 Downloads 에 쓰려면 API 29+ 에서 `MediaStore` 를 써야 한다.

### 검증

앱 → ☰ → Raw Data → 버킷 → **Export → JSON**. **앱이 안 죽고 파일이 생기면 성공.**

```powershell
adb shell ls -la /sdcard/Android/data/<applicationId>/files/Download/
adb pull /sdcard/Android/data/<applicationId>/files/Download/<파일>.json
```

**CSV 가 아니라 JSON 으로 뽑는다** — CSV 는 버킷 메타(타입·hostname)가 빠져서 젯슨이
버킷 종류를 판별하지 못한다.

---

## 5. F1 — `PushWorker.kt` (5분마다 젯슨으로 전송)

### ★ HMAC 규격 — 한 글자도 틀리면 401 이다

젯슨 서버(`web/auth.py`)가 검증하는 형식이다. **아래가 유일한 정답이다.**

```
헤더:  Authorization: LT1 <device>:<ts>:<nonce>:<sig>

sig  = base64url_nopad( HMAC-SHA256( secret, msg ) )
msg  = "<device>\n<ts>\n<nonce>\n<sha256(body) 소문자 hex>"

<device>  기기 이름. 젯슨에 등록된 이름과 정확히 같아야 한다 (예: phone-example)
<ts>      정수 unix 초 (밀리초 아님). 서버 시각과 ±300초 안이어야 한다
<nonce>   요청마다 새로 만드는 무작위 문자열. 창 안에서 재사용하면 거부된다
<body>    실제로 보내는 바이트 그대로. 서명 후 한 바이트도 바꾸면 안 된다
```

```kotlin
private fun authHeader(device: String, secret: String, body: ByteArray): String {
    val ts = System.currentTimeMillis() / 1000          // ★ 초 단위
    val nonce = UUID.randomUUID().toString()

    val bodyHex = MessageDigest.getInstance("SHA-256")
        .digest(body)
        .joinToString("") { "%02x".format(it) }          // ★ 소문자 hex

    val msg = "$device\n$ts\n$nonce\n$bodyHex"

    val mac = Mac.getInstance("HmacSHA256").apply {
        init(SecretKeySpec(secret.toByteArray(Charsets.UTF_8), "HmacSHA256"))
    }
    val sig = Base64.encodeToString(
        mac.doFinal(msg.toByteArray(Charsets.UTF_8)),
        Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP   // ★ 셋 다 필수
    )
    return "LT1 $device:$ts:$nonce:$sig"
}
```

> ★★ **`Base64.NO_WRAP` 을 빼면 안드로이드가 줄바꿈을 넣는다.** 헤더가 깨져서 401 이
> 나오는데 원인이 안 보인다. `URL_SAFE`(`-_` 사용)와 `NO_PADDING`(`=` 없음)도 필수다.
>
> **서명은 보낼 바이트에 대해 계산한다.** JSON 을 문자열로 만든 뒤 다시 직렬화하거나,
> gzip 을 서명 후에 걸면 해시가 어긋난다. **압축을 쓰려면 압축된 바이트를 서명한다.**

### 본문 형식 — ActivityWatch export 그대로

우리 봉투를 새로 만들지 않는다. **`/api/0/export` 출력을 그대로 보낸다.**

```json
{
  "buckets": {
    "aw-watcher-android": {
      "type": "currentwindow",
      "client": "aw-watcher-android",
      "hostname": "localhost",
      "events": [
        {"timestamp": "2026-08-21T09:00:00.000Z", "duration": 42.0,
         "data": {"app": "카카오톡", "package": "com.kakao.talk", "classname": "..."}}
      ]
    }
  }
}
```

| 제한 | 값 |
|---|---|
| 본문 크기 | **4 MB** (`ingest.max_body_bytes`) — 넘으면 **413** |
| 이벤트 수 | **50,000** / 요청 — 넘으면 400 |
| 시계 오차 | **±300초** (`ingest.clock_skew_sec`) |

5분치는 이 한도에 한참 못 미친다. **다만 오프라인이 길어져 밀린 분량을 한 번에
보내면 넘을 수 있으니, 이벤트 수 기준으로 잘라 여러 번 보낸다.**

### 재전송 안전성

젯슨의 `aw_event` PK 가 `(bucket_id, ts)` 라 **같은 페이로드를 몇 번 넣어도 결과가
같다.** 응답을 못 받았으면 그냥 다시 보내면 된다.

단 **nonce 는 매번 새로 만든다** — 같은 nonce 로 재전송하면 재전송 방지에 걸려 401 이다.

### 응답

```json
{"ok": true, "device": "phone-example", "buckets": 1, "events": 128, "skipped": []}
```

| 코드 | 뜻 | 폰이 할 일 |
|---|---|---|
| 200 | 들어감 | 커서 전진 |
| **401** | 서명·시각·nonce 문제 | **재시도해도 똑같다.** 설정·시계를 본다 |
| **404** | 수신이 꺼져 있음 (`ingest.enabled=false`) | 젯슨 설정 문제 |
| 413 | 본문 초과 | 잘라서 다시 |
| 400 | 형식 오류 / **미등록 기기** | §8 로 기기를 먼저 등록 |
| 5xx·타임아웃 | 젯슨 쪽 | 백오프 후 재시도 |

> **401 을 무한 재시도하지 마라.** 배터리만 먹는다. 로그에 사유를 남기고 멈춘다 —
> 서버가 실패 사유를 본문에 그대로 돌려준다(설정 오류를 진단할 수 있게 일부러 그렇게 뒀다).

### 스케줄링 — WorkManager 를 쓰면 안 된다

**`WorkManager` 의 주기 작업 최소 간격은 15분이다.** 5분을 못 맞춘다.

→ 기존 `BackgroundService`(foreground service)의 코루틴 루프로 돌린다.
이미 foreground 알림이 떠 있으므로 추가 비용이 없다.

```kotlin
while (isActive) {
    runCatching { pushOnce() }.onFailure { Log.w(TAG, "push 실패", it) }
    delay(intervalMinutes * 60_000L)
}
```

**보낸 지점을 커서로 저장한다**(마지막으로 성공한 이벤트 timestamp). 노트북 쪽
`aw_sync` 와 같은 구조다 — **늦게 도착하는 것은 괜찮지만 빠지는 것은 안 된다.**

### 설정값을 어디에 두나

`device` · `secret` · 젯슨 URL · 주기. **하드코딩하지 말고 앱 안에 설정 화면을 하나
만든다** — 시크릿을 소스에 넣으면 커밋에 들어간다. `EncryptedSharedPreferences` 를 쓴다.

---

## 6. F2 — `InteractionWatcher.kt` (화면·터치)

**게이트 d 가 통과했으므로 `UsageStatsManager` 만으로 만든다.**
BroadcastReceiver 도, 접근성 권한도 필요 없다. 과거분까지 커서로 조회된다.

```kotlin
val events = usm.queryEvents(lastCursor, System.currentTimeMillis())
val e = UsageEvents.Event()
while (events.getNextEvent(e)) {
    when (e.eventType) {
        UsageEvents.Event.SCREEN_INTERACTIVE     -> // 화면 켜짐
        UsageEvents.Event.SCREEN_NON_INTERACTIVE -> // 화면 꺼짐
        UsageEvents.Event.USER_INTERACTION       -> // ★ 실제 터치
        UsageEvents.Event.KEYGUARD_HIDDEN        -> // 잠금 해제
    }
}
```

**만드는 것은 afk 버킷이다.** 젯슨이 노트북과 같은 방식으로 다룰 수 있게:

```
버킷 ID   aw-watcher-android-afk
타입      afkstatus
data      {"status": "not-afk"}  또는  {"status": "afk"}
```

> 젯슨의 `bucket_type()` 이 **`afk` 를 `android` 보다 먼저** 검사하도록 이미 짜여 있다.
> `aw-watcher-android-afk` 는 두 패턴에 다 걸리는데, afk 로 잡히는 것이 맞다.

**판정 규칙**: `USER_INTERACTION` 이 있는 구간만 `not-afk`. 화면만 켜져 있고 터치가
없으면 `afk` 다 — S24 실측에서 화면 켬 234 vs 터치 192 로 **실제로 갈린다.**

이 구분이 중요한 이유: 알림 확인하려고 화면만 켠 것을 "폰 사용 중"으로 세면
플래너가 폰 시간을 부풀린다.

---

## 7. F3 — 주기 5분 + 백업 끄기

### 7-1. 수집 주기

`UsageStatsWatcher` 의 `AlarmManager.INTERVAL_HOUR` 를 **설정값(기본 5분)** 으로 바꾼다.

```kotlin
// 기존: AlarmManager.INTERVAL_HOUR
alarmManager.setInexactRepeating(type, triggerAt, intervalMs, pendingIntent)
```

> **`setInexactRepeating` 은 그대로 둬도 된다.** 안드로이드 OS 가 사용 통계를 자체적으로
> 계속 기록하고 있고 우리는 커서로 따라잡는 것이라, **주기가 정확도를 좌우하지 않는다.**
> 늦는 것은 *도착*이지 *기록*이 아니다. 정확한 알람(`setExactAndAllowWhileIdle`)을 쓰면
> 배터리만 먹고 Doze 와 싸우게 된다.

`EventParsingWorker`(24시간 주기)는 **놓친 것을 메우는 백스톱이라 그대로 둔다.**

### 7-2. ★ `allowBackup` 끄기 — 이건 정리 항목이 아니다

`app/src/main/AndroidManifest.xml`:

```xml
<application
    android:allowBackup="false"
    android:dataExtractionRules="@xml/data_extraction_rules"   <!-- API 31+ -->
    ...>
```

**`allowBackup="true"` 면 앱 데이터가 구글/삼성 백업으로 클라우드에 올라간다.**
이 프로젝트의 전제가 "데이터는 기기 밖으로 나가지 않는다" 인데 정면으로 충돌한다.
**HMAC 시크릿까지 같이 올라간다.**

---

## 8. 젯슨에 붙이기 — 순서가 중요하다

**`POST /ingest/aw` 는 이미 등록된 기기만 받는다.** 그래서 **첫 등록은 파일로 해야 한다.**

```
F0 로 Export 를 고친다
  → 앱에서 JSON export → adb pull
  → 젯슨에서  lt import --from <파일> --device phone-example --rollup   ← 여기서 등록된다
  → 그 다음부터 F1 의 POST 가 통과한다
```

**젯슨 쪽에서 (사람이 하는 일):**

```bash
cd /home/user/project/project-jetson/life-trainer

# 1) 기기 등록 + 첫 데이터
.venv/bin/lt import --from ~/phone.json --device phone-example --rollup

# 2) 수신 켜기 (기본이 꺼짐이다)
$EDITOR config/lifetrainer.toml
```

```toml
[ingest]
enabled = true
secret  = "<무작위 32바이트 — 이 값을 폰 설정에도 넣는다>"
```

```bash
systemctl --user restart lifetrainer-web
```

> ★ **`--device` 이름은 한 번 정하면 계속 쓴다.** 나중에 바꾸면 같은 폰이 두 기기로 갈라진다.
> 자동 추론에 맡기면 안 된다 — 폰의 `/api/0/info` 가 hostname 을 **`localhost`** 로 준다.
>
> ★ **`[ingest].secret` 은 `[web].session_secret` 과 다른 값이어야 한다.** 폰이 들고 있는
> 값이라, 새더라도 플래너 세션까지 열려서는 안 된다.

**네트워크**: 폰과 젯슨이 같은 Wi-Fi 면 젯슨의 tailnet IP(`100.64.0.2:8770`)로 바로
쏘면 된다. **셀룰러에서도 되게 하려면 Cloudflare Tunnel 이 필요하고 도메인이 있어야
한다** — [runbook.md](runbook.md) §⑦⑧. 그건 나중에 해도 된다.

---

## 9. 검증 — 눈으로 본다

```bash
# 젯슨에서
.venv/bin/lt doctor
sqlite3 data/lifetrainer.db "select name,kind from device"          # 폰이 보이는가
sqlite3 data/lifetrainer.db "select bucket_id,type from aw_bucket"  # android 버킷이 붙었는가
.venv/bin/lt timeline --day $(date +%F)                             # ★ PNG 를 실제로 연다
```

확인할 것:

- 폰 사용이 **제 시간대에** 찍혔는가 (타임존·에폭 단위 오류가 여기서 드러난다)
- **노트북 작업 시간이 폰에 먹히지 않았는가.** 겹친 구간은 "마지막 상호작용이 그 시간을
  소유한다" 규칙으로 중재된다
- **하루 합이 24시간을 넘지 않는가**

> 합성 데이터로 만든 가정 위에서 자기를 검증한 것이 이 프로젝트의 가장 큰 사고
> (`HISTORY/2026-08-17-aw-overlap-inflation.md`) 원인이었다. **기기 간 겹침은 정확히
> 같은 부류라 실데이터로만 확인된다.**

### 분류 규칙

폰 앱은 처음에 **전부 미분류**로 나온다 — `config/rules.yaml` 에 안드로이드가 하나도 없다.

```bash
sqlite3 data/lifetrainer.db \
  "select app, seconds_total/60 from unclassified order by 2 desc limit 20"
```

상위부터 `rules.yaml` 에 **패키지명으로** 넣고 재롤업한다(젯슨이 `app` 컬럼에
패키지명을 넣으므로). 색을 새로 더한다면 `palette.yaml` 의 검증기로 계산한다 — 눈대중 금지.

---

## 10. 3일 무인 확인

배터리 관리가 워처를 죽였는지 본다.

- 폰: 설정 → 앱 → (우리 앱) → 배터리 → **"제한 없음"**
- 삼성: 설정 → 배터리 → 백그라운드 사용 제한 → **"절전 앱 / 딥 슬리핑 앱" 목록에서 뺀다**
- 3일 뒤 타임라인에 **설명되지 않는 빈 구간**이 없는지 본다

---

## 11. 보고할 것

```
빌드      원본 baseline 빌드 성공 여부 / 툴체인에서 막힌 것
정체성    applicationId · 앱 이름 · keystore 백업 위치(경로만, 파일은 보내지 말 것)
F0        Export 가 크래시 없이 JSON 을 뱉는가 / 저장 경로
          ★ 업스트림 PR 로 낼 만한 형태인지 (이슈 #228)
F1        첫 POST 가 200 을 받았는가. 401 이면 서명 msg 문자열을 그대로 보고할 것
          (secret 은 빼고 — device/ts/nonce/bodyhex 만)
F2        afk 버킷이 붙는가. not-afk 비율이 말이 되는가
F3        주기 5분 반영 / allowBackup=false 확인
젯슨      device 테이블에 폰이 보이는가 / 타임라인 PNG 를 눈으로 봤는가
미분류    상위 20개 목록 (규칙 보강 판단용)
```

**막히면 그 지점에서 멈추고 보고한다.** 특히 401 은 추측으로 고치지 말 것 —
서명 문자열 한 글자 차이라 로그를 봐야 안다.
