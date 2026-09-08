# 백업과 복원 — 무엇을 잃을 수 있고 어떻게 돌아오나

> 2026-08-28 신설. **이 문서가 없던 동안 백업은 있었지만 복원해 본 적이 없었다.**
> 그리고 사본이 전부 원본과 같은 NVMe 에 있었다.

## 0. 이 기기가 실제로 잃을 수 있는 것

이 기기는 **암호화되지 않은 단일 ext4 rootfs** 하나다 (`/dev/nvme0n1p1`).
A/B 이중화도 없다. 그래서 위협은 셋으로 갈린다:

| 잃는 방식 | 같은 디스크의 사본이 도움이 되나 |
|---|---|
| 실수로 DB 를 고쳤다 / 마이그레이션이 잘못됐다 | **된다** |
| NVMe 고장 · 파일시스템 손상 | **안 된다** |
| 기기 도난 · 분실 | **안 된다.** 게다가 창 제목까지 그대로 읽힌다 |

**두 번째와 세 번째를 막는 것은 원본 밖 사본 하나뿐이다.**

★ **자동 복제는 안 하기로 했다 (2026-08-29).** 대신 `make recovery-bundle` 로
**되돌릴 수 없는 작업 직전에** 사람이 들고 나간다 (§3-B · §4).
→ **마지막 묶음 이후의 데이터는 잃는다.** 받아들이기로 한 위험이다.

---

## 1. 무엇을 백업하나 — DB 만으로는 못 돌아온다

`scripts/backup.sh` 가 하루 한 번(06:20) 두 가지를 만든다.

| 산출물 | 무엇 | 왜 |
|---|---|---|
| `data/backup/lifetrainer-<날짜>.db` | SQLite 온라인 백업 | 활동·계획·문서·잡 전부 |
| `data/backup/state-<날짜>.tar.gz` | **DB 밖의 것** ★ 비밀값 포함 · 0600 | 아래 |
| `data/backup/backup-<날짜>.sha256` | 둘의 해시 | 복원 시험이 대조한다 |

`state-*.tar.gz` 안에 든 것과, 없으면 무슨 일이 나는가:

| 무엇 | 없으면 |
|---|---|
| `config/lifetrainer.toml` | gitignore 라 저장소에 없다. **토큰·키가 전부 여기 있다** |
| `~/.openclaw/openclaw.json` | 저장소 밖 배선. 증상이 *"에이전트가 툴을 안 부른다"* 뿐이라 원인을 못 찾는다 |
| `enabled-units.txt` · `timers.txt` · 드롭인 | 무엇이 켜져 있었는지 모른다. `install-units.sh` 는 지금 상태를 완전히 재현하지 못한다 |
| `versions.txt` (node · openclaw · python · git HEAD) | llama.cpp·OpenClaw 를 다시 세울 때 어느 버전이었는지 |

**여기 없는 것 — 의도적이다:**

- **모델 가중치** (`~/models/`) — 수 GB 다. `refs/models/README.md` 가 무엇을 왜 골랐는지 갖고 있다
- **llama.cpp 빌드** — `operate/notes/llm-runtime.md` 의 빌드 절차로 다시 만든다
- **ingest·session secret** — 값을 백업하지 않는다. `web/auth.py` 가 없으면
  `data/ingestsecret` 을 0600 으로 **다시 만든다.** 폰 쪽에 새 값을 넣는 것이 복구 절차다

---

## 2. 매일 도는 것

```bash
systemctl --user list-timers | grep backup     # 06:20
journalctl --user -u lifetrainer-backup -n 30  # ★ journal 영속화가 안 돼 있으면 재부팅 뒤 빈손이다
```

스크립트가 **검증에 실패하면 그 백업을 지우고 실패로 끝난다.** 깨진 파일을
"백업 있음" 으로 세지 않기 위해서다. 보존 정리도 **검증을 통과한 새 백업이 생긴 뒤에만** 한다.

---

## 3. 월 1회 — 복원해 본다

```bash
bash scripts/restore-test.sh          # 가장 최근 백업
bash scripts/restore-test.sh data/backup/lifetrainer-2026-08-21.db   # 특정 백업
```

운영 DB 를 건드리지 않고 임시 경로에서만 돈다. 보는 것:

- 해시가 기록과 일치하는가
- `PRAGMA quick_check` · `foreign_key_check`
- **내용이 있는가** — 빈 DB 도 quick_check 는 통과한다
- 실제 조인 질의가 도는가 — 스키마만 맞고 관계가 깨진 경우를 잡는다

> **왜 이걸 따로 도는가.** 이 저장소의 반복 실패 4번이 *"테스트 통과 ≠ 동작"* 이다.
> 실제로 `data/backup/` 에 `.db-wal` 이 딸린 사본이 하나 있었다 — 파일은 있었지만
> **동작 중인 WAL DB 를 그냥 복사한 것**이라 복원 정합성이 없었다.
> 백업이 있다는 사실은 복원 가능하다는 뜻이 아니다.

---

## 3-B. 사람이 들고 나가는 묶음 — `make recovery-bundle`

`scripts/backup.sh` 는 **앱**을 되살린다. 이건 **기기**를 되살린다.

```bash
make recovery-bundle          # → recovery-bundle-<날짜>.tar.gz (약 35MB, 0600)
```

저장소 백업에 없던 것 — 실제로 조사해 보니 저장소 밖에 이만큼 있었다:

| | 왜 없으면 안 되나 |
|---|---|
| `~/.cloudflared/` | 터널 자격증명. **폰 수집 경로가 여기로 온다** |
| `~/project/wifi/` | ★ MT7601U 드라이버. **커널 hold 의 이유**이고, 재플래시하면 이게 없으면 WiFi 가 안 붙는다 |
| `~/.config/jetson/` | heartbeat URL |
| `~/.openclaw/` 배선 | 에이전트. 없으면 증상이 *"툴을 안 부른다"* 뿐이다 |
| systemd 심링크·활성 목록 | 무엇이 켜져 있었는지 |

**안 넣는 것 — 기준은 크기가 아니라 "다시 만들 수 있는가" 다:**

```
~/models/     5.3G   → 파일명 + sha256 만. 다시 받는다
~/llama.cpp/  1.5G   → commit + 빌드 옵션만. 다시 빌드한다
.venv                → pyproject 로 다시 만든다
~/.openclaw/npm 40M  → 캐시다
~/.openclaw 세션 30M → 대화 원문이라 뺀다 (보존 기간 문제이기도 하다)
```

> ★★ **토큰·API 키·터널 자격증명·개인 활동 기록이 들어 있다.** 0600 으로 만들지만
> **암호화는 안 돼 있다.** 옮긴 뒤 이 기기에서 지운다. `.gitignore` 가 커밋을 막는다.

> **검증이 산출물을 더럽히면 안 된다.** 처음에 quick_check 로 DB 를 열었더니
> `-wal`·`-shm` 이 옆에 생겨 그대로 묶였다 — **"동작 중인 WAL DB 를 복사한 것"과
> 구분이 안 되는 상태**, 즉 이 문서를 쓰게 만든 바로 그 결함이다. 지금은 검사 뒤 지운다.

---

## 4. 원본 밖으로 — **자동 복제는 안 하기로 했다 (2026-08-29 결정)**

**대신 `make recovery-bundle` 로 사람이 들고 나간다** (§3-B).

| | |
|---|---|
| 왜 | 매일 자동으로 내보낼 만큼 데이터가 빨리 변하지 않는다. 하루치를 잃는 것과 자동화·암호화 키 관리를 유지하는 비용을 견줬다 |
| 대신 | **되돌릴 수 없는 작업 직전에 한 번씩** 묶음을 뜬다 (BSP 업그레이드·재플래시·큰 마이그레이션 전) |
| 남는 위험 | §0 의 두 번째·세 번째 — **NVMe 고장·도난 시 마지막 묶음 이후가 사라진다.** 받아들이기로 한 위험이다 |

★ **이건 미완이 아니라 결정이다.** 다시 검토하려면 위 판단이 바뀌었는지부터 본다.

### 마음이 바뀌면 (스크립트는 이미 있다)

```ini
# systemd/lifetrainer-backup.service
Environment=LT_BACKUP_REMOTE=/mnt/외장/lifetrainer
Environment=LT_BACKUP_AGE_RECIPIENT=age1...
```

- 둘 다 있어야 동작한다. **한쪽만 채우면 스크립트가 일부러 실패한다** —
  개인 활동 기록과 창 제목이 평문으로 나가는 것을 막으려고
- 암호화는 `age` (`sudo apt install age`)

---

## 5. 실제로 복원하는 절차

### 5-1. DB 만 되돌린다 (실수·마이그레이션 사고)

```bash
systemctl --user stop lifetrainer-worker lifetrainer-web lifetrainer-slack
mv data/lifetrainer.db data/lifetrainer.db.before-restore     # 지우지 말고 옮긴다
cp data/backup/lifetrainer-<날짜>.db data/lifetrainer.db
rm -f data/lifetrainer.db-wal data/lifetrainer.db-shm         # 옛 WAL 을 남기면 섞인다
.venv/bin/lt doctor
systemctl --user start lifetrainer-worker lifetrainer-web lifetrainer-slack
```

### 5-2. 기기를 새로 세운다 (NVMe 고장 · 재플래시)

순서를 지킨다. **앞이 안 되면 뒤가 조용히 반쪽으로 선다.**

1. JetPack 설치 → `make verify` 로 CUDA·전력모드 확인
2. 저장소 clone → `versions.txt` 의 git HEAD 로 맞춘다
3. llama.cpp 빌드 (`operate/notes/llm-runtime.md`) → 가중치 내려받기 (`refs/models/README.md`)
4. `bash operate/tools/install.sh` → `llama-server` 기동 · **`LLAMA_CTX=20480` 확인**
   (드롭인이 안 걸리면 KV 가 두 배가 되어 임베딩과 같이 못 올라간다)
5. `python -m venv .venv && pip install -e '.[dev]'`
6. `state-*.tar.gz` 를 풀어 `config/lifetrainer.toml` 과 `~/.openclaw/openclaw.json` 복원
7. `cp lifetrainer-<날짜>.db data/lifetrainer.db`
8. `bash scripts/install-units.sh` → **`enabled-units.txt` 와 대조**한다.
   설치 스크립트가 안 켜는 것이 있다 (`docs/issues/` 확인)
9. `bash scripts/install-agent.sh` → `lt doctor` 로 전 항목 확인
10. `bash ../operate/tools/verify-boot.sh` → 재부팅 뒤에도 서는지
11. **ingest secret 은 새로 생긴다** — 폰 앱에 새 값을 넣어야 수집이 재개된다

### 5-3. 되돌리기

`5-1` 에서 옮겨 둔 `data/lifetrainer.db.before-restore` 를 되돌리면 원상복구다.
**복원 전 파일을 지우지 않는 이유가 이것이다.**

---

## 6. 지금 어디까지 왔나

| | 상태 |
|---|---|
| 자동 백업 · 무결성 · 해시 · 보존 | ✅ 2026-08-28 |
| 복원 시험 스크립트 | ✅ 2026-08-28 (첫 통과 확인) |
| **원본 밖 자동 복제** | ⛔ **안 하기로 했다 (2026-08-29)** — 수동 묶음으로 대신한다 (§4) |
| 디스크 암호화 (LUKS/OP-TEE) | ❌ 판단 안 됨. 개인 활동 기록을 담는 기기다 |
| 월 1회 복원 시험 자동 알림 | ❌ |
