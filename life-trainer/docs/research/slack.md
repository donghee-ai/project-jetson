# Slack Bolt for Python 조사 — NAT 뒤 온디바이스 봇

> 조사일: 2026-08-16 / 대상: Socket Mode, 공인 IP 없음, Python 3.10

---

## 1. 패키지

| 패키지 | 버전 | Python | 비고 |
|---|---|---|---|
| `slack_bolt` | **1.30.0** (2026-07-15) | >=3.7 | `slack_sdk>=3.38.0,<4` 를 자동으로 끌고 온다 |
| `slack_sdk` | **3.43.0** (2026-06-30) | >=3.7 | 기본 설치에 **필수 의존 없음** |

**`websocket-client` 는 필요 없다.** 동기 `App` + `SocketModeHandler` 는
`slack_bolt.adapter.socket_mode.builtin` 을 쓰고, 이건 slack_sdk 자체 내장 소켓 클라이언트
(`slack_sdk.socket_mode.builtin`)로 동작한다. `pip install slack_bolt` 하나면 끝.

`websocket-client` / `aiohttp` / `websockets` 는 **다른 어댑터를 일부러 골랐을 때만** 필요하다
(`adapter.socket_mode.websocket_client` 등, import 줄이 다르다).

---

## 2. 최소 구성

```python
import os
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

app = App(token=os.environ["SLACK_BOT_TOKEN"])        # xoxb-…

if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()   # xapp-…
```

| 토큰 | 접두어 | 용도 | 발급 위치 |
|---|---|---|---|
| Bot token | `xoxb-` | Web API 호출 | 앱 → **OAuth & Permissions** → Install to Workspace 후 상단 |
| App-level token | `xapp-` | `apps.connections.open` (WSS URL 획득) | 앱 → **Basic Information** → **App-Level Tokens** → Generate Token and Scopes → 스코프 `connections:write` |

**Socket Mode 토글**도 사이드바 **Socket Mode** 에서 따로 켜야 한다.

---

## 3. 앱 매니페스트

생성 경로: **api.slack.com/apps** → **Create New App** → **From an app manifest**
→ 워크스페이스 선택 → JSON 붙여넣기 → Next → Create.

```json
{
  "display_information": {"name": "Life Trainer"},
  "features": {
    "bot_user": {"display_name": "life-trainer", "always_online": true},
    "slash_commands": [
      {"command": "/lt",  "description": "Life Trainer 조회", "usage_hint": "today | week | status", "should_escape": false},
      {"command": "/log", "description": "활동 수동 기록",     "usage_hint": "운동 60m 헬스장",       "should_escape": false}
    ]
  },
  "oauth_config": {
    "scopes": {"bot": ["chat:write", "commands", "files:write", "im:history", "im:write", "users:read"]}
  },
  "settings": {
    "event_subscriptions": {"bot_events": ["app_mention", "message.im"]},
    "interactivity": {"is_enabled": true},
    "org_deploy_enabled": false,
    "socket_mode_enabled": true,
    "token_rotation_enabled": false
  }
}
```

`interactivity.is_enabled` 는 슬래시 커맨드만 쓸 거면 필수는 아니지만,
`response_url` 배관을 공유하고 나중에 버튼·모달을 붙일 때 필요하므로 켜 둔다.

---

## 4. 앱이 둘일 때 — 우리 상황 (OpenClaw 앱이 이미 있음)

**서로 다른 두 앱은 각자의 app-level 토큰으로 독립된 Socket Mode 연결을 갖는다.**
`app_id`, 봇 토큰, 앱 토큰이 전부 별개고 연결 상태를 공유하지 않는다. 간섭이 없다.

**같은 앱 토큰으로 두 프로세스를 띄우면 (사고 시나리오)**:

- 앱 토큰당 WebSocket 연결은 **최대 10개**까지 허용된다
- 문서 직접 인용: *"When multiple connections are active, each payload may be sent to
  **any** of the connections. It's best not to assume any particular pattern for how
  payloads will be distributed across multiple open connections."*
- → **라운드로빈도 아니고 중복도 아니다. 예측 불가.**
- 새 연결이 열려도 **기존 연결이 자동으로 끊기지 않는다** (10개 상한까지 공존)
- Slack 이 의도한 다중 연결 용도는 재시작 시 무중단(새 연결 열고 옛 것 닫기)이지 부하 분산이 아니다

> **결론: 앱 토큰 하나당 프로세스 하나.** systemd 로 `lifetrainer-slack.service` 를
> 돌린다면 인스턴스가 둘이 되지 않도록 해야 한다.

---

## 5. 파일 업로드 — v1 은 완전히 폐기됐다

```python
result = client.files_upload_v2(
    channel="C0123456789",        # 채널 ID. 봇이 이미 멤버여야 한다
    file="/path/report.png",      # 또는 content=bytes/str
    filename="report.png",
    title="Daily report",
    initial_comment="오늘 리포트",
    thread_ts=None,
)
file_id = result["file"]["id"]    # 'F…'
```

- 필요 스코프: **`files:write`**. 파일을 언급하는 메시지도 보낸다면 `chat:write` 추가
- **함정**: 봇이 대상 채널의 멤버가 아니면 `not_in_channel` 로 실패하거나 조용히 공유가 안 된다.
  공개 채널이면 `conversations.join`, 아니면 사람이 초대해야 한다
- **v1 `files.upload` 는 끝났다.** 2024-04 폐기 예고 → 2024-05-16 이후 생성된 앱은 호출 불가
  → 2025-03-11 부터 순차 종료 → **2025-11-12 완전 차단.**
  `files_upload_v2` 는 내부적으로 `files.getUploadURLExternal` + `files.completeUploadExternal` 을 엮는다

---

## 6. ★ Block Kit — 업로드한 PNG 를 카드 안에 넣을 수 있다

이미지 블록은 `image_url` 대신 **`slack_file`** 객체를 받는다.
`{"id": "F0123456"}` 또는 `{"url": "https://files.slack.com/files-pri/…"}`.
**공개 URL 이 필요 없다.** 둘 중 하나만 줘야 하고(동시 지정 금지), png/jpg/jpeg/gif 만 된다.
봇이 그 파일에 접근 권한이 있어야 한다(자기가 올린 파일이면 충족).

```json
{"blocks": [
  {"type": "header", "text": {"type": "plain_text", "text": "일일 리포트 — 2026-08-16"}},
  {"type": "section", "fields": [
    {"type": "mrkdwn", "text": "*활동 시간*\n7시간 20분"},
    {"type": "mrkdwn", "text": "*커버리지*\n86%"}
  ]},
  {"type": "context", "elements": [{"type": "mrkdwn", "text": "Life Trainer · 23:30 KST"}]},
  {"type": "image", "block_id": "timeline",
   "title": {"type": "plain_text", "text": "144슬롯 타임라인"},
   "slack_file": {"id": "F0123456"},
   "alt_text": "하루 타임라인"}
]}
```

**실행 순서**: `files_upload_v2()` → `result["file"]["id"]` → 그 ID 를 `slack_file` 에 넣어
`chat.postMessage`.

---

## 7. 슬래시 커맨드

```python
@app.command("/log")
def handle_log(ack, respond, command):
    ack()                                  # 3초 안에. 인자 없으면 빈 200
    respond(f"기록: {command['text']}")     # 기본 ephemeral
```

- **`ack()` 는 3초 안에.** 넘기면 사용자에게 실패로 보인다
- `respond()` — 그 인터랙션의 `response_url` 로 보낸다. 기본 `ephemeral`,
  `{"response_type": "in_channel"}` 로 공개 전환
- `say()` — `chat.postMessage` 로 해당 채널에 일반 메시지
- `client.chat_postMessage()` — 원시 호출. `thread_ts`·`blocks` 자유롭고 시간 제약 없음
- **`response_url` 한도: 30분 이내 최대 5회.** 그 뒤엔 `chat.postMessage` 로 가야 한다

**느린 작업**은 ack 후 별도 스레드/큐로 넘긴다. Bolt 의 lazy listener 도 있으나
(`app.command("/log")(ack=…, lazy=[…])`) FaaS 지향 기능이므로,
Socket Mode 상시 프로세스에서는 `ack()` → `threading.Thread` → `respond()`/`chat_postMessage` 가
단순하고 확실하다.

---

## 8. 능동 발송

```python
# 채널
client.chat_postMessage(channel="C0123456789", text="…")     # 봇이 멤버여야 함

# DM
im = client.conversations_open(users="U0123456789")           # 1~8명 (1명=DM)
client.chat_postMessage(channel=im["channel"]["id"], text="…")
```

`conversations.open` 에는 `im:write` 필요. 반환 채널 ID 는 `D…` 형태.

`chat.scheduleMessage`: **최대 120일 후**까지 예약 가능(넘으면 `time_too_far`).
같은 채널에 **5분 창 안에서 30건**까지. `metadata` 파라미터를 쓴 예약 메시지는 게시되지 않는다.

---

## 9. 함정

| 항목 | 내용 |
|---|---|
| **레이트리밋** | `chat.postMessage` 는 번호 티어가 아닌 "Special tier". 실무 기준 **채널당 초당 1건** 정도. 짧은 버스트는 용인 |
| **재연결** | Slack 이 몇 시간마다 연결을 갱신한다(`disconnect`, `reason: refresh_requested`, 가능하면 ~10초 전 예고). `SocketModeHandler` 가 자동 재연결하므로 별도 재시도 로직 불필요. 다만 같은 WS 가 영원할 거라 가정하지 말 것 |
| **systemd 종료** | `handler.close()` 로 정리한다. **내장 시그널 핸들러는 없다** — SIGTERM/SIGINT 를 직접 걸어야 한다 |
| **블로킹 핸들러** | builtin 어댑터는 스레드 풀로 디스패치하며 기본 동시성 10. 핸들러가 수 초씩 붙잡으면(예: 로컬 LLM 호출) 풀이 마르고 다른 이벤트가 밀린다. 무거운 일은 큐로 넘길 것 |

```python
import signal
def _bye(sig, frame):
    handler.close()
    raise SystemExit(0)
signal.signal(signal.SIGTERM, _bye)
signal.signal(signal.SIGINT, _bye)
handler.start()
```

> 시그널 핸들러 패턴과 `concurrency=10` 기본값은 공식 문서에 명시된 것이 아니라
> 문서화된 `close()` + 어댑터 소스에서 유추한 것이다. **미확인.**

---

## 참고 URL

- Bolt Socket Mode: https://docs.slack.dev/tools/bolt-python/concepts/socket-mode/
- Events API Socket Mode: https://docs.slack.dev/apis/events-api/using-socket-mode
- 앱 매니페스트: https://docs.slack.dev/app-manifests/configuring-apps-with-app-manifests/
- 파일 업로드: https://docs.slack.dev/tools/python-slack-sdk/tutorial/uploading-files/
- `files.upload` 폐기: https://docs.slack.dev/changelog/2024-04-a-better-way-to-upload-files-is-here-to-stay/
- 이미지 블록: https://docs.slack.dev/refs/reference/block-kit/blocks/image-block/
- 비공개 파일 이미지 블록: https://slack.com/blog/developers/uploading-private-images-blockkit
- 슬래시 커맨드: https://docs.slack.dev/tools/bolt-python/concepts/commands/
- `conversations.open`: https://docs.slack.dev/refs/reference/methods/conversations.open/
- `chat.scheduleMessage`: https://docs.slack.dev/refs/reference/methods/chat.scheduleMessage/
- 레이트리밋: https://docs.slack.dev/apis/web-api/rate-limits/

---

## ★ 매니페스트 파일을 고쳐도 Slack 은 안 바뀐다 (2026-08-19)

슬래시 명령의 **사용법 힌트**(입력창에 뜨는 회색 예시)는 Slack 쪽 앱 설정에 있다.
저장소의 `config/slack-app-manifest-merged.json` 은 **사본**이다.

실제로 예시를 대학생 기준으로 바꿨는데(`/plan … #프로젝트 @90m !high`) Slack 입력창에는
며칠 전 예시(`#수학 @60m !high`)가 그대로 떠 있었다. 사용자가 보고 알려줬다.

적용하는 두 가지 방법:

| | 방법 | 필요한 것 |
|---|---|---|
| 손 | api.slack.com/apps → 앱 → **App Manifest** → 붙여넣기 → Save | 없음 |
| 명령 | `SLACK_CONFIG_TOKEN=xoxe-... bash scripts/apply-slack-manifest.sh` | **설정 토큰**(xoxe-) |

**봇 토큰(xoxb-)으로는 앱 설정을 못 바꾼다.** 설정 토큰은 api.slack.com/apps 하단
"Your App Configuration Tokens" 에서 발급하고 **12시간** 유효하다. 저장소에 넣지 않는다.

스크립트는 적용 전에 현재 매니페스트를 `data/backup/` 에 내려받는다 — 되돌릴 길을
만들어 두고 바꾼다. 권한(scope)이 바뀌면 **재설치**가 필요하고, 힌트·설명만 바뀌면
재설치 없이 바로 반영된다.

> 같은 부류를 하루에 두 번 밟았다: systemd 서비스도 파일만 고치고 재기동을 안 해
> 옛 코드가 돌고 있었다. **파일을 고치는 것과 실물이 바뀌는 것은 다르다.**
