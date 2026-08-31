# OpenClaw 에이전트 게이트웨이 — 로컬 LLM 결합 기록

> 작성일: 2026-08-15 / **갱신: 2026-08-24 — §7 신설 (Life Trainer 를 에이전트로 결합)**
> 상태: **구축·검증 완료, 게이트웨이 가동 중 (Slack 채널만 꺼짐).
> 그 위에 `lifetrainer` 에이전트가 올라가 있다 — §7**
> 관련: [llm-runtime.md](llm-runtime.md) · [llm-models.md](../../measure/findings/llm-models.md) ·
> [performance.md](../../measure/findings/performance.md)
> 실행 자산: [openclaw-setup/](../../life-trainer/deploy/)

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

Slack 앱은 [slack-app-manifest.json](../../life-trainer/deploy/slack-app-manifest.json)
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

생성 속도는 [Qwen3-8B-Q4KM.depth.txt](../../measure/results/Qwen3-8B-Q4KM.depth.txt) 의
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
→ [patch-cron-schema.sh](../../life-trainer/deploy/patch-cron-schema.sh)

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
[llm-models.md](../../measure/findings/llm-models.md) 의 30B 툴 벤치 실패 유형과 같다.

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

### 10-B. ★ 시스템 Node 로 **절반만** 옮겨져 있었다 (2026-08-28)

`use-system-node.sh` 를 돌린 뒤 게이트웨이는 시스템 Node 로 **실행**된다.
그런데 실행하는 **스크립트는 여전히 nvm 안**이다:

```
ExecStart=/usr/local/bin/node \
          /home/user/.nvm/versions/node/v22.23.2/lib/node_modules/openclaw/dist/index.js
          ↑ 시스템          ↑ 여기가 안 옮겨졌다
```

`openclaw` CLI 도 같다 (위임이 이걸 부른다). **nvm 을 갈아엎으면 둘 다 죽고**,
증상은 *"에이전트가 툴을 안 부른다"* 하나뿐이다.

`lt doctor` 가 CLI 경로만 보고 있어서 **절반만 옮겨진 상태가 안 드러났다.**
지금은 게이트웨이 `ExecStart` 도 같이 본다 — 그리고 그 조회가 실패하면
**실패했다고 말한다.** 조용히 "괜찮음" 으로 넘어가면 이번에도 안 드러난다.

나머지 절반을 옮기는 것 (sudo 필요, **rollback 절차가 스크립트 안에 있다**):

```bash
bash life-trainer/deploy/install-openclaw-system.sh   # ★ sudo 로 감싸지 않는다
```

★ **`sudo bash` 로 돌리면 실패한다.** root 의 PATH 에는 `openclaw` 가 없고
`systemctl --user` 는 사용자 버스에 못 붙는다 — **읽어야 하는 상태는 사용자 것이고
설치만 root 가 필요하다.** 스크립트가 내부에서만 `sudo npm` 을 쓴다.

★ **Node 메이저가 같이 바뀐다** (nvm v22 → 시스템 v24). 변수가 둘이 되는데,
게이트웨이는 **이미 시스템 Node 로 돌고 있어** 데몬 경로는 확인됐다. 안 해 본 것은
CLI 경로뿐이고 smoke test 3번이 그걸 본다.

★ **nvm 설치본을 먼저 지우지 않는다.** 둘이 공존하는 동안에만 되돌릴 수 있고,
시스템 쪽이 며칠 멀쩡히 돈 뒤에 지운다. 그리고 **버전을 고정해서 옮긴다** —
옮기는 김에 올리면 무엇이 원인인지 못 가른다.

### ✅ 2026-08-28 옮겼다 — 그 과정에서 나온 함정 둘

```
게이트웨이  /usr/local/bin/node /usr/local/lib/node_modules/openclaw/dist/index.js
위임 CLI    /usr/local/bin/openclaw
lt doctor   OK 17 / WARN 0 / FAIL 0
smoke       "오늘 계획이 뭐야?" → 계획·달성률 정상 응답 (Node 24)
```

**① `use-system-node.sh` 가 방금 한 일을 되돌렸다.** 그 스크립트는 안에서
`openclaw gateway install --force` 를 부르는데, **PATH 로 찾으면 nvm 쪽이 잡힌다**
(PATH 에서 nvm 이 `/usr/local/bin` 보다 앞이다). 그러면 유닛 `ExecStart` 에 nvm 경로가
다시 박힌다. 시스템 설치를 끝낸 직후 그 스크립트를 돌렸다가 정확히 그렇게 됐다.
지금은 `/usr/local/bin/openclaw` 를 **우선해서** 부른다.

**② `command -v openclaw` 는 설치 후에도 nvm 을 가리킨다** — 같은 PATH 순서 때문이다.
그래서 위임 경로를 PATH 에 맡기지 않고 절대경로로 고정했다:

```toml
# config/lifetrainer.toml
[agent]
openclaw_bin = "/usr/local/bin/openclaw"
```

**둘 다 "설치했으니 됐다" 와 "실제로 그게 불린다" 가 다른 경우다.** 이 저장소가
반복해서 겪은 부류 — 살아 있다 ≠ 서빙 가능하다, 툴을 불렀다 ≠ 일을 했다 — 와 같다.

### nvm 설치본 삭제 — 2026-08-28 같은 날 지웠다

원래 "며칠 멀쩡히 돈 뒤에" 지울 생각이었는데, 그 전에 **양쪽 경로를 실제로 재서**
Node 24 에서 도는 것을 확인했으므로 앞당겼다 (아래 §측정).

```bash
rm -f  ~/.nvm/versions/node/v22.23.2/bin/openclaw
rm -rf ~/.nvm/versions/node/v22.23.2/lib/node_modules/openclaw   # 370MB
```

**Node 22 자체는 남겼다** — `@playwright` 가 거기 붙어 있고 `measure/tools/gh-research*.mjs`
와 디자인 캡처가 그걸 쓴다. 지웠으면 그 도구가 조용히 죽었을 것이다.

지운 뒤 확인: 셸의 `openclaw` 가 `/usr/local/bin` 으로 바뀌고, 게이트웨이 재기동 ·
위임 · `lt doctor` 17/17 · `verify-boot` 전부 통과.

**되돌리려면** (시스템 쪽이 나중에 깨지면):
```bash
PATH=$HOME/.nvm/versions/node/v22.23.2/bin:$PATH npm install -g openclaw@2026.7.1-2
```

### ★ 측정 — "경로가 시스템이다" 와 "런타임이 시스템이다" 는 다르다

`/usr/local/bin/openclaw` 의 shebang 은 `#!/usr/bin/env node` 다. **경로가 아니라
PATH 가 런타임을 정한다.** 그래서 옮긴 직후의 smoke test 는 개발 셸에서 돌아
**Node 22 로 실행됐고**, "Node 24 에서 확인했다" 는 말은 틀렸었다.

| 경로 | 런타임 | 어떻게 쟀나 |
|---|---|---|
| 게이트웨이 | v24.19.0 | `readlink -f /proc/<MainPID>/exe` |
| **Slack 위임** | v24.19.0 | `delegate.ask()` 를 부르고 **그 파이썬 프로세스의 자식**을 잡아 `/proc/<pid>/exe` |
| 개발 셸 | (삭제 전) v22 | PATH 에서 nvm 이 앞이었다 |

운영이 24 인 이유는 두 겹이다 — `lifetrainer-slack.service` 의 PATH 에 nvm 이 없고,
`delegate.py` 가 그 앞에 `/usr/local/bin` 을 한 번 더 덧붙인다.

**재는 동안 함정 둘을 밟았다:**
1. `pgrep -f openclaw` 가 **측정 스크립트 자신**을 잡았다 — 루트 README 가
   *"`pkill -f` 자기매칭"* 으로 적어 둔 그것이다
2. 그다음엔 **항상 떠 있는 게이트웨이**를 잡았다. CLI 자식이 아니다

→ **부모 PID 의 자식만 본다.** `pgrep -P <파이썬 PID>` 로 좁히면 둘 다 안 걸린다.

---

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
llama-server.service     enabled, active   Qwen3-8B Q4_K_M, -c 20480, RSS 6.7 GB
llama-embed.service      enabled, active   Qwen3-Embedding-0.6B, CPU, :8081, 1.8 GB
openclaw-gateway.service enabled, active   292 MB
8080 포트                모델 id `qwen3-8b` 응답 중
channels.slack.enabled   false             ← Life Trainer 가 소켓을 가져갔다
contextWindow            20480             ← 서버의 -c 와 **반드시** 같아야 한다
```

> ### 2026-08-23 에 바뀐 것 두 가지
>
> **① ctx 40960 → 20480.** KV(q8_0)는 토큰당 76.5 KiB 라 40,960 이면 2.99GB 를 시동 시
> 전액 선불한다 — 대화 길이와 무관하다. `-c 40960` 은 `n_ctx_train` 상한이라서 고른
> 값이었지 필요한 값이 아니었다. Life Trainer 한 턴 실측 최악이 2,980 토큰이고
> OpenClaw 압축 설정(`maxHistoryShare 0.7` + `reserveTokens 6000`)이 재계산 없이
> 성립하는 하한이 20,480 이다 (14,336 + 6,000 = 20,336 < 20,480).
> 드롭인 `operate/systemd/llama-server.service.d/ctx.conf` 로 `LLAMA_CTX` 를 준다.
> **`openclaw.json` 의 `contextWindow` 도 같이 20480 으로 내렸다** — §4-2 그대로,
> 한쪽만 바꾸면 조용히 잘린다. 되돌리기: `~/.openclaw/openclaw.json.bak-ctx20480`.
>
> **② 게이트웨이가 08-23 02:14 에 내려갔다가 17:16 에 복귀했다.** 그날 밤 임베딩
> 서버(1.8GB)를 들이면서 메모리를 내준 것으로 보인다(정상 종료, 기록 없음).
> ctx 축소로 4.5GB 가 확보돼 지금은 **임베딩 서버와 게이트웨이가 동시에** 돈다.
>
> **③ ★ 임베딩 서버가 샌다 — 상한을 올리면 이 서버(8B)가 죽는다.** 같은 날 밤,
> `llama-embed` 의 `MemoryMax` 를 3G → 4G 로 올렸더니 스왑 7.8GB 를 전부 먹고
> 8B 가 OOM 으로 재시작했다(가용 112MB, 대화가 503). 유휴 1.8GB 인데 요청 약
> 1,380건 뒤 6.7GB 였고 재시작하니 즉시 1.8GB 로 돌아왔다 — 누수다.
> `MemoryMax=2500M` + `MemorySwapMax=0` 으로 되돌렸다.
> **이 서버의 안정성이 그 유닛의 상한에 달려 있다** — 임베딩 쪽을 손볼 때 같이 본다
> ([HISTORY](../../life-trainer/HISTORY/2026-08-23-raising-the-memory-cap-made-it-worse.md)).

> ★ **Slack 소켓은 워크스페이스 앱당 하나뿐이다.** 둘 다 켜면 서로 뺏는다.
> 지금은 [Life Trainer](../../life-trainer/HANDOFF.md) 가 갖고 있고, 게이트웨이는
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

- **지속 부하 발열 미측정** — [performance.md](../../measure/findings/performance.md) 의
  빈칸과 동일. 24시간 가동 설계에 필요하다.
- **에이전트 능동 발송 미검증** — §4-13. `message` 툴의 실제 가용 여부.
- **cron 실전 미검증** — dist 패치 후 Slack 에서 예약 동작을 확인하지 않았다.
- **readiness 체크 결함 미수정** — §4-8. 모델 id 확인이 빠져 있다.
- **A3B 백엔드 미시도** — 메모리상 어렵다.
  30B-A3B @ 40,960 + 게이트웨이 ≈ 14GB > 가용 13.4GB. 들어가더라도
  시스템 프롬프트 12.5K 때문에 모든 턴이 깊은 컨텍스트에서 시작하는데,
  거기는 8B 가 A3B 를 앞서는 구간이다(9,603 깊이에서 7.80 vs 7.76, 이후 격차 확대).
  툴 콜링도 6/6 vs 4/6. **"할 수 있지만 할 이유가 없다."**

---

## 7. Life Trainer 를 에이전트로 붙이기 (2026-08-24)

§5 까지는 "게이트웨이가 살아 있다" 였다. 여기서부터는 **그 위에 우리 능력을 올린
기록**이다. 코드는 [`life-trainer/lifetrainer/agent/`](../../life-trainer/lifetrainer/agent/),
설치는 [`scripts/install-agent.sh`](../../life-trainer/scripts/install-agent.sh).

> **층 구분은 여기 없다.** *"에이전트가 정하는 것 / 앱이 이미 정한 것 / 그 사이 경계"* 는
> 판정이라 [`life-trainer/docs/architecture.md §2-7`](../../life-trainer/docs/architecture.md) 에 있다.
> 이 문서는 **어떻게 붙였나**(재현·함정)를 다룬다.

### 7-1. 구성

```
openclaw-gateway :18081
  └─ agent "lifetrainer"      workspace: life-trainer/data/agent/workspace
       model  llamacpp/qwen3-8b          (llama-server :8080 공유)
       tools  allow ["lt__*"]            ← 절대 허용목록. exec·write 등 기본 툴이 사라진다
       │
       └─ MCP stdio "lt"     .venv/bin/python -m lifetrainer.agent.mcp_server
            slash · read_file · list_dir · write_file
            get_plans · get_activity_summary · search_docs · compare_days
            schedule_reminder · list_reminders · cancel_reminder
            web_search · fetch_url
```

**MCP 서버는 의존성이 0 이다.** 줄 단위 JSON-RPC 2.0 이고 쓰는 메서드가
`initialize`·`tools/list`·`tools/call`·`ping` 넷뿐이라 SDK 를 안 들였다.

**툴 정책은 에이전트별로 건다.** `mcp.servers` 는 전역이라 `main` 에이전트도 우리 툴을
받게 되는데, `main` 쪽에 `tools.deny = ["lt__*"]` 를 걸어 턴마다 2,976 토큰을 안 물게 했다.

### 7-2. 프롬프트를 12,541 → 5,247 토큰으로

§3 의 12,541 토큰(첫 턴 41.6초)이 출발점이었다. 지금은 이렇다.

| | 토큰 | 비고 |
|---|---|---|
| OpenClaw 골격 | 약 1,400 | 우리가 못 줄이는 부분 |
| 툴 스키마 13개 | 2,976 | `lt agent budget` (상한 3,000, 테스트가 지킨다) |
| 워크스페이스 `AGENTS.md` | 973 | 상한 1,000 |
| **합계** | **약 5,247** | 첫 호출 프롬프트 처리 **17.8초** (295 tok/s) |

실측 확인: 게이트웨이 트레이스의 첫 모델 호출이 `input 2,370 + cacheRead 2,842 = 5,212`.

**큰 것 두 개를 줄였다.**

#### ★ `agents add` 가 깔아 두는 인격 파일 6개 — 지우면 다시 생긴다

새 에이전트를 만들면 워크스페이스에 `SOUL.md`·`IDENTITY.md`·`USER.md`·`TOOLS.md`·
`HEARTBEAT.md`·`BOOTSTRAP.md` 가 깔린다. **6,122바이트고 게이트웨이가 전부 시스템
프롬프트에 넣는다.** 범용 비서를 상정한 내용("너는 누구인가", 기억 파일 관리,
하트비트)이라 우리 에이전트에는 해가 된다 — 특히 `BOOTSTRAP.md` 는 "정체를 찾고
이 파일을 지워라"라고 지시해서 첫 턴에 모델이 자기소개를 시작한다.

**지웠더니 재기동 때 다시 생겼다.** 없는 파일을 시드하기 때문이다. 이 저장소가
이미 두 번 밟은 부류다 — `nvpower.sh` 가 부팅마다 심링크를 되돌리고,
`daemon install` 이 유닛을 다시 만든다.

→ **비워서 남긴다.** 한 줄짜리 안내문으로 덮어쓰면 파일이 존재하므로 시드가 안 돈다.
`lt agent prompt` 가 이걸 한다. `BOOTSTRAP.md` 만은 원래 한 번 쓰고 지우는 파일이라
진짜로 지운다.

#### 워크스페이스 `AGENTS.md` 7,196 → 1,311바이트

기본 파일은 범용 비서용이다. 우리 것은 **코드가 만든다**
(`agent/prompt.py`) — 슬래시 명령표는 `slash.COMMANDS` 에서, 움직일 수 있는 폴더는
살아 있는 `Sandbox` 에서 가져온다. 손으로 베끼면 같은 값을 두 곳에서 관리하게 된다.

**결과: 시스템 프롬프트 23,744자 → 10,455자. 첫 턴 auto-compaction 0회**
(전에는 1회 — §4-3 의 그 실패였다).

### 7-3. ★ `experimental.localModelLean` 은 켜지 마라 (8B 기준)

이름이 정확히 우리 상황을 가리켜서 켜 봤다. 골격을 23,744 → 16,927자로 줄여 준다.

**대신 툴 13개가 메타툴 3개로 바뀐다** — `tool_call`·`tool_search`·`tool_describe`.
모델이 먼저 툴을 검색하고 그 다음에 `tool_call` 로 감싸 부르는 지연 로딩 구조다.

8B 가 그 간접층을 못 넘었다.

```json
{"name": "tool_call", "arguments": {"id": "get_plans", "args": {"date": "today"}}}
```

`tool_call` 을 **툴 이름 자체로** 착각해 호출이 실패했고(`failures: 1`), 모델은
사용자에게 *"`/lt today` 를 실행해보세요"* 라고 답했다 — §4-6 의 실패 모드다.

**툴이 20개를 넘는 에이전트를 위한 기능이다.** 우리 툴은 이미 13개 2,976 토큰이라
줄일 것보다 잃을 것이 크다. 왕복도 한 번 더 는다.

### 7-4. 감옥은 우리가 짠다 — `tools.fs.workspaceOnly` 를 안 믿는 이유

1. **경계가 우리 것이 아니다.** 설정 하나가 바뀌면 같이 움직인다.
2. ★ **MCP 툴은 게이트웨이의 fs 정책을 거치지 않는다.** 우리 프로세스가 직접
   파일을 연다. 여기서 안 막으면 아무도 안 막는다.

`agent/sandbox.py` 가 `realpath` 로 접은 뒤 containment 를 본다(문자열 비교 금지 —
`/data` 허가가 `/dataX` 를 통과시킨다). 읽기(`docs/ HISTORY/ config/ data/agent/`)와
쓰기(`data/agent/`)를 나누고, **허용 폴더 안이어도 비밀은 이름으로 거부한다**
(`config/lifetrainer.toml` 에 Slack 토큰과 Serper 키가 있다. `*.bak-*` 백업본도).

### 7-5. 8B 가 여기서 낸 실패 4가지 (전부 실측)

§4-6 의 "하겠다고 말하고 끝낸다"가 형태를 바꿔 계속 나온다. **툴 이름만 채점하면
전부 통과한다** — 답을 읽어야 보인다.

| 증상 | 원인 | 손본 곳 |
|---|---|---|
| `/view` 로 목록만 보고 **완료는 사용자에게 시켰다** | §4-6 그대로 | 채점기가 "바꾼 흔적"을 요구 (`require_slash`) |
| 허용 폴더에 이름을 **이어 붙여** `…/data/agent/config` 를 부르고 "config 폴더는 없다"고 답했다 | §4-4 (`workspace/workspace/`) 와 같은 부류 | 못 찾았을 때 결과에 "짧은 이름을 쓰라"를 붙이고 스키마에도 박음 |
| **총 활동 시간을 "코딩 시간"이라고** 답했다 | 모델만의 잘못이 아니었다 — `compare_days` 가 **증감만** 주고 카테고리별 절대값을 안 줬다. 붙일 숫자가 없으니 눈에 보이는 걸 붙였다 | 툴이 `gaming 4시간 56분→52분(-4시간 4분)` 으로 양쪽을 싣는다 |
| "내일 계획 넣어줘" 가 **오늘에** 들어갔다. 에러 없음 | `/plan` 에 날짜 자리가 없어 모델이 `@2026-08-25`(기간 토큰)에 얹었고 조용히 버려졌다 | `day` 인자 추가. **낱말로** 받는다 — 하루가 06:00 에 시작해 모델이 계산하면 이틀 뒤가 나온다 |

**교훈은 하나로 모인다: 프롬프트로 안 되는 것은 구조로 막는다.** 넷 중 셋은
프롬프트가 아니라 **툴 결과와 스키마**를 고쳐서 잡았다 — 모델이 스스로 고칠 수 있는
자리가 거기이기 때문이다.

### 7-6. 메모리 — 상주 13.9MB, 천장 약 100MB, 누수 없음

| 시점 | MCP 서버 RSS |
|---|---|
| 유휴 (초기화 직후) | **13.9 MB** |
| 슬래시 첫 호출 뒤 | 74.1 MB (`slack_bolt` +27MB, 리포트 모듈) |
| RAG 검색 뒤 | 91.5 MB (numpy) |
| 슬래시 400회 뒤 | **88.3 MB** (100 → 104 → 89 → 88) |

**지연 import 가 이 숫자를 만든다.** `report.planner`(matplotlib)를 import 하면
+44MB 라서 `/view`·`/week` 은 에이전트에게 **PNG 를 안 그린다** — 텍스트 채널이라
어차피 못 읽는다. 400회 뒤에 오히려 줄어든 것이 §5-③ 임베딩 서버 누수와의 대조점이다.

★ 다만 **`openclaw agent` CLI 한 번이 node 프로세스 350MB** 다. §6 의 "게이트웨이 WS
직결" 이 아직 유효한 숙제인 이유다.

### 7-6b. ★ 프롬프트는 **매번 재주입되지 않는다** — 캐시는 이미 돈다

"시스템 프롬프트 5.3K 를 턴마다 다시 처리하니까 느린 것 아니냐"는 자연스러운 의심을
실측으로 확인했다. **아니다.**

같은 세션에서 연달아 세 번 물었을 때 (게이트웨이 트레이스의 usage):

| | 새로 처리 | 캐시 적중 | 턴 전체 |
|---|---|---|---|
| 턴1 | 2,953 | 8,108 | 45.9초 |
| 턴2 | **802** | 11,791 | 43.0초 |
| 턴3 | **265** | 6,577 | **15.1초** |

llama.cpp 가 공통 접두사를 KV 캐시에서 재사용한다. 더 중요한 것은 **턴1의
`cacheRead 8,108`** 이다 — 그건 **완전히 새 세션의 첫 턴**이었다. 이전 세션이 남긴
KV 가 그대로 맞은 것이다. 시스템 프롬프트가 안 변하기 때문이고,
`agent/prompt.py` 가 프롬프트를 **코드에서 생성해 고정**시킨 것이 여기서 값을 한다 —
매번 조금씩 달라지면 이 적중이 안 난다.

**그러면 왜 여전히 느린가.** 턴2 는 새로 처리한 것이 802 토큰(≈2.7초)뿐인데 43초였다.
나머지는 **생성**이다 — 8B 가 9.4 tok/s 라 답변 255 토큰이 약 27초다.
거기에 `openclaw` CLI(node) 기동 약 8초.

> **프롬프트 캐시를 더 손봐도 큰 이득이 없다.** 걷을 것은 이미 걷었고,
> 남은 병목은 **생성 속도(모델 크기)와 CLI 기동**이다.

안 켜져 있는 llama.cpp 플래그가 둘 있다 (`llama-server-qwen3.sh`):

| 플래그 | 판단 |
|---|---|
| `--cache-reuse N` | 켤 만하다. 프롬프트가 **중간에서** 갈릴 때도 KV 시프트로 재사용한다 — 압축이 걸리거나 다른 채널이 끼어든 뒤에 이득. **미검증** |
| `--slot-save-path` | 슬롯이 1개뿐이라 이득이 작다 |
| `--parallel 2` (슬롯 증설) | ❌ **하면 안 된다.** 슬롯 수가 KV 를 배수로 잡아 ctx 가 20,480 → 10,240 이 된다. 시스템 프롬프트만 5.3K 라 §4-3 의 압축 지옥으로 되돌아간다 |

### 7-7. 채점기

`scripts/eval_agent.py` — **툴 선택으로 채점한다.** 답변 문장을 문자열로 채점하면
맞는 답이 틀린 답이 되는 것을 음성 쪽에서 이미 겪었다("이십사도"→"24도").

★ **툴 인자는 응답 JSON 에 없다.** `toolSummary.tools` 가 `["lt__slash"]` 라고만
알려 줘서, 그걸로 채점하면 위 표의 1·4번이 통과한다. 인자는 게이트웨이 트레이스
(`~/.openclaw/agents/<id>/sessions/*.trajectory.jsonl`)에만 있어서 거기까지 읽는다.

```bash
.venv/bin/python scripts/eval_agent.py --trials 2     # 케이스 9개 × 2회, 약 20분
```
