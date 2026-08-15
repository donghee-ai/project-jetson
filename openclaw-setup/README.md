# OpenClaw on Jetson — 완전 온디바이스 에이전트 게이트웨이

**OpenClaw 2026.7.1-2** 를 Jetson Orin NX 16GB 에 올리고, 모델 백엔드로
**로컬 llama.cpp (Qwen3-8B-Q4_K_M)** 를 붙인 기록. 외부 API 호출 없음.

구축일 2026-08-15.

---

## 왜 Qwen3-8B 인가

[research/llm-models.md](../research/llm-models.md) 의 툴 콜링 벤치 결과를 그대로 따랐다.

| 모델 | 툴 콜링 | 비고 |
|---|---|---|
| **Qwen3-8B Q4_K_M** | **6/6** | 채택 — 에이전트에는 툴 콜링이 전부다 |
| Qwen3-30B-A3B IQ2_M | 4/6 | 크지만 툴 콜링에서 밀린다 |
| EXAONE 3.5 7.8B | 2/6 | 한국어 문서용이지 에이전트용이 아니다 |

OpenClaw 는 매 턴 20개 안팎의 툴 스키마를 모델에 넘긴다. 툴 콜링이 안 되면
게이트웨이 자체가 무의미하다.

---

## 구조

```
openclaw CLI 2026.7.1-2   (npm -g, nvm node v22.23.2)
  │
  └─ openclaw-gateway.service      127.0.0.1:18081   (loopback, token auth)
       │  Requires= / After=
       └─ llama-server.service     127.0.0.1:8080    (OpenAI 호환 /v1)
            └─ Qwen3-8B-Q4_K_M.gguf
               -ngl 99 -c 40960 --parallel 1 -fa on -ctk/-ctv q8_0 --jinja
```

| 파일 | 역할 |
|---|---|
| `bin/llama-server-qwen3.sh` | 모델 서버 실행 래퍼 |
| `systemd/llama-server.service` | 모델 백엔드 유닛 |
| `systemd/openclaw-gateway.service.d/10-depends-llama.conf` | 게이트웨이 → 백엔드 의존 |
| `systemd/openclaw-gateway.service.d/20-system-node.conf` | 서비스 PATH 최소화 |
| `install.sh` | 유닛 등록 + linger (사용자 권한) |
| `install-system-node.sh` | `/usr/local` 에 Node 설치 (**root**) |
| `use-system-node.sh` | 게이트웨이를 시스템 Node 로 전환 |

설정 `~/.openclaw/openclaw.json` · 워크스페이스 `~/.openclaw/workspace`

---

## 재현 절차

```bash
# 1. CLI 설치 — nvm 기반이라 sudo 불필요
npm install -g openclaw

# 2. 비대화형 온보딩 — 로컬 llama.cpp 를 custom provider 로 등록
openclaw onboard --non-interactive --accept-risk \
  --mode local --flow quickstart \
  --auth-choice custom-api-key \
  --custom-provider-id llamacpp \
  --custom-base-url http://127.0.0.1:8080/v1 \
  --custom-model-id qwen3-8b \
  --custom-compatibility openai --custom-text-input \
  --gateway-bind loopback --gateway-auth token --gateway-port 18081 \
  --skip-channels --skip-daemon --skip-ui --skip-search

# 3. 컨텍스트 창을 모델 실제 한계에 맞춘다 (아래 함정 2)
openclaw config set models.providers.llamacpp.models.0.contextWindow 40960

# 4. compaction 완화 (아래 함정 3)
openclaw config set agents.defaults.compaction.reserveTokens 6000
openclaw config set agents.defaults.compaction.maxHistoryShare 0.7
openclaw config set agents.defaults.compaction.keepRecentTokens 8000

# 5. 게이트웨이 데몬 등록
openclaw daemon install

# 6. 모델 백엔드 유닛 + 의존 관계 + linger
bash openclaw-setup/install.sh

# 7. 버전 매니저 의존 제거 — Node 24.19.0 LTS 를 /usr/local 에
sudo bash openclaw-setup/install-system-node.sh
bash openclaw-setup/use-system-node.sh    # gateway install --force + PATH 드롭인
```

### 최종 상태 확인

`openclaw daemon status` 는 드롭인을 못 보므로(함정 9) systemd 에게 직접 묻는다.

```bash
systemctl --user show openclaw-gateway -p ExecStart -p Environment
# ExecStart  → /usr/local/bin/node ...
# PATH       → /usr/local/bin:/usr/bin:/bin:/home/user/.local/bin
```

---

## 실측

| 항목 | 값 |
|---|---|
| npm 설치 | 300 packages / 17초 |
| 모델 로드 | 약 10초 (페이지 캐시 온) |
| **OpenClaw 시스템 프롬프트** | **12,541 토큰** |
| 첫 턴 프롬프트 처리 | 42.6초 (294 tok/s) |
| 2턴째 (프롬프트 캐시) | 7.5초 |
| 생성 속도 | 7.17 tok/s @ 깊이 ~13K |
| llama-server RSS | 9.8 GB |
| 남는 메모리 | 4.0 GB |

생성 속도는 [results/Qwen3-8B-Q4KM.depth.txt](../results/Qwen3-8B-Q4KM.depth.txt) 의
깊이별 곡선(9.6K → 7.80 tok/s, 16K → 6.58 tok/s)과 정확히 일치한다. 게이트웨이가
얹혀도 추론 성능에 손해는 없다.

### 동작 검증

| 항목 | 결과 |
|---|---|
| OpenAI 호환 툴 콜링 | ✅ `get_weather` 호출 |
| 파일 쓰기 툴 | ✅ |
| 셸 툴 | ✅ "8코어 / 15,642MB" — 실제 값 정확 |
| 멀티턴 세션 | ✅ 이전 턴 값 기억 (8 → 16) |
| systemd 재구성 후 | ✅ |

---

## 기록해둔 함정들

### 1. `--jinja` 없으면 툴 콜링이 통째로 안 된다

llama.cpp 는 기본 채팅 템플릿에서 tool call 을 파싱하지 않는다. `--jinja` 를 줘야
GGUF 에 박힌 모델 원본 템플릿을 쓰고, 그래야 `tool_calls` 가 나온다.
[docs/llm-runtime.md](../docs/llm-runtime.md) 의 벤치용 실행 명령에는 이 플래그가
없다 — 벤치는 직접 프롬프트를 던졌기 때문에 필요 없었지만, 에이전트에는 필수다.

### 2. 컨텍스트는 40,960 이 천장이다 — 더 주면 조용히 잘린다

`-c 49152` 로 올렸더니 로그에만 경고가 뜨고 실제로는 40960 으로 잘렸다.

```
W llama_context: n_ctx_seq (49152) > n_ctx_train (40960) -- possible training context overflow
W srv    load_model: the slot context (49152) exceeds the training context of the model (40960) - capping
```

Qwen3-8B 의 학습 컨텍스트가 40,960 이다. 문제는 **OpenClaw 쪽 `contextWindow` 는
49152 로 남아 있었다**는 것 — 서버는 40960 만 갖고 있는데 클라이언트는 49152 인 줄
알고 프롬프트를 채운다. 끝에서 넘친다. 양쪽 숫자를 반드시 맞춰야 한다.

40,960 이상이 필요하면 YaRN(`--rope-scaling yarn`)을 켜야 하고 품질 저하가 따라온다.

### 3. 시스템 프롬프트가 12.5K — 32K 컨텍스트에서는 첫 턴부터 압축된다

onboard 직후 32,768 로 돌렸더니 **매 턴** auto-compaction 이 걸렸다.

```
[agent/embedded] embedded run auto-compaction start: reason=threshold
[agents/cli-compaction] CLI transcript compaction skipped: Already compacted
```

원인은 OpenClaw 의 시스템 프롬프트 12,541 토큰 —
워크스페이스 파일(AGENTS.md 7KB, SOUL.md, TOOLS.md, IDENTITY.md …) + 툴 스키마 약 20개.
32K 의 38% 를 시작부터 먹고 들어가니 compaction 임계에 즉시 닿는다.
히스토리가 매 턴 날아가서 멀티턴이 성립하지 않는다.

컨텍스트를 40,960 으로 올리고 `reserveTokens` 를 6000 으로 낮추니 compaction 0회.
**로컬 소형 모델에 에이전트 프레임워크를 얹을 때는 프레임워크의 시스템 프롬프트
크기를 먼저 재야 한다.** 모델 컨텍스트만 보고 결정하면 안 된다.

### 4. 8B 는 워크스페이스 상대 경로를 헷갈린다

"워크스페이스에 hello.txt 를 만들어라" → `~/.openclaw/workspace/workspace/hello.txt`
에 만들었다. 툴 호출과 내용은 정확했고 경로 해석만 틀렸다. 툴 콜링 6/6 이어도
경로·상태 추론은 별개다.

### 5. `daemon install` 만으로는 부팅에서 안 뜬다

`openclaw daemon install` 은 **사용자** systemd 유닛(`~/.config/systemd/user/`)을
만든다. 기본값 `Linger=no` 에서는 로그인해야 시작된다 — 헤드리스 Jetson 에서는
SSH 로 붙을 때까지 아무것도 안 뜬다는 뜻이다.

```bash
loginctl enable-linger aisw    # Linger=yes
```

### 6. 게이트웨이만 데몬화하면 재부팅 후 전부 실패한다

게이트웨이는 뜨는데 llama-server 가 없으면 모든 턴이 실패한다. `Requires=` +
`After=` 로 묶고, llama-server 유닛에 **readiness 체크**를 넣어야 한다.
`Type=exec` 만으로는 프로세스가 뜬 즉시 "시작됨"이 되어 모델 로드 10초 동안
게이트웨이가 먼저 붙는다.

```ini
ExecStartPost=/bin/sh -c 'until curl -sf http://127.0.0.1:8080/health >/dev/null; do sleep 2; done'
TimeoutStartSec=600
```

### 7. 유닛 본체를 직접 고치면 다음 `daemon install` 에 날아간다

`openclaw daemon install` 은 `openclaw-gateway.service` 를 다시 생성한다.
의존 관계·런타임 변경은 전부 **드롭인**(`openclaw-gateway.service.d/*.conf`)에
넣어야 살아남는다.

### 8. nvm node 를 가리키는 유닛 — 시스템 Node 를 깔아야 없어진다

```
Gateway service uses Node from a version manager; it can break after upgrades.
System Node 22 LTS (22.22.3+) or Node 24.15+ not found
```

`dist/runtime-paths-*.js` 를 열어보니 판정 기준이 명확하다.

- 시스템 Node 후보 (Linux): **`/usr/local/bin/node`, `/usr/bin/node` 뿐**
- version-manager 마커: `/.nvm/`, `/.fnm/`, `/.volta/`, `/.asdf/`, `/.n/`, `/.nodenv/` …
  경로에 이 문자열이 있으면(realpath 기준) 탈락

즉 `~/.local/node` 같은 데 깔아도 인정 안 된다. `/usr/local` 에 공식 tarball 을
풀어야 하고 root 가 필요하다. Ubuntu 22.04 의 apt `nodejs` 는 **12.22.9** 라
쓸 수 없다 (OpenClaw 는 `node:sqlite` 와 WAL-reset-safe SQLite 때문에 22.22.3+ /
24.15+ 를 요구한다).

**설치 후 유닛을 다시 생성해야 한다.** `/usr/local/bin/node` 가 생겨도 기존 유닛은
그대로다. 그리고 `openclaw daemon install` 은 이미 설치돼 있으면
`Gateway service already enabled.` 로 그냥 넘어간다 — `--force` 가 필요하다.

```bash
openclaw gateway install --force   # ExecStart 가 /usr/local/bin/node 로 재생성된다
```

### 9. 서비스 감사(audit)는 드롭인을 못 본다 — 경고 하나는 오탐이다

시스템 Node 로 전환한 뒤에도 이 경고 하나가 남는다.

```
Gateway service PATH includes version managers or package managers (/home/user/.nvm/.../bin)
```

그런데 실제 실행 환경은 이미 깨끗하다.

```
$ systemctl --user show openclaw-gateway -p Environment
PATH=/usr/local/bin:/usr/bin:/bin:/home/user/.local/bin
```

`dist/inspect-*.js` 의 `detectMarkerLineWithGateway` 가 **`.service` 파일 텍스트를
직접 줄 단위로 파싱**한다. `systemctl show` 를 쓰지 않으므로 `*.service.d/` 드롭인의
override 가 감사에 잡히지 않는다. 유닛 본체가 생성하는 PATH 에는 nvm 경로가 계속
섞이고, 이걸 config 로 조절할 수단은 없다(스키마에 해당 항목 없음). 드롭인으로
덮는 게 유일한 방법이고, 그래서 경고는 남지만 런타임은 정상이다.

**확인은 `openclaw daemon status` 가 아니라 systemd 에게 물어야 한다.**

```bash
systemctl --user show openclaw-gateway -p ExecStart -p Environment
```

또 하나 — 드롭인에 `ExecStart` 를 중복으로 박지 않는 게 좋다. `dist/index.js`
경로가 하드코딩되어 openclaw 를 다른 위치에 재설치하면 드롭인이 유닛을 깨뜨린다.
ExecStart 는 `gateway install --force` 에 맡기고 드롭인에는 PATH 만 남긴다.

### 10. `tools.profile: coding` 은 툴 5개를 지운다

```
tool policy removed 5 tool(s) via tools.profile (coding): agents_list, gateway, message, nodes, tts
tool policy removed 1 tool(s) via gateway sender owner-only tools.deny: cron
```

채널(텔레그램 등)을 붙일 거면 `message` 가 지워진 걸 알고 있어야 한다.

---

## 운영

```bash
systemctl --user status llama-server openclaw-gateway
systemctl --user restart llama-server        # 게이트웨이도 같이 재시작된다
openclaw daemon status                       # 서비스 점검 + 연결 probe
openclaw health
openclaw logs

openclaw chat                                # 로컬 TUI
openclaw dashboard                           # Control UI (토큰 포함 URL)
openclaw agent --agent main -m "..."         # 단발 턴
```

세션을 지정하려면 `--session-key agent:main:<이름>`. 생략하면
`No target session selected` 로 거절당한다.

---

## 아직 안 한 것

- **채널 연동** — `--skip-channels` 로 건너뛴 상태. 텔레그램/디스코드 등을 붙이면
  폰에서 Jetson 의 로컬 LLM 에 말을 걸 수 있다. `openclaw channels`
- **LAN/Tailscale 노출** — 지금은 loopback 전용. 이 Jetson 안에서만 접근된다.
  [docs/llm-runtime.md](../docs/llm-runtime.md) 에 Tailscale 주소가 있다.
- **30B 백엔드** — 툴 콜링 4/6 이라 보류. 30B(IQ2_M, 10.4GB) 를 올리면
  40K 컨텍스트의 KV 캐시까지 16GB 안에 못 들어간다.
