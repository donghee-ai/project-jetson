# OpenClaw 에이전트 게이트웨이 — 로컬 LLM 결합 기록

> 작성일: 2026-08-15 / **갱신: 2026-08-21 — §5 현재 상태 정정**
> 상태: **구축·검증 완료, 게이트웨이 가동 중 (Slack 채널만 꺼짐)**
> 관련: [llm-runtime.md](llm-runtime.md) · [llm-models.md](../../research/llm-models.md) ·
> [performance.md](../../research/performance.md)
> 실행 자산: [openclaw-setup/](../../openclaw-setup/)

---

## 0. 결론 먼저

**OpenClaw 2026.7.1-2 를 로컬 llama.cpp(Qwen3-8B)에 붙여 완전 온디바이스 에이전트를
구성하고 Slack 으로 연결했다. 외부 API 호출 없음.**

그 과정에서 얻은 결론은 하나로 모인다.

> **로컬 소형 모델을 에이전트로 쓸 때 병목은 모델 성능이 아니라
> 프레임워크의 프롬프트 비용과 스키마 호환성이다.**

| 관찰 | 값 |
|---|---|
| 프레임워크 시스템 프롬프트 | **12,541 토큰** |
| 그중 툴 하나(`cron`)가 차지 | **3,912 토큰** (툴 전체의 44%) |
| Qwen3-8B 기준 실제 대화 가용 | **약 28K 토큰** |
| 에이전트 경유 응답 지연 | 25~40초 (직접 질의 3.9초) |
| 툴 스키마 비호환으로 인한 전체 실패 | 정규식 1개가 원인 |

---

## 1. 구성

```
openclaw CLI 2026.7.1-2   (npm -g / 런타임은 /usr/local/bin/node v24.19.0)
  │
  ├─ Slack (Socket Mode, 아웃바운드)   ← 인바운드 포트 불필요
  │
  └─ openclaw-gateway.service   127.0.0.1:18081   (loopback, token auth)
       │  Requires= / After=
       └─ llama-server.service  127.0.0.1:8080
            └─ Qwen3-8B-Q4_K_M
               -ngl 99 -c 40960 --parallel 1 -fa on -ctk/-ctv q8_0 --jinja
```

설정 `~/.openclaw/openclaw.json` · 워크스페이스 `~/.openclaw/workspace`

Socket Mode 는 Slack 쪽으로 나가는 연결이라 게이트웨이를 외부에 열 필요가 없다.
LAN/Tailscale 노출을 한 번 구성했다가 **되돌렸다** — Slack 만 쓸 거면 불필요하다.

---

## 2. 재현 절차

```bash
# 1. CLI (nvm 기반이라 sudo 불필요)
npm install -g openclaw

# 2. 비대화형 온보딩 — 로컬 llama.cpp 를 custom provider 로
openclaw onboard --non-interactive --accept-risk \
  --mode local --flow quickstart \
  --auth-choice custom-api-key \
  --custom-provider-id llamacpp \
  --custom-base-url http://127.0.0.1:8080/v1 \
  --custom-model-id qwen3-8b \
  --custom-compatibility openai --custom-text-input \
  --gateway-bind loopback --gateway-auth token --gateway-port 18081 \
  --skip-channels --skip-daemon --skip-ui --skip-search --skip-health

# 3. 초기값으로 두면 깨지는 것 3가지 (§4 함정 2·3·5)
openclaw config set models.providers.llamacpp.models.0.contextWindow 40960
openclaw config set agents.defaults.compaction.reserveTokens 6000
openclaw config set agents.defaults.compaction.maxHistoryShare 0.7
openclaw config set agents.defaults.compaction.keepRecentTokens 8000

# 4. 데몬 + 모델 백엔드 유닛 + linger
openclaw daemon install
bash openclaw-setup/install.sh

# 5. 버전 매니저 의존 제거 (§4 함정 8·9)
sudo bash openclaw-setup/install-system-node.sh
bash openclaw-setup/use-system-node.sh

# 6. cron 툴 호환 패치 (§4 함정 6)
bash openclaw-setup/patch-cron-schema.sh

# 7. Slack — 플러그인은 코어에 없다
openclaw plugins install @openclaw/slack
openclaw config set plugins.allow '["slack"]' --strict-json
openclaw config set channels.slack.appToken 'xapp-...'
openclaw config set channels.slack.botToken 'xoxb-...'
openclaw config set channels.slack.mode socket
openclaw config set channels.slack.enabled true
systemctl --user restart openclaw-gateway
```

Slack 앱은 [slack-app-manifest.json](../../openclaw-setup/slack-app-manifest.json)
을 붙여넣어 만든다. 플러그인이 내장한 공식 매니페스트를 추출한 것으로,
스코프 23개·이벤트 15개·Socket Mode·슬래시 커맨드가 한 번에 설정된다.

---

## 3. 실측

| 항목 | 값 |
|---|---|
| npm 설치 | 300 packages / 17초 |
| 모델 로드 | 약 10초 |
| **시스템 프롬프트** | **12,541 토큰** (워크스페이스 파일 7,199 + 툴 스키마) |
| 첫 턴 프롬프트 처리 | 41.6초 (295 tok/s) |
| 2턴째 (프롬프트 캐시 적중) | 400~1,200 토큰만 재처리 → 7.5초 |
| 생성 속도 | 7.17 tok/s @ 깊이 ~13K |
| **직접 질의 (llama-server)** | **3.9초** |
| **에이전트 경유** | **25~40초** |
| RAM (8B @ 40,960 + 게이트웨이) | 11,123 / 15,643 MB |

생성 속도는 [Qwen3-8B-Q4KM.depth.txt](../../results/Qwen3-8B-Q4KM.depth.txt) 의
깊이별 곡선(9.6K → 7.80, 16K → 6.58 tok/s)과 일치한다.
**게이트웨이를 얹어도 추론 성능 자체에는 손해가 없다.**

### 툴 토큰 분포 (23개, 실제 페이로드 캡처 후 측정)

```
cron              3,912   ← 전체의 44%
skill_workshop      662
sessions_spawn      509
exec                485
process             432
... (나머지 18개 합계 약 2,800)
────────────────────────
합계              8,833
```

### 동작 검증

| 항목 | 결과 |
|---|---|
| OpenAI 호환 툴 콜링 | ✅ |
| 파일 쓰기 / 셸 실행 | ✅ ("8코어 / 15,642MB" 실제 값 정확) |
| 멀티턴 세션 | ✅ 이전 턴 값 기억 |
| Slack 왕복 | ✅ |
| Slack 이벤트 기반 발송 | ✅ `openclaw message send` (에이전트 미경유) |
| 에이전트 능동 발송 | ⚠️ 미검증 |

---

## 4. 기록해둔 함정들

### 1. `--jinja` 없으면 툴 콜링이 통째로 안 된다

llama.cpp 는 기본 채팅 템플릿에서 tool call 을 파싱하지 않는다. `--jinja` 를 줘야
GGUF 의 모델 원본 템플릿을 쓰고 `tool_calls` 가 나온다.
[llm-runtime.md](llm-runtime.md) 의 벤치용 명령에는 이 플래그가 없다 —
벤치는 직접 프롬프트를 던져서 필요 없었지만 에이전트에는 필수다.

### 2. 컨텍스트는 40,960 이 천장 — 더 주면 조용히 잘린다

```
W llama_context: n_ctx_seq (49152) > n_ctx_train (40960) -- possible training context overflow
W srv    load_model: the slot context (49152) exceeds the training context (40960) - capping
```

Qwen3-8B 의 학습 컨텍스트가 40,960 이다. 문제는 **OpenClaw 쪽 `contextWindow` 는
그대로 49152 로 남는다**는 것 — 서버는 40960 만 갖고 있는데 클라이언트는 49152 인 줄
알고 프롬프트를 채운다. 끝에서 넘친다. **양쪽 숫자를 반드시 맞춰야 한다.**

### 3. 시스템 프롬프트 12.5K — 32K 컨텍스트에서는 첫 턴부터 압축된다

온보딩 직후 32,768 로 돌렸더니 **매 턴** auto-compaction 이 걸렸다.

```
[agent/embedded] auto-compaction start: reason=threshold
[agents/cli-compaction] compaction skipped: Already compacted
```

32K 의 38% 를 시작부터 먹으니 압축 임계에 즉시 닿는다. 히스토리가 매 턴 날아가
멀티턴이 성립하지 않는다. 컨텍스트를 40,960 으로 올리고 `reserveTokens` 를 6000 으로
낮추니 compaction 0회.

> **로컬 소형 모델에 에이전트 프레임워크를 얹을 때는 프레임워크의 시스템 프롬프트
> 크기를 먼저 재야 한다.** 모델 컨텍스트만 보고 결정하면 안 된다.

### 4. 8B 는 워크스페이스 상대 경로를 헷갈린다

"워크스페이스에 hello.txt 를 만들어라" → `workspace/workspace/hello.txt` 에 만들었다.
툴 호출과 내용은 정확했고 경로 해석만 틀렸다. 툴 콜링 6/6 이어도 경로·상태 추론은 별개다.

### 5. 툴 스키마의 정규식 하나가 요청 전체를 400 으로 만든다 ★

웹 UI 에서만 채팅이 깨지고 CLI 는 멀쩡한 증상이 났다. 원인은 llama.cpp 였다.

```
400 Unable to generate parser for this template. Automatic parser generation failed:
    JSON schema conversion failed: Pattern must start with '^' and end with '$'
```

`--jinja` 로 툴 콜링을 할 때 llama.cpp 는 툴 스키마를 GBNF 문법으로 변환하는데,
**앵커(`^`…`$`)가 없는 정규식이 하나라도 있으면 요청 전체를 거부한다.**

캡처 프록시로 실제 페이로드를 잡아 격리했다.

| 툴 구성 | 결과 |
|---|---|
| 원본 23개 | 400 |
| **cron 제거 (22개)** | **200** |
| cron 단독 (1개) | 400 |

범인은 `cron` 툴의 `job.declarationKey` 에 있는 `pattern: "\S"` 하나였다.

**앵커를 붙여도 안 된다.** llama.cpp 의 GBNF 변환기가 `\S` 자체를 못 다룬다.

```
pattern="\S"        → 400 Pattern must start with '^' and end with '$'
pattern="^\S+$"     → 400 Failed to initialize samplers: failed to parse grammar
pattern="^.*\S.*$"  → 400 Failed to initialize samplers: failed to parse grammar
pattern 제거         → 200 OK
```

제거해도 안전하다. `minLength: 1` 이 남고, 공백 문자열 거부는 런타임 코드가
이미 두 군데에서 한다(`"declarationKey must not be blank"`).
→ [patch-cron-schema.sh](../../openclaw-setup/patch-cron-schema.sh)

**CLI 는 왜 멀쩡했나** — `cron` 이 owner 전용 툴이라 CLI 발신자에게는 자동 제외됐다.
웹 UI(operator.admin)에는 실려서 깨졌다. **증상이 반쪽만 나타나 원인 추적이 어려웠다.**

이건 OpenClaw 만의 문제가 아니라 **로컬 llama.cpp 백엔드를 쓰는 모든 에이전트
프레임워크가 밟는 문제**다.

### 6. 소형 모델은 "하겠다"고 말하고 끝낸다 ★

같은 세션, 같은 모델에서 결과가 갈린 대조 실험.

| 지시 | 결과 |
|---|---|
| "바탕화면에 노래 가사 txt 만들어줘" | **툴 호출 0건.** 내용을 채팅에 출력하고 *"I'll create this as a text file"* 로 종료 |
| "`/절대/경로`에 써. 쓴 다음 `ls -la` 로 확인해서 보여줘" | **성공** (파일 시스템에서 독립 검증) |

앞의 경우 **에러가 안 난다.** 그럴듯한 문장이 돌아오는데 아무 일도 일어나지 않았다.
[llm-models.md](../../research/llm-models.md) 의 30B 툴 벤치 실패 유형과 같다.

> **성공률이 모델 성능이 아니라 프롬프트 형태에 좌우된다.**
> 검증을 요구하면 "하겠다"로 빠져나갈 수 없다 — 결과를 보여주려면 실제로 실행해야 하므로.

### 7. `daemon install` 만으로는 부팅에서 안 뜬다

**사용자** systemd 유닛(`~/.config/systemd/user/`)이라 기본값 `Linger=no` 에서는
로그인해야 시작된다. 헤드리스에서는 SSH 로 붙을 때까지 아무것도 안 뜬다.

```bash
loginctl enable-linger aisw
```

### 8. 게이트웨이만 데몬화하면 재부팅 후 전부 실패한다

llama-server 가 없으면 모든 턴이 실패한다. `Requires=` + `After=` 로 묶고,
llama-server 유닛에 **readiness 체크**를 넣어야 한다. `Type=exec` 만으로는 프로세스가
뜬 즉시 "시작됨"이 되어 모델 로드 10초 동안 게이트웨이가 먼저 붙는다.

```ini
ExecStartPost=/bin/sh -c 'until curl -sf http://127.0.0.1:8080/health >/dev/null; do sleep 2; done'
TimeoutStartSec=600
```

> ⚠️ **이 readiness 체크에는 결함이 있다.** 포트만 보고 *누가* 응답하는지는 안 본다.
> 다른 llama-server(4B)가 8080 을 선점한 상태에서 헬스체크는 통과하고 유닛은 계속
> 실패하는 모순이 발생했고, `Requires=` 로 묶인 게이트웨이가 재시작을 반복하다
> `start-limit-hit` 으로 죽었다(1시간 무중단 다운). **`/v1/models` 의 모델 id 까지
> 확인해야 한다.** (미수정)

### 9. 유닛 본체를 직접 고치면 다음 `daemon install` 에 날아간다

`openclaw daemon install` 은 `openclaw-gateway.service` 를 다시 생성한다.
의존 관계·PATH 변경은 전부 **드롭인**(`openclaw-gateway.service.d/*.conf`)에 넣어야 한다.

또 드롭인에 `ExecStart` 를 중복으로 박지 말 것. `dist/index.js` 경로가 하드코딩되어
openclaw 를 재설치하면 드롭인이 유닛을 깨뜨린다. ExecStart 는
`openclaw gateway install --force` 에 맡기고 드롭인에는 PATH 만 남긴다.

### 10. nvm node 를 가리키는 유닛 — 시스템 Node 가 필요하다

`dist/runtime-paths-*.js` 의 판정 기준:

- 시스템 Node 후보 (Linux): **`/usr/local/bin/node`, `/usr/bin/node` 뿐**
- version-manager 마커: `/.nvm/`, `/.fnm/`, `/.volta/`, `/.asdf/` … (realpath 기준)

`~/.local/node` 에 깔아도 인정 안 된다. `/usr/local` 에 공식 tarball 을 풀어야 하고
root 가 필요하다. Ubuntu 22.04 의 apt `nodejs` 는 **12.22.9** 라 못 쓴다
(OpenClaw 는 `node:sqlite` 와 WAL-reset-safe SQLite 때문에 22.22.3+ / 24.15+ 요구).

설치 후 **유닛을 다시 생성해야 한다.** `openclaw daemon install` 은 이미 설치돼 있으면
`already enabled` 로 넘어가므로 `openclaw gateway install --force` 가 필요하다.

### 11. 서비스 감사(audit)는 드롭인을 못 본다 — 경고 하나는 오탐

시스템 Node 로 전환해도 PATH 경고가 남는다. 그런데 실제 런타임은 깨끗하다.

```
$ systemctl --user show openclaw-gateway -p Environment
PATH=/usr/local/bin:/usr/bin:/bin:/home/user/.local/bin
```

`dist/inspect-*.js` 의 `detectMarkerLineWithGateway` 가 **`.service` 파일 텍스트를
직접 줄 단위로 파싱**한다. `systemctl show` 를 쓰지 않으므로 드롭인 override 가
감사에 잡히지 않는다.

> **확인은 `openclaw daemon status` 가 아니라 systemd 에게 물어야 한다.**
> `systemctl --user show openclaw-gateway -p ExecStart -p Environment`

### 12. Slack — 코어가 아니라 플러그인이다

```
openclaw plugins install @openclaw/slack
openclaw config set plugins.allow '["slack"]' --strict-json   # 미설정 시 경고
```

**DM 페어링 게이트가 있다.** 모르는 발신자의 DM 은 차단되고 봇 주인이 승인해야 한다.
셸이 열린 에이전트라 이 기본값이 옳다.

```
openclaw pairing approve slack <코드>
→ Approved slack sender U…
→ Command owner configured slack:U… (commands.ownerAllowFrom was empty)
```

**첫 승인자가 자동으로 명령 owner 가 된다.** 그리고 owner 는 owner 전용 툴을 받는데
`cron` 이 그중 하나다 — §4-5 의 400 이 Slack 에서도 그대로 재현되므로 패치가 선행돼야 한다.

### 13. `tools.profile: coding` 은 툴을 지운다

```
tool policy removed 5 tool(s) via tools.profile (coding):
  agents_list, gateway, message, nodes, tts
```

`message` 가 빠지면 **에이전트가 스스로 메시지를 보낼 수 없다.**
`tools.alsoAllow` 로 되살릴 수 있으나, 제거 목록에서 빠진 뒤에도
`allowlist contains unknown entries (message)` 경고가 남아 실제 가용 여부는 미확인이다.

이벤트 기반 발송은 **에이전트를 거치지 않는 경로가 더 안전하다.**

```bash
openclaw message send --channel slack --target "user:U…" -m "..."
# → ✅ Sent via Slack. Message ID: …
```

§4-6 의 실패 유형(하겠다고 말하고 안 함)이 원천적으로 불가능해진다.
Frigate 등 이벤트 소스를 붙일 때는 이 경로가 정답이다.

### 14. LAN 개방 — `bind` 만 바꾸면 웹 UI 가 거부된다

*(Slack 전용 구성으로 되돌렸으므로 현재는 해당 없음. 기록만 남긴다.)*

`gateway.bind = lan` 은 코드상 `0.0.0.0` 이라 유선·무선·`tailscale0` 을 한 번에 덮는다
(`tailnet` 을 고르면 오히려 LAN 이 빠진다). 그러나 non-loopback 바인드에서는
Control UI 오리진이 **loopback 2개만** 자동 시드된다.

```
gateway: seeded gateway.controlUi.allowedOrigins
  ["http://localhost:18081","http://127.0.0.1:18081"] for bind=lan
```

접근할 주소를 전부 `gateway.controlUi.allowedOrigins` 에 명시해야 한다.
**HTTP API 는 200 이 떠서 "열렸다"고 착각하기 쉽다** — 브라우저로 붙어봐야 안다.

또 `gateway.tailscale.mode=serve` 는 bind 가 loopback 이길 강제한다. LAN 과 양립하지
않는다. 다만 serve 는 tailscaled 가 `127.0.0.1:18081` 로 프록시하는 것이므로,
openclaw 에 알리지 않고 `tailscale serve --bg --https=443 18081` 로 직접 걸면
LAN 과 HTTPS 가 공존한다(operator 지정 필요: `sudo tailscale set --operator=$USER`).

---

## 5. 현재 상태 (2026-08-21 기준)

**스택은 살아 있다. 다만 Slack 채널만 OpenClaw 에서 떼어냈다.**

```
llama-server.service     enabled, active   Qwen3-8B Q4_K_M, -c 40960, RSS 10.2 GB
openclaw-gateway.service enabled, active   329 MB
8080 포트                모델 id `qwen3-8b` 응답 중
channels.slack.enabled   false             ← Life Trainer 가 소켓을 가져갔다
```

> ★ **Slack 소켓은 워크스페이스 앱당 하나뿐이다.** 둘 다 켜면 서로 뺏는다.
> 지금은 [Life Trainer](../../Life_Trainer/HANDOFF.md) 가 갖고 있고, 게이트웨이는
> 셸·파일 작업을 `openclaw agent --json` 으로 위임받을 수 있도록 살려만 뒀다.

`llama-server` 는 **두 프로젝트가 공유한다.** Life Trainer 는 전용 모델 서버를 띄우지
않는다 — 8B 스택 하나로 가용 메모리가 거의 찬다 (전체 15 GB 중 여유 3.9 GB).

### 2026-08-15 기록 (당시 상태)

작성 시점에는 유닛이 전부 내려가 있었고 8080 을 Qwen3-4B 가 수동으로 쓰고 있었다.
**8B 스택(11.1GB)과 4B(3.7GB)는 가용 13.4GB 안에서 공존할 수 없고 포트도 같다** —
이 제약 자체는 지금도 유효하다.

### Slack 을 OpenClaw 로 되돌리려면

```bash
systemctl --user disable --now lifetrainer-slack     # 먼저 소켓을 놓게 한다
openclaw config set channels.slack.enabled true
systemctl --user restart openclaw-gateway
```

스택 전체가 내려가 있는 상태에서 올릴 때는:

```bash
killall -q llama-server              # 다른 모델이 8080 을 쥐고 있으면 (pkill -f 는 자기매칭 주의)
bash openclaw-setup/install.sh       # 유닛 재등록 + 기동 + linger
systemctl --user enable --now openclaw-gateway
```

설정(`~/.openclaw/openclaw.json`)과 Slack 토큰은 그대로 남아 있으므로 재온보딩은 불필요하다.

---

## 6. 미완

- **지속 부하 발열 미측정** — [performance.md](../../research/performance.md) 의
  빈칸과 동일. 24시간 가동 설계에 필요하다.
- **에이전트 능동 발송 미검증** — §4-13. `message` 툴의 실제 가용 여부.
- **cron 실전 미검증** — dist 패치 후 Slack 에서 예약 동작을 확인하지 않았다.
- **readiness 체크 결함 미수정** — §4-8. 모델 id 확인이 빠져 있다.
- **A3B 백엔드 미시도** — 메모리상 어렵다.
  30B-A3B @ 40,960 + 게이트웨이 ≈ 14GB > 가용 13.4GB. 들어가더라도
  시스템 프롬프트 12.5K 때문에 모든 턴이 깊은 컨텍스트에서 시작하는데,
  거기는 8B 가 A3B 를 앞서는 구간이다(9,603 깊이에서 7.80 vs 7.76, 이후 격차 확대).
  툴 콜링도 6/6 vs 4/6. **"할 수 있지만 할 이유가 없다."**
