# 폰 사용 기록 수집 — 앱·전송 경로 조사

> 조사일 2026-08-18 / 상태: **조사만.** 아직 아무것도 안 깔았고 안 만들었다
> 관련: [../../HANDOFF.md](../../HANDOFF.md) §7-C · [../../docs/known-issues.md](../../docs/known-issues.md)

---

## 0. 결론 먼저

**직접 만들 필요는 없다. 다만 완전한 앱 하나는 없고 두 개를 조합해야 한다.**

```
폰: ActivityWatch (aw-android)   사용량 수집 → 내보내기 (JSON)
    Syncthing                    E2E 암호화 P2P 전송
         ↓
젯슨: 수신 폴더 → lt import
```

이 조합의 값어치는 **젯슨을 인터넷에 열지 않아도 된다**는 데 있다. VPN도,
공유기 포트포워딩도, Cloudflare Tunnel도 필요 없다.

**단, 두 가지가 미확인이라 실물로 확인하기 전에는 코드를 짜지 않는다** (§4).

---

## 1. 제약 조건 (사용자가 정한 것)

| | |
|---|---|
| 폰 연결 | **셀룰러 데이터** (집 Wi-Fi 에 안 붙음) |
| VPN | **쓰지 않는다** (Tailscale 포함) |
| 데이터 원칙 | 기기 밖으로 안 나간다 — 이 프로젝트의 전제 |

이 셋이 겹치면 **젯슨의 LAN(192.168.1.100)도 Tailscale(100.64.0.2)도 못 쓴다.**
남는 것은 공개 인터넷 경유뿐인데, 젯슨에는 **모든 창 제목**이 들어 있어서 노출
비용이 크다. 그래서 §3 의 Syncthing 경로를 찾았다.

---

## 2. 앱 후보

| 앱 | 라이선스 | 수집 | 내보내기 | 판정 |
|---|---|---|---|---|
| **ActivityWatch (aw-android)** | MPL-2.0 | ✅ UsageStats | ✅ CSV/JSON + REST `/api/0/export` | **1순위** |
| usageDirect | GPL-3.0+ | ✅ UsageStats | ❓ README·F-Droid 에 **언급 없음** | 2순위, 확인 필요 |
| Screen Time (Atharok) | 오픈소스 | ✅ | ❓ 미확인 | 3순위 |
| HA Companion | Apache-2.0 | ⚠️ | — | **❌ 탈락** |
| App Usage Tracker | 비공개 | ✅ | CSV + 이메일 | 오픈소스 아님 |

### 왜 ActivityWatch 가 1순위인가

기능이 좋아서가 아니라 **우리 파이프라인이 그대로 재사용되기 때문**이다.
노트북에서 쓰는 `collect/aw_sync.py`·`aw_client.py`·롤업이 그대로 붙는다.
앱이 `aw-server-rust` 를 내장하고 있어서 폰 자체가 작은 aw-server 가 된다.

### ★ HA Companion 을 탈락시킨 근거

공식 이슈에 이렇게 적혀 있다:

> the last used app reported by the sensor is **nearly all the time the Home
> Assistant app itself** — 데이터를 보내려고 HA 앱이 깨어나므로 가장 최근
> 사용 기록이 HA 자신이 된다
>
> the 'last used app' sensor only polls/updates **every 15 minutes**

우리 슬롯은 10분이다. **15분 폴링으로는 원리적으로 못 채운다.**

---

## 3. ★ 전송 — Syncthing 이 제약 셋을 동시에 만족한다

공식 설명에서 확인한 것:

> Syncthing is encrypted, free, open source and **works behind firewalls and
> CGNAT connections without the need for any additional port forwarding or
> network configuration**
>
> Because the encryption is **end-to-end**, any relay servers used are
> **unable to read your data**. 릴레이는 연결 메타데이터(ID·IP·포트·전송량)만
> 다루고 파일은 저장하지 않는다

| 제약 | Syncthing |
|---|---|
| 셀룰러 (LAN 불가) | ✅ 릴레이 경유로 동작 |
| VPN 안 씀 | ✅ 자체 프로토콜에 암호화 내장 |
| 젯슨 인터넷 노출 | ✅ **불필요** — 아웃바운드 연결만 |
| 제3자가 내용을 봄 | ✅ 못 봄 (E2E) |

**폰은 상시 연결될 필요도 없다.** 사용량 앱이 폰에 로컬로 쌓으므로, 며칠 뒤에
동기화돼도 데이터는 사라지지 않는다.

### 대안이었던 것들 (더 나쁨)

| | 왜 밀렸나 |
|---|---|
| Cloudflare Tunnel | 젯슨을 공개 인터넷에 노출. 받기 전용 엔드포인트로 좁혀도 노출은 노출 |
| 공유기 포트포워딩 + DDNS | 위와 같고 더 위험 |
| 수동 내보내기 + 클라우드(카톡·메일) | 제3자 서버를 거친다 — 데이터 원칙 위반 |
| 집 Wi-Fi 직결 | 사용자가 셀룰러만 쓴다 |

---

## 4. ⚠️ 미확인 — 여기가 갈림길이다

### 4-1. aw-android 의 내보내기가 불안정하다

저장소 이슈에서 확인:

- **#228** — "Export button in bucket view" 가 **조용히 실패(silently fails)**
- **#186** — CSV/JSON export via WebAppInterface + FileProvider (PR)
- **#220** — Sync 가 **Android 16 에서 SIGABRT 로 앱을 죽인다** (`libaw_sync.so`)
- **#221** — Sync Settings 를 네비게이션에서 접근 가능하게

내보내기 기능은 **있는데 동작이 보장되지 않는다.** 실물로 확인해야 한다.

### 4-2. 자동 내보내기가 되는지 모른다

수동 버튼만 있으면 매일 손이 간다. 대안:
- 며칠에 한 번 수동으로 눌러도 된다 (데이터는 폰에 쌓여 있다)
- 또는 폰에서 `localhost:5600/api/0/export` 를 주기적으로 호출 (Termux 등)

### 4-3. ActivityWatch 공식 동기화는 못 믿는다

공식 문서:

> **Syncing** is one of the most requested features for ActivityWatch.
> It is currently being worked on and **is in a testing phase.**

그래서 앱 내장 동기화가 아니라 **Syncthing 으로 파일을 옮기는** 우회를 택했다.

---

## 5. 확인 순서 (앱 두 개, 약 30분 + 하루 대기)

```
1. ActivityWatch 설치 (F-Droid 또는 Play)  →  사용 정보 접근 권한 허용
2. 하루 두고 데이터가 쌓이는지 확인
3. ★ 내보내기 버튼이 실제로 파일을 만드는지 (#228 이 고쳐졌는가)
4. 되면 Syncthing 설치 → 그 폴더를 젯슨과 연결
```

3번에서 막히면 usageDirect → Screen Time 순으로 같은 확인을 한다.
셋 다 안 되면 그때 직접 만드는 것을 논의한다.

---

## 6. 우리 쪽에 필요한 작업 (실물 데이터를 본 뒤에)

| | 문제 | 근거 |
|---|---|---|
| **1** | `base_url` 이 **하나뿐** | `config/lifetrainer.toml` 의 `[activitywatch] base_url` 단일값. 기기 여러 대를 받으려면 확장 필요 |
| **2** | ★ **안드로이드엔 AFK 워처가 없다** | 롤업의 `active_sec` 은 afk 버킷에서 나온다. 폰은 afk 개념이 없어 그대로 두면 **폰 활동이 전부 `away` 로 분류**된다. "화면이 켜져 있고 앱이 떠 있으면 활동" 규칙이 필요하다 |
| **3** | 버킷 타입 판별 | `aw_sync.bucket_type()` 이 ID 에서 `window`/`afk`/`web` 을 찾는다. 안드로이드 버킷 ID 에는 없고 메타의 `currentwindow` 로 폴백돼야 하는데 **실물 확인 필요** |

**2번이 제일 크다.** 어제 만든 `rollup.absorb_short_switches` 와 같은 층에 들어간다.

---

## 7. 지금 폰 데이터 없이도 되는 것 (중요)

**"코딩 10분 → 폰 3분 → 코딩 10분 = 코딩 23분" 은 이미 동작한다.**
`rollup.absorb_short_switches` 가 5분 미만 공백을 앞 활동으로 흡수한다.

폰 데이터는 **"5분 이상 자리를 비운 게 폰 때문인지 다른 일 때문인지"** 를
구분하는 데만 쓰인다. 그 구분의 가치가 위 작업량과 미확인 위험을 넘는지는
실물 확인 후에 다시 판단한다.

---

## 8. 출처

- [ActivityWatch aw-android](https://github.com/ActivityWatch/aw-android)
- [ActivityWatch — Exporting data](https://docs.activitywatch.net/en/latest/features/exporting-data.html)
- [ActivityWatch — Syncing (testing phase)](https://docs.activitywatch.net/en/latest/features/syncing.html)
- [usageDirect (F-Droid)](https://f-droid.org/en/packages/godau.fynn.usagedirect/) · [소스](https://codeberg.org/fynngodau/usageDirect)
- [Screen Time (F-Droid)](https://f-droid.org/packages/com.atharok.screentime/)
- [HA Companion — last used app 이슈 #2126](https://github.com/home-assistant/android/issues/2126)
- [HA Companion — Sensors 문서](https://companion.home-assistant.io/docs/core/sensors/)
- [Syncthing](https://www.alternativeto.net/software/syncthing/about/)
