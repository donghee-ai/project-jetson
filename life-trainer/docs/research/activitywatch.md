# ActivityWatch 조사 — 원격 폴링 관점

> 조사일: 2026-08-16 / 근거: 문서가 아니라 **소스 코드**(`aw-server-rust`, `aw-watcher-window`,
> `aw-watcher-afk`, `aw-client`) 직접 확인. 공식 문서는 스스로 불완전하다고 밝히고 있다.

---

## 1. 버전 — 베타를 쓴다

| 트랙 | 버전 | 배포일 | Windows 인스톨러 |
|---|---|---|---|
| 안정 | `v0.13.2` | 2024-10-05 | `activitywatch-v0.13.2-windows-x86_64-setup.exe` |
| **베타 (권장)** | **`v0.14.0b3`** | 2026-07-28 | `activitywatch-v0.14.0b3-windows-x86_64-setup.exe` |

다운로드: `https://github.com/ActivityWatch/activitywatch/releases/download/<태그>/<파일명>`

**베타를 권장하는 이유는 하나다 — API 키 인증이 안정판에 없다.**
Bearer 토큰 인증은 2026-04-17 에 병합되어 `v0.14.0b1` 부터 존재한다.
네트워크에 노출할 거라면 이게 있고 없고의 차이가 크다.

> `v0.14.0b3-research`("Research Edition")는 프라이버시 필터 연구용 빌드다. **쓰지 말 것.**
> Tauri 트레이 버전(`activitywatch-tauri-…`)도 병행 배포되나 자동시작·설정 경로가
> 동일한지는 미확인. 레거시 PyQt 인스톨러를 쓴다.

기본 워처: `aw-watcher-window`(활성 창 앱·제목), `aw-watcher-afk`(입력 유휴 판정).
둘 다 `aw-qt` 가 관리한다.

**자동시작은 기본값으로 켜져 있다.** 인스톨러(`activitywatch-setup.iss`)의
`[Tasks] StartMenuEntry` 에 `unchecked` 플래그가 없어 체크된 상태로 나오고,
`{userstartup}` 에 `aw-qt.exe` 바로가기를 만든다.
나중에 켜려면 `shell:startup` 에 바로가기를 직접 넣는다.

---

## 2. REST API

```
GET    /api/0/info                                        ← 인증 면제 (헬스체크용)
GET    /api/0/buckets/                                    → {bucket_id: Bucket}
GET    /api/0/buckets/<id>                                → Bucket
GET    /api/0/buckets/<id>/events?start=&end=&limit=      → [Event]
GET    /api/0/buckets/<id>/events/count?start=&end=       → 정수 (JSON 객체 아님)
POST   /api/0/query/                                      → [Value] (timeperiod 당 1개)
```

`start`/`end` 는 **RFC3339 필수**. 형식이 틀리면 400.

### 이벤트 스키마

```json
{"id": 123, "timestamp": "2026-08-16T10:00:00.123456Z", "duration": 4.5, "data": {…}}
```

- `timestamp` — **항상 UTC**. 문서 명시: *"all timestamps are stored as UTC.
  Timezone information (UTC offset) is currently discarded."*
- `duration` — **float 초** (내부는 ns 정밀도)
- `id` — **서버가 부여**. 클라이언트가 만들지 않는다. 버킷 간 연속성·예측 가능성 없음

### 버킷 ID 와 data 스키마

| 워처 | 버킷 ID | `data` |
|---|---|---|
| window | `aw-watcher-window_<HOSTNAME>` | `{"app": "chrome.exe", "title": "…"}` |
| afk | `aw-watcher-afk_<HOSTNAME>` | `{"status": "afk" \| "not-afk"}` |
| web | `aw-watcher-web-chrome_<HOSTNAME>` | `{"url", "title", "audible", "incognito"}` |

**afk 의 status 는 이 두 값뿐이다.** 포럼에 도는 "hibernating" 상태는 현재 소스에 없다.

### 쿼리 API

```json
{
  "query": ["events = query_bucket(\"aw-watcher-window_HOST\");", "RETURN = events;"],
  "timeperiods": ["2026-08-16T00:00:00+00:00/2026-08-16T23:59:59+00:00"]
}
```

`query` 는 **줄 단위 문자열 배열**. `timeperiods` 는 `시작/끝` 을 슬래시로 이은 **문자열 하나**
(쌍이 아니다). 응답은 timeperiod 당 하나씩 든 배열.

주요 함수: `flood()`(작은 틈 메움), `filter_period_intersect(events, filter)`(겹치는 구간만),
`merge_events_by_keys(events, keys)`(키별 병합·합산), `filter_keyvals(events, key, [vals])`.

### ★ 증분 동기화 수단이 없다

**웹훅도, 커서도, ETag 도 없다.** 클라이언트가 `start`/`end` 로 직접 페이징해야 한다.
엔드포인트 목록에 sync 토큰 비슷한 것이 아예 존재하지 않는다.

---

## 3. ★ 하트비트 병합 — 이중 계산의 원인이자 해법

`aw-transform/src/heartbeat.rs` + `aw-datastore/src/datastore.rs` 확인 결과:

워처는 하트비트를 계속 보낸다. 서버는 **`heartbeat.data == last_event.data`** 이고
**`heartbeat.timestamp ∈ [last_event.timestamp, last_event_end + pulsetime]`** 이면 병합한다.

병합 시:

```
timestamp  →  그대로 (last_event 것 유지)
id         →  그대로 (datastore.rs: "Use the event ID from last_event
                      to ensure we update the correct row")
duration   →  증가          ← 이것만 변한다
```

> **버킷의 마지막(진행 중) 이벤트는 `id` 와 `timestamp` 가 고정이고 `duration` 만 자란다.**

### 여기서 나오는 결론

**`end` 타임스탬프 기준으로 페이징하면 안 된다.**
`start=<지난 폴링의 끝>` 으로 조회하면, 아직 진행 중이던 활동이 다음 폴링에서
**새로운 별개 이벤트처럼** 잡혀 하나의 활동이 조각나고 이중 계산된다.

### 안전한 증분 동기화

1. 버킷마다 마지막으로 본 이벤트의 **`timestamp`** 를 기억한다 (끝 시각이 아니다).
2. 매 폴링에서 `start = 마지막_본_timestamp - 여유(5~15분)`, `end = now` 로 **겹쳐서** 다시 가져온다.
3. **`(bucket_id, timestamp)` 로 upsert** 한다. 병합이 시작 시각을 옮기지 않으므로 자연 키가 된다.
   (`id` 로 해도 되지만, id 는 서버 구현에 묶여 있어 timestamp 쪽이 안전하다.)
4. 시간 여유를 두는 이유는 시계 오차와 하트비트의 순서 뒤바뀜 방어다.

완전히 닫힌 이벤트는 재폴링해도 새 id 로 다시 나오지 않는다.
**중복 위험은 오직 "진행 중인 마지막 이벤트" 하나뿐이고, upsert 로 완전히 해소된다.**

---

## 4. 네트워크 노출 (Windows)

### 설정 파일 경로

```
%LOCALAPPDATA%\activitywatch\aw-server-rust\config.toml
```

> 오래된 문서의 `aw-server.toml` 은 파이썬 서버 시절 이름이다. 현행 Rust 서버는 `config.toml`.
> 과거 버그(#1068)로 `activitywatch\activitywatch\…` 처럼 경로가 중복되는 사례가 있었다.
> 예상 경로가 비어 있으면 한 단계 더 들어간 경로도 확인할 것.

### TOML 키 — `host` 가 아니라 `address` 다

```toml
address = "127.0.0.1"    # ← 키 이름이 address. host 가 아니다
port = 5600
cors = []
cors_regex = []

[auth]
api_key = "…"            # 선택. v0.14.0b1+ 에만 존재
```

CLI 플래그는 `--host <주소>`, `--port`, `--config`, `--dbpath`, `--testing`, `--verbose`.
**플래그 이름(`--host`)과 TOML 키 이름(`address`)이 다르다.**

### 인증 — "없다"고 단정하면 틀린다

2026-04-17 병합(PR #585, 이후 #588·#636 에서 퍼센트 인코딩·이중 슬래시 우회 취약점 보강,
마지막 수정 2026-07-28) 이후 Bearer 토큰 인증이 **선택적으로** 존재한다.

- `[auth] api_key` 설정 → 모든 `/api/*` 가 `Authorization: Bearer <키>` 를 요구
- **예외: `GET /api/0/info` 는 항상 공개** (헬스체크용)
- 기본값은 미설정 → 인증 완전 비활성 (기본 설치 상태에서는 무인증이 맞다)
- **안정판 v0.13.2 에는 없다.** v0.14.0b1 부터
- **PyPI `aw-client==0.5.15` 는 Authorization 헤더를 보낼 코드 경로 자체가 없다.**
  git master 조차 *로컬* 서버(`127.0.0.1`/`localhost`/`::1`)의 키만 자동 로드하고
  원격 호스트에는 명시적으로 거부한다 (`aw_client/config.py::load_local_server_api_key`)

### Host 헤더 검사는 비-loopback 바인드에서 꺼진다

`endpoints/hostcheck.rs` 의 DNS 리바인딩 방어는 **`address` 가 `127.0.0.1`/`localhost` 일 때만
동작**한다. `0.0.0.0` 이나 특정 IP 로 바꾸는 순간 경고 로그만 남기고 조용히 비활성화된다.
방어 계층 하나가 사라지므로 **비-loopback 바인드는 `api_key` 와 짝지어야 한다.**

### 권장 노출 방식 (우선순위)

1. **Tailscale 인터페이스 IP 에 직접 바인드**: `--host 100.x.y.z --port 5600`.
   포트가 tailnet 에서만 도달 가능해진다. "0.0.0.0 + 방화벽 규칙" 보다 안전하다 —
   잘못 설정될 수 있는 부품(방화벽 규칙)이 아예 없어지므로.
2. 그와 별개로 **`[auth] api_key` 를 설정**하고 폴러가 Bearer 헤더를 보낸다.
3. 굳이 `0.0.0.0` 이어야 한다면 Windows 방화벽 인바운드 규칙을
   `RemoteIP=100.64.0.0/10`(Tailscale CGNAT 대역)으로 좁히고,
   그 외 전부를 막는 명시적 deny 규칙을 같이 넣는다.
4. 평문 HTTP 다 (aw-server-rust 에 TLS 없음). **Tailscale 이 WireGuard 로 감싸므로
   그대로 두는 것이 맞다.** 별도 TLS 를 얹지 말 것.

CORS 는 `http://127.0.0.1:<port>`, `http://localhost:<port>`, 고정 Chrome 확장 ID,
그리고 **모든 `moz-extension://`** 를 허용한다. 서버-대-서버 폴러에는 무관하지만,
문서 스스로 *"Firefox 에서는 악성 확장이 데이터스토어 전체를 가져갈 수 있다"* 고 경고한다.

---

## 5. aw-watcher-web (브라우저 확장)

- Chrome/Edge: `https://chromewebstore.google.com/detail/activitywatch-web-watcher/nglaklhklhcoonedhgnpgddginnjdadi`
- Firefox: `https://addons.mozilla.org/en-US/firefox/addon/aw-watcher-web/`
- 이벤트 타입 `web.tab.current`, `data = {url, title, audible, incognito}`
- 별도 페어링·토큰 없음. 로컬 `http://localhost:5600` 에 붙는다 (CORS 화이트리스트에 있음)
- 버킷 ID 는 서버가 `!local` 관례로 호스트명을 채워 넣어
  최종적으로 `aw-watcher-web-chrome_<실제호스트명>` 형태가 된다

---

## 6. Python 클라이언트 — 쓰지 않는다

`aw-client` 0.5.15 (2025-01-09):

```python
ActivityWatchClient(client_name="…", testing=False, host=None, port=None, protocol="http")
get_events(bucket_id, limit=-1, start=None, end=None)   # ← 인자 순서 주의
```

**원격 폴러에는 그냥 `requests` 를 쓰는 게 맞다.**

1. 0.5.15 에 API 키 인증 지원이 **전혀 없다** — 몽키패치나 포크가 필요해진다
2. `persist-queue`, `aw-core`, `tomlkit` 등을 끌고 오는데, 로컬 큐잉·단일 인스턴스 락 같은
   기능은 무상태 원격 폴러에 무의미하고 오히려 깨지기 쉽다
3. 필요한 REST 표면(`/buckets/`, `/buckets/<id>/events`, `/query/`)이 너무 단순하다

---

## 7. Windows 워처의 실제 동작

`aw_watcher_window/windows.py` (master):

```python
win32gui.GetForegroundWindow()          # 활성 창 핸들
win32gui.GetWindowText(hwnd)            # 제목
win32api.OpenProcess(0x0400, …) + win32process.GetModuleFileNameEx(…)   # 앱 이름
```

| 사안 | 내용 |
|---|---|
| **관리자 권한 프로세스** | 워처가 비승격 상태면 승격된 프로세스에 대한 `OpenProcess` 가 거부된다. 커밋 `2d878c5` 에서 WMI(`Win32_Process`) 폴백을 되살렸으나 **보장은 아니다.** 승격 앱에서 앱명·제목이 간헐적으로 비는 것을 예상할 것 |
| **UWP / 스토어 앱** | 이슈 #31 — 실제 앱이 아니라 호스트 프로세스 `ApplicationFrameHost.exe` 로 잡힌다. 오래된 미해결 문제 |
| `exclude_title` 옵션 | Windows 에서 버그 이력(#38). 제목 마스킹은 이걸 믿지 말고 **소비자 쪽에서** 처리할 것 |
| **확장 없이 브라우저 탭** | 창 제목에 활성 탭의 페이지 제목이 들어오긴 한다. 그러나 **URL·audible·incognito 는 없고 탭 구분도 안 된다.** URL 이 필요하면 `aw-watcher-web` 이 필수 |

### 슬립/최대절전

특별 처리가 없다. 하트비트가 멈췄다가 재개되면 `last_event_end + pulsetime` 을 넘겨
병합되지 않고 **새 이벤트**가 삽입된다. 그 사이는 버킷 타임라인의 **빈 구간(gap)** 으로 남는다.
"자는 중" 상태 이벤트는 만들어지지 않는다.
→ 롤업에서 gap 은 `off`(PC 꺼짐/수면)로 해석하면 된다.

### 시계 오차

이벤트 타임스탬프는 **Windows 기기 자신의 시계**로 찍힌다. 서버가 보정하지 않는다.
폴러와 PC 의 시계가 벌어지면 경계 구간에서 누락·중복이 생긴다.
Windows 쪽에 `w32tm` 동기화를 확인할 것.

---

## 8. 미확인

- v0.14.0b3 신규 설치 시의 실제 설정 파일 경로를 실제 Windows 기기에서 검증하지 않았다
  (소스와 문서가 일치하나 실측은 아님)
- 경로 중복 버그(#1068)가 현행 인스톨러에서 재현되는지
- Tauri 빌드의 자동시작·설정 경로가 레거시와 동일한지

---

## 참고 URL

- 릴리스: https://github.com/ActivityWatch/activitywatch/releases
- 버킷/이벤트 문서: https://github.com/ActivityWatch/docs/blob/master/src/buckets-and-events.rst
- 서버 설정: https://github.com/ActivityWatch/aw-server-rust/blob/master/aw-server/src/config.rs
- 하트비트 병합: https://github.com/ActivityWatch/aw-server-rust/blob/master/aw-transform/src/heartbeat.rs
- API 키: https://github.com/ActivityWatch/aw-server-rust/blob/master/aw-server/src/endpoints/apikey.rs
- Host 검사: https://github.com/ActivityWatch/aw-server-rust/blob/master/aw-server/src/endpoints/hostcheck.rs
- Windows 워처: https://github.com/ActivityWatch/aw-watcher-window/blob/master/aw_watcher_window/windows.py
- 보안 문서: https://docs.activitywatch.net/en/latest/security.html
