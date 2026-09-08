# 남은 단계 — 폰이 있어야 하는 작업

코드는 전부 들어갔고 서명된 APK 도 나온다. 여기서부터는 **실기기 없이는 진행할 수 없다.**

APK: `c:\jetsonapk\aw-android\mobile\build\outputs\apk\release\mobile-release.apk`

> ★ **순서가 중요하다.** 젯슨은 **이미 등록된 기기만** POST 를 받는다. 그래서 첫 등록은
> 반드시 파일(`lt import`)로 해야 하고, 그 다음에야 5분 push 가 200 을 받는다.

---

## 1. 설치

폰 USB 연결 → 파일 전송 모드 → 개발자 옵션의 USB 디버깅 켬.

```powershell
# adb 는 WSL 안에 있다. Windows 에서 쓰려면 platform-tools 를 따로 받거나,
# WSL 의 adb 에 USB 를 넘기려면 usbipd-win 이 필요하다.
# 가장 간단한 방법: APK 를 폰으로 복사해 파일 관리자에서 직접 설치한다.
```

무선 디버깅을 쓰면 WSL 에서 바로 붙는다 (폰과 PC 가 같은 네트워크일 때):

```bash
wsl -d Ubuntu
. ~/.lt-build-env
adb pair <폰에 뜬 IP:포트>       # 설정 → 개발자 옵션 → 무선 디버깅 → 페어링 코드
adb connect <폰 IP:5555>
adb install -r "$LT_REPO/mobile/build/outputs/apk/release/mobile-release.apk"
```

설치 후 폰에서:

- **사용 통계 접근 권한**을 허용한다 (앱이 먼저 물어본다). 없으면 아무것도 수집되지 않는다
- 설정 → 앱 → **LT Phone** → 배터리 → **"제한 없음"**
- 삼성: 설정 → 배터리 → 백그라운드 사용 제한 → **절전/딥 슬리핑 앱 목록에서 뺀다**

> 원본 ActivityWatch 와 **공존 설치**된다 (`applicationId` 가 다르다). 원본의 기존 데이터는
> 옮길 방법이 없다.

---

## 2. Export 로 표본 뽑기

앱 → ☰ → **Raw Data**

### ★ 어느 버튼인지가 중요하다

| UI 위치 | 문구 | 쓰나 |
|---|---|---|
| 버킷 목록 각 행의 드롭다운 | `Export bucket as JSON` | ✗ 버킷 **하나만** 나온다 |
| 같은 드롭다운 | `Export events as CSV` | ✗ 버킷 메타(type·hostname)가 빠진다 |
| 페이지 하단 "Import and export buckets" 카드 | **`Export all buckets as JSON`** | ✅ **이것** |

전부 내보내야 하는 이유: 젯슨은 `_PHONE_BUCKET_TYPES = {android, unlock}` 로 기기 종류를
정한다. 버킷 하나만 뽑았는데 그게 `aw-watcher-android-afk` 면 **폰이 `laptop` 으로 등록되고**
`upsert_device` 가 kind 를 덮어쓰지 않아 사람이 손으로 고쳐야 한다.

내려받는 파일명은 `aw-bucket-export.json` 이다.
(`Export all buckets as JSON` → `GET /api/0/export`, 버킷별 → `GET /api/0/buckets/<id>/export`.
 둘 다 `{"buckets": {...}}` 봉투는 같지만 범위가 다르다.)

PR #229 의 **"저장 위치 선택" 피커**가 떠야 정상이다. Downloads 에 저장한다.

```bash
adb pull /sdcard/Download/aw-bucket-export.json ./phone.json
```

### 젯슨에 넣기 전에 확인

```bash
python3 -c "import json;d=json.load(open('phone.json'));print(list(d['buckets']))"
```

**`aw-watcher-android` 가 목록에 있어야 한다.**

### 기다릴 필요는 없다

안드로이드 OS 가 사용 통계를 약 7일치 자체 보관하고, `SessionEventWatcher` 는 첫 실행 때
커서가 없으면 `parseUsageEventsSince(0)` 으로 있는 것을 전부 긁어온다. 즉 **설치 직후
몇 분이면 최근 며칠치가 채워진다.** 반나절씩 기다릴 이유가 없다.

---

## 3. 젯슨에 기기 등록  ← **여기가 유일하게 남은 관문**

수신(`[ingest] enabled = true`)과 시크릿(`data/ingestsecret`, 0600)은 **이미 켜 뒀다.**
서명 경로도 살아있는 서버에 대고 검증을 마쳤다 — 유효한 서명으로 보내면 401 이 아니라
**400 "등록되지 않은 기기"** 가 돌아온다. 즉 남은 것은 등록뿐이다.

```bash
scp phone.json user:~/phone.json
ssh user
cd /home/user/project/project-jetson/life-trainer

# --device 이름은 한 번 정하면 계속 쓴다. 자동 추론 금지 (폰 hostname 이 localhost 다)
.venv/bin/lt import --from ~/phone.json --device phone-example --rollup

# 시크릿 확인 — 폰에 넣을 값
cat data/ingestsecret
```

---

## 4. Cloudflare Tunnel (셀룰러에서도 보내려면)

**아직 도메인이 없다.** 터널은 고정 호스트이름이 필요하다 — 폰 설정에 박아 넣는 값이라
매번 바뀌면 안 된다. `*.trycloudflare.com` 임시 터널은 재시작마다 주소가 바뀌어 쓸 수 없다.

1. 도메인을 하나 등록한다. **Cloudflare Registrar** 에서 사면 네임서버 설정이 생략된다
   (연 만원대). 다른 곳에서 샀다면 네임서버를 Cloudflare 로 옮긴다 (무료, 반영에 몇 시간)
2. 젯슨에서 한 번만 사람이 로그인한다:

```bash
ssh user
~/bin/cloudflared tunnel login       # 뜨는 URL 을 브라우저로 열어 도메인을 고른다
```

3. 나머지는 스크립트가 한다:

```bash
# c:\jetsonapk\jetson-tunnel-setup.sh 를 젯슨에 올려서
bash jetson-tunnel-setup.sh lt.내도메인.com
```

**cloudflared 2026.8.2 는 이미 설치돼 있다** (`~/bin/cloudflared`, sudo 불필요).
`systemctl --user` 서비스로 돌아가고, 이 계정은 `Linger=yes` 라 재부팅 후에도 유지된다.

### ★ 무엇이 공개되는가

인그레스 규칙이 **`/ingest/` 경로만** 통과시키고 나머지는 404 다.
플래너 UI·대시보드는 **여전히 tailnet 전용**이다 — `config/lifetrainer.toml` 의
"창 제목이 담긴 개인 기록이라 LAN 전체 노출은 안 된다" 는 입장을 그대로 지킨다.
인터넷에 나가는 것은 HMAC 으로 보호되는 엔드포인트 하나뿐이다.

터널이 뜬 뒤 밖에서 확인:

```bash
curl -i https://lt.내도메인.com/               # 404 여야 한다 (UI 안 열림)
curl -i -X POST https://lt.내도메인.com/ingest/aw   # 401 이어야 한다 (서명 없음)
```

---

## 4-b. 폰에서 push 설정

앱 → ☰ → **Life Trainer push**

| 항목 | 값 |
|---|---|
| 기기 이름 | `phone-example` (젯슨에 등록한 이름과 **정확히** 같아야 한다) |
| 젯슨 주소 | `https://lt.내도메인.com` — **https 여야 한다** |
| HMAC 시크릿 | 젯슨의 `data/ingestsecret` 내용 |
| 전송 주기 | 5 |
| 전송 스위치 | 켬 |

> ★ **평문 http 는 앱이 막는다.** `network_security_config.xml` 이 `127.0.0.1` 외의
> 평문을 차단한다 (Android 9+ 기본값). 기록에 창 제목이 담기므로 망에 그대로 흘리지
> 않겠다는 뜻이고, 터널이 TLS 를 붙여 주므로 문제되지 않는다.

**저장** 하면 그 시점부터 **자동 전송이 시작된다.** "지금 보내기" 는 5분을 기다리지 않고
설정이 맞는지 즉시 확인하는 진단 버튼일 뿐, 이것을 눌러야 전송되는 것이 아니다.

### 자동 전송이 도는 방식

| | |
|---|---|
| 주 경로 | `BackgroundService` 안의 Handler 체인 — 서비스 기동 1분 뒤 첫 전송, 이후 설정 주기(기본 5분) |
| 폴백 | `AlarmManager` 반복 알람 — **서비스가 OS 에 죽어도 살아남아** 전송을 이어간다 |
| 조건 | 전송 스위치 ON + device·URL·시크릿이 모두 채워져 있을 것 |
| 멈추는 경우 | 401 을 한 번 받으면 멈춘다 (재시도해도 같아서 배터리만 먹는다). 설정 화면에서 저장하면 해제된다 |

설정을 저장하면 돌고 있는 `BackgroundService` 에 즉시 반영된다 — 앱을 다시 켜거나
재부팅할 필요가 없다. 주기를 바꿔도 마찬가지다.

전송이 도는지는 **폰 상단의 상시 알림**(foreground service)으로 확인할 수 있다.
그 알림이 사라져 있으면 배터리 관리가 서비스를 죽인 것이다 — 1번의 배터리 설정을 다시 본다.

"지금 보내기" 를 누르면 결과가 화면에 그대로 나온다.

| 나오는 것 | 뜻 |
|---|---|
| `OK — 버킷 N개 / 이벤트 M건` | 성공 |
| `보낼 새 이벤트가 없다` | 연결·인증 정상. 커서 이후 새 이벤트가 없을 뿐 |
| `실패 HTTP 400` + "등록되지 않은 기기" | 3번을 안 했거나 이름이 다르다 |
| `실패 HTTP 404` | 터널 인그레스나 `ingest.enabled` 문제 |
| `실패 HTTP 401` | 아래 참조 |

> ★ **401 은 추측으로 고치지 않는다.** 서버가 사유를 본문에 그대로 돌려주고,
> 검증 순서가 **서명 → 시각 → nonce** 다. 실측으로 확인한 규칙:
> - **"요청 시각이 허용 오차(300초)를 벗어났습니다"** → **서명은 이미 통과한 것**이다.
>   시크릿은 맞고 **폰 시계만 틀렸다.** 폰의 자동 시간 설정을 켠다
> - **"서명이 유효하지 않습니다"** → 시크릿이 다르다
> - **"이미 처리된 요청입니다 (재전송)"** → nonce 재사용. 정상 동작에서는 안 나온다
>
> 401 을 한 번 받으면 앱이 전송을 멈춘다 (재시도해도 같아서 배터리만 먹는다).
> 설정 화면에서 저장하면 해제된다.

> 셀룰러가 안 될 때: 집 와이파이로 돌아오면 밀린 분량이 커서 기준으로 이어서 전송된다.
> **늦게 도착하는 것은 괜찮지만 빠지지는 않는다.**

### ★ Cloudflare 가 앞에서 막을 수 있다 (error code 1010)

Cloudflare 의 Browser Integrity Check 는 **User-Agent 가 없거나 수상한 요청을 오리진에
닿기도 전에 403 으로 끊는다.** 젯슨 로그에는 아무것도 안 남아 원인 찾기가 고약하다.

실측:

| User-Agent | 결과 |
|---|---|
| 없음 (python urllib 기본) | **403 `error code: 1010`** — 차단 |
| `Dalvik/...` (안드로이드 기본) | 400 — 통과 |
| `LTPhone/1.0` (우리가 박은 값) | 400 — 통과 |

앱은 `PushSender` 에서 `User-Agent: LTPhone/1.0` 을 **명시적으로 보낸다.** 기본값으로도
지금은 통과하지만 그건 Cloudflare 의 판단에 기대는 것이라 언제든 바뀔 수 있다.

> 나중에 curl 로 직접 찔러볼 때 403/1010 이 나오면 **서명 문제가 아니라 UA 문제다.**
> `curl -H "User-Agent: LTPhone/1.0" ...` 로 다시 해 볼 것.

---

## 5. 눈으로 검증

```bash
ssh user
cd /home/user/project/project-jetson/life-trainer
.venv/bin/lt doctor

# ★ 젯슨에 sqlite3 CLI 가 없다. python 으로 본다.
python3 -c "
import sqlite3; c=sqlite3.connect('data/lifetrainer.db')
for r in c.execute('select id,name,kind,active from device'): print(r)
for r in c.execute('select bucket_id,type,device_id from aw_bucket'): print(r)"

.venv/bin/lt timeline --day $(date +%F)      # ★ PNG 를 실제로 연다
```

확인할 것:

- `phone-example` 가 **`kind='phone'`, `active=1`** 인가
- 버킷 3종이 붙었는가 — `aw-watcher-android`(android) / `-unlock`(unlock) / `-afk`(**afk**)
- 폰 사용이 **제 시간대에** 찍혔는가 (타임존·에폭 오류가 여기서 드러난다)
- **노트북 시간이 폰에 먹히지 않았는가** / **하루 합이 24시간을 넘지 않는가**
- afk 의 `not-afk` 비율이 말이 되는가 (화면만 켠 시간이 사용으로 세어지면 안 된다)

> 합성 데이터로 만든 가정 위에서 자기를 검증한 것이 이 프로젝트의 가장 큰 사고였다.
> **기기 간 겹침은 실데이터로만 확인된다.** `device` 에 아직 `synth-pc` 가 남아 있으니
> 헷갈리지 말 것.

### 분류 규칙

폰 앱은 처음에 전부 미분류다 — `config/rules.yaml` 에 안드로이드가 하나도 없다.

```bash
python3 -c "
import sqlite3; c=sqlite3.connect('data/lifetrainer.db')
for r in c.execute('select app, seconds_total/60 from unclassified order by 2 desc limit 20'): print(r)"
```

상위부터 **패키지명으로** 넣는다 (젯슨이 `app` 컬럼에 패키지명을 채운다).
색을 더하면 `palette.yaml` 검증기로 계산한다 — 눈대중 금지.

---

## 6. 3일 무인 확인

배터리 관리가 워처를 죽였는지 본다. 3일 뒤 타임라인에 **설명되지 않는 빈 구간**이
없는지 확인한다. 있으면 위 1번의 배터리 설정을 다시 본다.
