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
서비스 6개 · `LLAMA_CTX=20480` · 끊긴 심링크 0 · 헬스 2개 · 타이머 7개 전부 정상.

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
