# deploy/ — Life Trainer 를 이 기기에 세우는 것

> **2026-08-28: `openclaw-setup/` 에서 갈라져 나왔다.** 그 폴더에는 소비자가 다른
> 두 가지가 섞여 있었다 — 공유 추론 런타임은 [`../../runtime/`](../../runtime/) 으로,
> 에이전트 경로에만 필요한 것은 여기로.

| | |
|---|---|
| `install-gateway.sh` | 게이트웨이 드롭인 2개를 건다 (**둘 다** — 전에는 하나씩 흩어져 있었다) |
| `install-system-node.sh` · `use-system-node.sh` | OpenClaw 가 요구하는 Node 22+ 로 맞춘다 |
| `patch-cron-schema.sh` | `cron` 툴 스키마의 `pattern` 제거 — llama.cpp GBNF 가 400 을 낸다 |
| `enable-tailscale-serve.sh` | 게이트웨이 제어 UI 를 tailnet 에 연다 |
| `slack-app-manifest.json` | 게이트웨이용 Slack 앱 (Life Trainer 자체 매니페스트와 다른 것) |
| `systemd/openclaw-gateway.service.d/` | `10-depends-llama` · `20-system-node` |

---

# openclaw-setup — 실행 자산

OpenClaw 게이트웨이 운영에 필요한 systemd 유닛과 설치 스크립트.

**구축 기록·실측·함정 정리는 [../runtime/agent-gateway.md](../../runtime/agent-gateway.md)** 에 있다.
이 폴더는 그 문서가 참조하는 실행 파일만 담는다.

> ⚠️ `~/.config/systemd/user/` 의 드롭인이 이 폴더를 **심링크로 참조**한다.
> 폴더를 옮기면 systemd 설정이 깨진다.

---

## 파일

| 파일 | 역할 |
|---|---|
| `bin/llama-server-qwen3.sh` | 모델 서버 실행 래퍼 (Qwen3-8B, `-c` 는 `LLAMA_CTX`, 기본 40960) |
| `systemd/llama-server.service.d/ctx.conf` | **`LLAMA_CTX=20480`** — KV 캐시 2.99GB → 1.50GB (08-23) |
| `systemd/llama-server.service` | 모델 백엔드 유닛 (readiness 체크 포함) |
| `systemd/openclaw-gateway.service.d/10-depends-llama.conf` | 게이트웨이 → 백엔드 의존 |
| `systemd/openclaw-gateway.service.d/20-system-node.conf` | 서비스 PATH 최소화 |
| `install.sh` | 유닛 등록 + 기동 + linger (사용자 권한) |
| `install-system-node.sh` | `/usr/local` 에 Node 24 설치 (**root**) |
| `use-system-node.sh` | 게이트웨이를 시스템 Node 로 전환 |
| `patch-cron-schema.sh` | cron 툴 스키마 호환 패치 (**openclaw 업그레이드마다 재실행**) |
| `slack-app-manifest.json` | Slack 앱 생성용 매니페스트 (플러그인 내장본 추출) |

★ **`lifetrainer` 에이전트 배선은 여기 없다.** Life Trainer 쪽에 있다 —
`Life_Trainer/scripts/install-agent.sh` (에이전트 등록 · MCP 서버 · 툴 정책).
게이트웨이가 살아 있는 것이 선행 조건이라 이 폴더의 `install.sh` 를 먼저 돌린다.
자세한 것은 [openclaw-agent.md §7](../../runtime/agent-gateway.md).

---

## 자주 쓰는 것

```bash
# 전체 기동 (유닛 등록 포함)
bash install.sh

# openclaw 업그레이드 후 — dist 가 교체되므로 패치 재적용
bash patch-cron-schema.sh

# ★ 업그레이드는 에이전트 배선도 지울 수 있다 (openclaw.json 은 남지만
#   워크스페이스 시드가 다시 돈다). 뒤이어 한 번 돌린다:
bash ../Life_Trainer/scripts/install-agent.sh

# 상태 확인은 openclaw daemon status 가 아니라 systemd 에게
systemctl --user show openclaw-gateway -p ExecStart -p Environment
systemctl --user status llama-server openclaw-gateway
```

현재 상태와 되돌리는 방법은
[../runtime/agent-gateway.md §5](../../runtime/agent-gateway.md) 참조.
