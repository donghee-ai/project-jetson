# runtime/ — 측정이 정한 값으로 돌리는 법

**여기 있는 것은 공유 자산이다.** `:8080` 의 `llama-server` 를 **세 소비자가 쓴다** —
벤치마크(`bench/*.py` 8개) · [Life Trainer](../Life_Trainer/) · OpenClaw 게이트웨이.

그래서 프레임워크 이름(`openclaw-setup/`)이 아니라 **소비자 기준**으로 여기에 있다.
전에는 `llama-server` 가 OpenClaw 의 부속처럼 보였다.

| | |
|---|---|
| [llm-runtime.md](llm-runtime.md) | llama.cpp CUDA 빌드 · 서버 운영 |
| [agent-gateway.md](agent-gateway.md) | OpenClaw 결합 — **함정 14가지** + §7 Life Trainer 결합 |
| `llama-server-qwen3.sh` | 서버 기동 스크립트 (`LLAMA_CTX` · Flash Attention · CUDA Graphs) |
| `systemd/llama-server.service` | 사용자 유닛 |
| `systemd/llama-server.service.d/ctx.conf` | **`LLAMA_CTX=20480`** — 아래 참조 |
| `install.sh` | 위 둘을 `~/.config/systemd/user/` 에 건다 |

## 설정값은 측정의 산출물이다

`ctx 20480` 은 취향이 아니다. [README ③](../README.md) 의 메모리 예산에서 나왔다 —
`40960` 이면 KV 캐시가 **2.99 GB** 라 임베딩(1.8 GB)과 같이 못 올라간다. `20480` 이 **1.50 GB** 다.

## ★ `ctx.conf` 링크가 끊기면 조용히 망가진다

`LLAMA_CTX` 가 기본값 `40960` 으로 돌아가고 KV 가 두 배가 되어, 8B 가 `llama-embed`
옆에서 **CUDA 버퍼를 못 잡는다. 에러 메시지가 원인을 안 가리킨다.**

전에는 `install.sh` 가 이 링크를 **안 걸었다**. 지금은 건다. 확인:

```bash
systemctl --user show llama-server -p Environment --value | tr ' ' '\n' | grep LLAMA_CTX
# → LLAMA_CTX=20480
```

## 재부팅 검증 — 2026-08-28 통과

`openclaw-setup/` 를 해체한 뒤 **콜드 부팅으로 확인했다** (`up 0 minutes`).
서비스 6개 · `LLAMA_CTX=20480` · 끊긴 심링크 0 · 헬스 2개 · 타이머 전부 정상.

### 두 번째 콜드 부팅 — 2026-08-28 오후, 그날 바꾼 것을 검증

같은 날 오후에 **유닛 12개의 절대경로를 `%h` 로 바꾸고**, 유닛 목록을
`desired-state.txt` 하나로 합치고, 백업 타이머를 신설하고, journal 을 영속화했다.
넷 다 `daemon-reload` 로만 확인한 상태였다 — **심링크와 경로는 프로세스가 도는
동안엔 안 깨진 것처럼 보인다.** 그래서 다시 껐다 켰다:

| 확인한 것 | 결과 |
|---|---|
| `%h` 가 콜드 부팅에서도 같은 경로로 풀리는가 | ✅ 기존 유닛·신규 백업 유닛 둘 다 |
| desired-state 의 12개가 **전부** 자동으로 뜨는가 | ✅ (선언에 없는데 도는 것도 0) |
| 새 백업 타이머가 재부팅을 넘기는가 | ✅ |
| **journal 이 이전 부팅을 갖고 있는가** | ✅ 2 부팅 보관 · 이전 부팅 유저 로그 115줄 |

★ **관측 공백이 실제로 닫혔다.** 이전 부팅의 종료 시퀀스(`lifetrainer-slack.service:
Consumed 9.202s CPU time.`)까지 읽힌다 — 아침에 *"밤에 무슨 일이 있었나"* 를 물을 수
있게 됐다는 뜻이다. `llama-server` 는 그전까지 `journalctl` 에 **항목이 하나도
없었는데** 이제 기동 로그가 남는다.

```bash
bash bench/verify-boot.sh
```

## journal 영속화 — 2026-08-28 적용

전에는 `/var/log/journal` 이 없어 journald 가 **메모리에만** 썼다. 재부팅하면 로그가
전부 사라져서, [HANDOFF §10](../Life_Trainer/HANDOFF.md) 이 안내하는 `journalctl`
명령이 빈손이었다. **관측 불가와 정상은 다르다** — 이 저장소가 겪은 사고는 전부
*"언제부터 그랬나"* 를 물어야 풀리는 종류였다.

```bash
sudo bash bench/enable-persistent-journal.sh   # 용량 상한(500M)을 먼저 걸고 켠다
```

`Storage=persistent` · `SystemMaxUse=500M` · `MaxRetentionSec=30day`.
**상한을 먼저 거는 순서가 중요하다** — rootfs 가 단일 파티션이라 무제한 journal 은
DB 와 같은 72GB 를 두고 다툰다.

`verify-boot.sh` §6 이 상태를 본다. 켠 **직후에는 노란불**이다 — 영속화를 이번 부팅
도중에 켰으면 이전 부팅이 디스크에 있을 수가 없다. **설정은 옳고 증명만 아직 안 된
상태를 실패로 세지 않는다.** 다음 재부팅 뒤 초록불이 된다.

**살아 있는 상태만 보고 넘어가지 않는다** — 심링크는 프로세스가 도는 동안엔
안 깨진 것처럼 보인다. 유닛 경로를 건드리면 이 스크립트를 다시 돌린다.

★ **2026-08-28: 이 스크립트가 "떠 있나" 만 보던 것을 고쳤다.** 유닛 목록을
`Life_Trainer/systemd/desired-state.txt` **하나**에서 읽어 대조한다 —
install·uninstall 과 같은 파일이다. 전에는 셋이 각자 목록을 들고 있어서
**설치 스크립트로 세운 기기가 지금 도는 기기와 달랐고**, 이 검사는 그 차이를 못 봤다.
desired 에 없는데 도는 것도 실패로 잡는다.

## 설치

```bash
bash runtime/install.sh                        # 추론 런타임 (공유)
bash ../Life_Trainer/deploy/install-gateway.sh # 게이트웨이 드롭인 (에이전트 경로에만)
```

## 기기 자체는 누가 보나 — `make host-status` (2026-08-28 신설)

`lt doctor` 17항목은 **전부 애플리케이션**이다 (DB·AW·LLM·Slack·큐·임베딩·에이전트).
기기가 죽어가는 것은 아무도 안 보고 있었다 — **앱이 초록불인 채로 기기가 나빠질 수 있다.**

```bash
make host-status
```

| 보는 것 | 왜 |
|---|---|
| 디스크·inode·ext4 오류 | rootfs 단일 파티션에 DB·백업·journal 이 전부 있다 |
| OOM kill (유닛별) | `llama-embed` 는 **일부러** 죽는다. 다른 유닛이 죽는 것과 가른다 |
| 발열·팬·OC 이벤트·전력모드 | 이 저장소의 모든 수치가 MAXN 전제다 |
| 유닛별 `NRestarts` | *"active"* 하나로는 **당일 반복 장애가 숨는다** |
| L4T 버전·hold·DKMS | [research/hardware.md §8](../research/hardware.md) |
| journal 영속 여부 | 로그가 재부팅을 넘기나 |

★ **못 읽는 것은 못 읽는다고 말한다.** NVMe SMART 는 root 가 필요해서 회색으로 뜬다 —
조용히 건너뛰면 *"확인했다"* 로 읽힌다.

### 이 스크립트를 쓰면서 낸 오탐 둘 (같은 함정을 두 번)

1. **커널 OOM 줄로 셌다** — `:8080` 대화 서버와 `:8081` 임베딩 서버가 **같은
   `llama-server` 바이너리**라 커널 로그로는 구분이 안 된다. *"다른 프로세스가
   죽고 있다"* 는 거짓 경보가 났다
2. 유닛 줄로 바꿨더니 이번엔 **`user@1000.service` 가 같은 이벤트를 한 번 더** 적어
   정확히 2배가 됐다. 또 같은 거짓 경보

**둘 다 "경보가 켜지는 조건" 을 안 재고 넣어서 났다.** 경보는 켜지는 조건보다
**안 켜지는 조건**을 확인하는 게 어렵다.

## 감시는 세 계층이다 (2026-08-28)

| 계층 | 무엇 | 언제 |
|---|---|---|
| **앱** | `lt doctor` 17항목 — DB·AW·LLM·Slack·큐·임베딩·에이전트 | 일 1회 |
| **기기** | `make host-status` — 디스크·OOM·발열·BSP·재시작 이력 | 일 1회 |
| **바깥** | `bench/heartbeat.sh` — dead-man switch | 15분 |

```bash
systemctl --user list-timers | grep jetson
```

### 앱·기기 — `jetson-daily-check.timer` (07:00)

**상태가 바뀌었을 때만 알린다.**

```
FAIL       → 항상 알린다
WARN 변화  → 알린다 (새로 생겼거나 사라졌을 때)
WARN 유지  → 안 알린다   ← 이미 아는 것. 알려진 WARN 은 docs/issues/ 에 있다
전부 OK    → 안 알린다
```

★ **이게 핵심이다.** 지금도 알려진 WARN 이 둘 있다(임베딩 backlog·OpenClaw 경로).
매일 그걸 다시 알리면 **일주일 안에 사람이 채널을 끈다.** 이 저장소는 오늘 하루에만
"안 꺼지는 경보" 함정을 세 번 밟았다 — 큐 실패율(`issues/0016`) · verify-boot 의
증명 못 한 상태 · host-status 의 이중 계수. **경보는 켜지는 조건보다 안 켜지는 조건이 어렵다.**

항목 **이름**만 비교한다. 뒤의 숫자까지 비교하면 매일 "변화" 가 된다.

### 바깥 — `jetson-heartbeat.timer` (15분) ★ **아직 미설정**

**같은 기기가 보내는 알림은 그 기기의 죽음을 못 알린다.** 전원 단절 · 네트워크 단절 ·
부팅 실패 · user manager 미기동 · Slack 경로 자체 장애 — 전부 *"알림이 안 온다"* 로만
나타나고, **안 오는 것은 눈에 안 띈다.** 그래서 판정을 바깥에 둔다.

```ini
# runtime/systemd/jetson-heartbeat.service
Environment=JETSON_HEARTBEAT_URL=https://hc-ping.com/<uuid>
```

- 나가는 것은 **"이 기기가 방금 정상이었다" 는 사실 하나**다. 개인 기록은 안 나간다
- 살아 있음을 **증명한 뒤에만** 보낸다 (`/health` + 핵심 유닛). 프로세스가 도는 것과
  서빙되는 것은 다르다 — 모델 id 를 안 보는 헬스체크가 1시간 무중단 다운을 낸 적 있다
- **아플 때는 안 보낸다.** dead-man switch 는 침묵이 곧 경보라,
  **아픈 채로 뛰는 심장이 제일 나쁘다**
- 타이머에 `Persistent=false` 다 — 밀린 heartbeat 를 나중에 몰아 보내면
  *"그동안 살아 있었다"* 는 거짓말이 된다

미설정이면 `verify-boot.sh` 가 노란불로 남긴다. **실패는 아니지만 조용히 넘어가지도 않는다.**

## 이 기기가 LAN 에 열어 둔 것 (2026-08-28 실측)

**방화벽이 없다** (`ufw`·`nftables` 둘 다 inactive). 노출 범위는 인터넷이 아니라
**LAN 전체**다 — 유선·무선 두 인터페이스가 같은 `192.168.0.0/24` 에 있다.
같은 공유기에 붙은 무엇이든 아래에 닿는다. 이 기기에는 **창 제목이 든 개인 활동 기록 DB**
가 있다.

| 포트 | 무엇 | 판정 |
|---|---|---|
| 111 | rpcbind | ~~❌ **NFS 마운트 0건**~~ ✅ **2026-08-28 껐다** (`disabled`) |
| 631 | snap `cupsd` | ~~❌ 프린터 없음~~ ✅ **2026-08-28 껐다** (`disabled`) |
| 5353 | avahi (mDNS) | ⚠️ **사람이 정할 몫** — 끄면 `ubuntu.local` 로 못 찾는다 |
| 22 | SSH | 필요. 다만 tailnet 제한 여부는 판단 필요 (§아래) |

```bash
sudo bash bench/harden-network.sh    # 111·631 을 끈다. rollback 절차 포함
```

**2026-08-28 적용 완료.** 끈 뒤 `verify-boot.sh` 로 우리 서비스가 안 다쳤는지 확인했고
(전부 통과), 남은 전체 개방 포트는 **SSH(22)와 mDNS(5353)뿐**이다.

★ **안 쓰는 것을 끄는 것이 방화벽 규칙보다 먼저다** — 규칙은 잊히지만 꺼진 서비스는
잊혀도 안 열린다. `make host-status` 가 이 상태를 계속 본다.

### 대신 정하지 않은 것 — SSH 를 tailnet 으로만 제한할지

우리 서비스는 이미 좁다: 웹 플래너(:8770)는 **Tailscale IP 에만** 바인딩,
`llama-server`·`llama-embed` 는 `127.0.0.1` 이다. 그래서 남는 것은 SSH 뿐인데,
tailnet 으로 제한하면 **Tailscale 이 죽었을 때 기기에 물리적으로 못 닿으면 잠긴다.**
그 위험과 같이 판단해야 하는 일이라 여기서 정하지 않는다.
