# 정비 계획 v2 — 2026-08-28

> **v1 대비 무엇이 바뀌었나**: 첫 판은 *"문서와 진단기가 현재 상태를 정직하게 말하게 한다"* 에만
> 집중했다. `maintenance-plan-review.md`(장기 운영 관점 리뷰)가 **측정 생성기 자체가 거짓을
> 만들고 있고, 백업·BSP·복구가 계획에 아예 없다**는 것을 지적했다. 그 지적은 **대부분 사실이었다.**
>
> **다만 리뷰를 그대로 옮기지 않았다.** 주장을 이 기기에서 하나씩 재현해 보고
> **✅ 확인 / ✏️ 정정 / ❓ 미검증** 으로 표시했다. 두 건은 과장이었고, 리뷰에도 v1 에도
> 없던 **P0 하나**(journal 이 재부팅을 못 넘긴다)를 검증 중에 찾았다.
>
> **이 문서의 규칙 — 살아 있는 숫자를 적지 않는다.** 현황은 `make status` · `lt doctor` 로
> 뽑고 여기엔 명령만 적는다. 예외는 **파일에 박혀 있는 고정 문자열**(§A 의 인용)과
> **오늘 조회로 확정된 판정**(§B-2 의 패키지 버전)뿐이고, 그건 살아 있는 값이 아니라 증거다.

---

## 0. 검증 결과 요약 — 무엇이 사실이고 무엇이 아니었나

| 리뷰의 주장 | 판정 | 근거 |
|---|---|---|
| `verify-jetpack.sh` 가 L4T 를 빈칸으로 낸다 | **✅ 확인** | 정규식이 `R36 (release), REVISION:` 형식을 못 읽는다 |
| CUDA 라이브러리 4종을 "없음" 으로 낸다 | **✅ 확인** | `find` 가 심링크 `lib64` 를 안 내려간다. `-L` 을 붙이면 나온다 |
| DLA 를 "없음" 으로 낸다 | **✅ 확인** | 스크립트는 `/dev/nvhost-nvdla*` 만 본다. 실제 노드는 `nvhost-ctrl-nvdla0/1` 이고 `libcudla.so.1` 이 `ldconfig` 에 있다 |
| JetPack 6.2.3 인데 L4T 는 36.5.0, 후보는 36.5.2 | **✅ 확인** | 커널·헤더가 `hold` 상태인 것까지 그대로 |
| 백업이 자동·외부·암호화·복원시험 전부 없다 | **✅ 확인** | 최신 백업이 08-23, 원본과 같은 NVMe. 백업 타이머 없음 |
| 커밋 `49581fa` 에 Serper 키가 들어갔다 | **✅ 확인** | 40자 키가 **4개 파일**에 들어 있고 `origin/main` 이력에 있다 |
| 그래서 **키를 회전해야 한다** | **✏️ 정정 — 이미 회전됐다** | 이력의 키와 현재 키의 sha256 이 다르다. [known-issues §17](Life_Trainer/docs/known-issues.md) 이 08-27 재발급 + 옛 키 403 확인을 기록. **남은 것은 공개 이력의 죽은 문자열뿐** |
| rpcbind(111)·CUPS(631)가 전체 인터페이스에 열려 있다 | **✅ 확인** | `ss -tulnp` 에서 `0.0.0.0:111` · `0.0.0.0:631` |
| rootfs 가 암호화 안 된 단일 ext4 | **✅ 확인** | `/dev/nvme0n1p1` 하나 |
| 유닛이 `/home/user` 절대경로에 묶여 있다 | **✅ 확인** | 유닛 12개 **전부** |
| `install-units.sh` 가 현재 상태를 재현 못 한다 | **✅ 확인 (리뷰보다 심각)** | §C-1 참조 |
| `thermal-test.sh` 의 "92°C = 스로틀링 영역" 표기가 부정확 | **✅ 확인** | 프로젝트 경보값을 BSP 임계값처럼 적고 있다 |
| 파이썬 의존성이 하한만 있고 lock 이 없다 | **✅ 확인** | [pyproject.toml](Life_Trainer/pyproject.toml) 전부 `>=` |
| **llama-server 가 같은 부팅에서 기동 타임아웃·강제 종료를 여러 번 냈다** | **✏️ 정정 — 재현 안 됨** | `NRestarts=0` · `ExecMainStatus=0`. 13:23 의 기동은 그 시각의 디코드 프로파일링(서버 재기동)과 일치한다. **그런데 확인하려다 §A-4 를 찾았다** |
| OC 이벤트 카운터 `oc2=15 · oc3=37` | **❓ 미검증** | 해당 sysfs 노드를 권한 없이 못 읽었다. 검사 자체는 값싸므로 §C-3 에 넣되 **값은 인용하지 않는다** |

> **리뷰를 그대로 안 옮긴 이유.** 이 저장소의 반복된 실패 1번이 *"조사 문서를 사실로 믿었다"* 다
> ([CLAUDE.md](Life_Trainer/CLAUDE.md)). 리뷰도 조사 문서다. 재현되는 것만 계획에 넣는다.

---

## 1. 순서

```
A. 거짓을 만드는 것부터 멈춘다      ← 생성기·로그. 여기가 안 잡히면 나머지가 무의미
B. 잃으면 못 돌아오는 것            ← 백업·복원·BSP
C. 다시 세울 수 있게 한다            ← 설치 재현성·호스트 감시
D. 문서와 CI                        ← v1 의 §1~3. 비용이 작아 A 와 같이 간다
E. 공개 준비                        ← 이력 정리·라이선스·장기 시험
F. 미룬다                           ← 브랜치·폴더 이름
```

**리뷰의 순서를 대체로 받아들이되 한 곳을 바꿨다.** 리뷰는 문서 정리(v1 §1)를 P2 로 내렸는데,
**D 를 A 와 같이 둔다.** [handbook](Life_Trainer/docs/handbook.md) 이 *"Serper 키가 없다"* 고
말하는 것은 **다음 사람이 없는 문제를 쫓게 만드는 것**이라 성격이 A 와 같고, 비용이 30분이다.
운영 리스크가 아니라 **사람의 시간을 태우는 리스크**다.

---

# A. 거짓을 만드는 것부터 멈춘다

## A-1. `verify-jetpack.sh` 가 틀린 `environment.md` 를 생성한다 ✅

**이게 v1 의 가장 큰 누락이다.** v1 은 handbook 이 틀렸다고 지적하면서
[environment.md](environment.md) 는 *"자동 생성"* 이라는 이유로 믿고 넘어갔다.
**생성기가 틀리면 자동 생성이 더 위험하다** — 사람이 고쳐도 다음 `make verify` 가 되돌린다.

지금 [environment.md](environment.md) 가 말하는 거짓 넷:

| 출력 | 실제 | 원인 ([bench/verify-jetpack.sh](bench/verify-jetpack.sh)) |
|---|---|---|
| `L4T: ` (빈칸) | `R36 (release), REVISION: 5.0` | 정규식 `R[0-9]+ .REVISION:` — `R36` 과 `REVISION` 사이가 한 글자라고 가정한다. 실제로는 `(release), ` |
| `❌ libcudart / libcublas / libcufft / libcurand` | 넷 다 설치돼 있고 `ldconfig` 에 등록돼 있다 | `find /usr/local/cuda/lib64 …` — **`lib64` 가 심링크**라 `find` 가 안 내려간다 |
| `❌ DLA 디바이스 없음` | `/dev/nvhost-ctrl-nvdla0` · `nvdla1` 존재, `libcudla.so.1` 설치됨 | 글롭이 `/dev/nvhost-nvdla*` 라 `ctrl-` 접두를 못 잡는다 |
| Super Mode 활성화 절차 안내 | 이 저장소가 **부팅마다 되돌려진다고 이미 기록**했다 | 장비 지원 여부와 무관하게 무조건 출력 |

**넷 중 앞 셋은 이 저장소의 대표 서사와 직접 충돌한다** — *"사양서와 실측이 다르다"* 를 말하는
저장소가, 실측 도구의 버그로 만든 ❌ 를 실측 결과로 싣고 있다. 공개하면 이게 제일 먼저 지적당한다.

### 할 일

- [ ] L4T 정규식 수정 → `grep -oE 'R[0-9]+ .*REVISION: [0-9.]+'` (또는 `sed` 로 필드 추출)
- [ ] CUDA 라이브러리 판정을 **파일 탐색에서 `ldconfig -p` + `dpkg-query` 로** 바꾼다.
      `find` 를 쓴다면 최소한 `-L`. **판정 근거를 "파일이 보이나"에서 "링커가 찾나"로 옮기는 것**이 핵심
- [ ] DLA 판정을 `/dev/nvhost*nvdla*` 로 넓히고, **"있다/없다" 대신 "확인된 것"만 출력**한다.
      진짜 가용성은 TensorRT 로 작은 엔진을 `--useDLACore` 로 빌드해 봐야 안다 → 별도 항목으로
- [ ] Super Mode 안내를 **조건부로** 바꾼다. 이 보드에서 안 되는 것이 이미 확인됐으므로,
      절차를 노출하지 말고 *"이 장비에서는 불가 — 근거는 `research/hardware.md`"* 로
- [ ] 고친 뒤 `make verify` 를 돌려 [environment.md](environment.md) 를 재생성하고,
      **README·research 에서 이 ❌ 들을 인용하고 있는 곳이 있는지** 확인한다

### 끝난 것으로 치는 조건

```bash
make verify && grep -c "❌" environment.md     # 남은 ❌ 가 전부 사실인지 하나씩 설명 가능해야 한다
```

## A-2. handbook 이 오늘 날짜로 틀린 말을 한다 ✅ (v1 §1 유지)

[docs/handbook.md](Life_Trainer/docs/handbook.md) 헤더가 `기준일: 2026-08-28` 인데 본문 7곳이
그 이전을 말한다. **아래는 파일에 박혀 있는 고정 문자열이다** — 살아 있는 값이 아니라 증거다.

| 줄 | 문서에 박혀 있는 것 | 실제 |
|---|---|---|
| `:334` `:477` | 웹 검색 "한국어=**네이버** / 그 외=Serper · **키 대기**" | 네이버 경로는 08-25 에 **코드에서 삭제**됐다. [`llm/websearch.py`](Life_Trainer/lifetrainer/llm/websearch.py) 가 첫머리에 그렇게 적고 있다. 키는 08-21 투입 |
| `:479` | RAG "1·3단계 완료, 2단계 남음" | 1~4단계 완료 (08-23) |
| `:488` `:489` | 이벤트·문서·요약·잡 수 | `make status` 와 다르다 |
| `:504` | §8-1 "Serper 키가 없다 ★ **유일한 WARN**" | 해결됨. 실제 WARN 은 §C-2 의 셋 |
| `:528` | §8-3 "`plan` 5건은 실제 일정이 아니다" | 08-24 교체됨 |

같은 병이 [HANDOFF §4](Life_Trainer/HANDOFF.md) 에 남아 있다 — §1 은 08-28 에 명령으로 바꿨는데
**§4 는 숫자 블록 그대로다.**

**원인은 문서가 두 시제를 섞고 있다는 것이다.** handbook 은 "무엇이 있고 어떻게 흐르는가"(구조,
거의 안 변함)와 "지금 무엇이 막혀 있는가"(현황, 매일 변함)를 한 파일에 담는다. 후자는 이미
[`docs/issues/`](Life_Trainer/docs/issues/) 와 `lt doctor` 가 하는 일이고,
[issues/README](Life_Trainer/docs/issues/README.md) 가 상태 열을 안 두는 이유와 정확히 같다.

- [ ] handbook **§8·§9 삭제** → *"지금 막힌 것은 `docs/issues/` 와 `lt doctor` 가 갖는다"* 한 줄
- [ ] handbook **§7 표의 상태 열 · "검증 기록" 숫자 삭제** → `make status` · `make test` 로
- [ ] handbook `:334` 의 네이버 서술 삭제
- [ ] **HANDOFF §4 를 삭제하고 §1 로 흡수**
- [ ] [docs/README.md](docs/README.md) `:24` 의 `openclaw-setup/` → `runtime/` (08-28 해체된 옛 이름)

## A-3. CI 가 숫자·상태 주장을 검증하지 않는다 ✅ (v1 §3, 리뷰 의견 반영)

`make links` 는 링크만 본다. 숫자와 상태 주장은 아무도 안 본다 — 그래서 A-2 가 생겼다.
*"자리를 하나로 줄인다"* 가 3차, *"명령으로 대체한다"* 가 4차인데, **강제하는 것이 없으면 5차가 난다.**

**리뷰의 지적을 받아들인다**: 정규식 하나로 모든 숫자를 막으면 오탐과 예외가 빠르게 는다.

```
make check          ← 새 진입점
├── make links              문서 링크 (있음)
├── make check-docs         숫자·상태 주장 (신규)
├── shellcheck bench/*.sh Life_Trainer/scripts/*.sh   (신규)
└── systemd-analyze verify  유닛 정적 검사 (신규)

make check-fast     ← pre-push 용. links + check-docs 만
```

- [ ] `bench/check-docs.sh` — 금지 대상을 **숫자 일반이 아니라 "운영 지표 리터럴" 로 한정**한다
      (이벤트 수·문서 수·테스트 수·잡 수·규칙 수). 예외는 **한 목록에** 둔다
- [ ] 예외 경로는 [check-links.sh](bench/check-links.sh) 가 이미 쓰는 것을 재사용한다 —
      `progress/` · `HISTORY/` 는 **그날의 사실이라 고치면 안 되는 문서**다.
      날짜 붙은 스냅숏 블록도 예외
- [ ] **막아야 할 예제 / 허용해야 할 예제를 fixture 로** 둔다. 검사기 자체를 테스트한다
- [ ] CI 에 `make check` 추가 (`links` 와 같은 러너 — 비용 0)

## A-4. ★ journal 이 재부팅을 못 넘긴다 — 리뷰에도 v1 에도 없던 것

**리뷰의 llama-server 주장을 검증하려다 찾았다.**

```
/var/log/journal        없음  → journald 가 volatile(메모리) 모드
journalctl --user       0B    → 재부팅하면 전부 사라진다
journalctl --user -u llama-server -b   → "No entries"
```

이게 왜 P0 인가:

- [HANDOFF §10 빠른 확인](Life_Trainer/HANDOFF.md) 이 `journalctl --user -u lifetrainer-sync -n 20`
  을 안내한다. **재부팅을 넘기면 그 명령이 빈손이다** — 문서가 있는 도구를 가리키는데 도구가 없다
- 이 프로젝트가 겪은 큰 사고는 **전부 사후 조사가 필요한 종류**였다:
  모델 id 를 안 봐서 난 1시간 무중단 다운, 야간 배치 277건 유실.
  둘 다 *"언제부터 그랬나"* 를 물어야 풀리는데, **지금 그 질문에 답할 방법이 없다**
- 리뷰가 본 llama-server 타임아웃을 내가 재현 못 한 것도 부분적으로 이 때문이다.
  **관측 불가와 정상은 다르다**

### 할 일 (5분)

```bash
sudo mkdir -p /var/log/journal
sudo systemd-tmpfiles --create --prefix /var/log/journal
sudo journalctl --flush
# 용량 상한을 반드시 같이 건다 — 이 기기는 rootfs 단일 파티션이다
printf '[Journal]\nStorage=persistent\nSystemMaxUse=500M\nMaxRetentionSec=30day\n' \
  | sudo tee /etc/systemd/journald.conf.d/persist.conf
sudo systemctl restart systemd-journald
```

- [ ] 위 적용 · `SystemMaxUse` 로 디스크 상한을 **먼저** 건다
- [ ] 재부팅 후 `journalctl --user -u llama-server -b -1` 이 이전 부팅을 보여주는지 확인
- [ ] [verify-boot.sh](bench/verify-boot.sh) 에 "journal 이 이전 부팅을 갖고 있나" 한 줄 추가

> **이 항목이 B·C 보다 먼저인 이유**: 백업도 감시도 **로그가 남아야 원인을 찾는다.**
> 지금은 무엇을 고쳐도 다음에 같은 일이 나면 또 처음부터 추측해야 한다.

---

# B. 잃으면 못 돌아오는 것

## B-1. 백업이 운영 체계가 아니다 ✅ — v1 의 명백한 누락

[`lt backup`](Life_Trainer/lifetrainer/cli.py) 은 SQLite 온라인 백업 API 를 쓴다. **구현은 옳다.**
없는 것은 그 주변 전부다:

```
자동 실행 타이머     없음 (lifetrainer 타이머 7개 중 백업 없음)
원본 밖 복제         없음 — 최신 백업이 원본과 같은 NVMe 의 data/backup/
암호화               없음 — 창 제목이 그대로 들어 있는 개인 활동 기록이다
보존 정책            없음 (손으로 만든 4개가 그냥 쌓여 있다)
무결성 검사          없음
복원 시험            **한 번도 안 했다**
```

★ 그리고 `data/backup/` 에 **`.db-shm` · `.db-wal` 이 딸린 사본이 하나 있다.**
그건 `lt backup` 이 아니라 **동작 중인 WAL DB 를 그냥 복사한 것**이다 — 복원했을 때
정합성을 보장 못 한다. 백업이 있다는 사실이 곧 복원 가능하다는 뜻이 아니라는 실물 증거다.

지금 백업은 *"실수로 DB 를 고쳤을 때"* 에만 쓸모 있고, **NVMe 고장·도난·rootfs 손상에는 무력하다.**
rootfs 는 암호화 안 된 단일 ext4 다.

### 할 일

- [ ] `lifetrainer-backup.timer` 신설 (일 1회, 야간 배치 종료 후)
- [ ] 생성 직후 **`PRAGMA quick_check`** + 해시 파일. 실패하면 그 백업을 폐기하고 알린다
- [ ] **원본 NVMe 밖으로 복제** — 외장 매체 또는 off-device. 암호화해서 나간다
- [ ] 보존 정책 (일 7 · 주 4 · 월 3 정도)
- [ ] **월 1회 실제 복원 시험** — 임시 경로에 복원 후 `schema_version` 과 주요 테이블 행 수 확인.
      *"백업이 있다"* 가 아니라 *"복원해 봤다"* 가 목표다
- [ ] **DB 만으로는 못 돌아온다.** 복구 대상 목록을 runbook 으로 남긴다:

  | 무엇 | 왜 |
  |---|---|
  | `config/lifetrainer.toml` | gitignore 라 저장소에 없다 |
  | `~/.openclaw/openclaw.json` | **저장소 밖 배선.** 없으면 증상이 "에이전트가 툴을 안 부른다" 뿐이다 |
  | systemd 활성 목록 + 드롭인 | §C-1 |
  | ingest·session secret 재생성 절차 | 값이 아니라 **절차**를 적는다 |
  | llama.cpp commit · 빌드 옵션 · CUDA arch | 다시 빌드해야 한다 |
  | 모델 파일명 + 해시 | 가중치는 저장소에 없다 |
  | Node · OpenClaw 버전 | §E-3 |

- [ ] **디스크 암호화 여부를 명시적으로 결정**한다. 개인 활동 기록과 창 제목을 담는 기기다.
      적용하든 안 하든 **판단과 위협 모델을 기록**한다 — 안 적으면 다음에 또 검토한다

> 리뷰가 rootfs A/B 이중화를 선택지로 들었다. **개인 단일 장비에는 복잡도가 값을 못 넘는다고 본다.**
> 대신 **외부 백업 + 재플래시 runbook** 을 반드시 갖춘다. 이 판단을 문서에 남긴다.

## B-2. JetPack 과 BSP 버전이 어긋나 있다 ✅

오늘 조회로 확정된 값이다 (`dpkg -l` · `apt-cache policy` · `apt-mark showhold`):

| 구성 | 설치 | APT 후보 |
|---|---|---|
| `nvidia-jetpack` | 6.2.3+b81 | 6.2.3+b81 |
| `nvidia-l4t-core` · `-bootloader` | **36.5.0** | **36.5.2** |
| `nvidia-l4t-kernel` | 5.15.185-tegra-36.5.0 | 5.15.199-tegra-36.5.2 |

그리고 **`nvidia-l4t-kernel` · `nvidia-l4t-kernel-headers` 가 `hold` 다.**

JetPack 6.2.3 은 Jetson Linux 36.5.2 를 포함한다. BSP OTA 는 펌웨어·커널 드라이버·유저스페이스가
같은 릴리스로 움직이는 것을 전제로 설계돼 있고, 섞는 것을 권장하지 않는다.

- <https://developer.nvidia.com/embedded/jetpack-sdk-623>
- <https://docs.nvidia.com/jetson/archives/r36.5.2/DeveloperGuide/SD/SoftwarePackagesAndTheUpdateMechanism.html>

### 할 일 — hold 를 그냥 풀지 않는다

- [ ] **먼저 `hold` 를 건 이유를 찾는다.** 기록이 없으면 그 사실 자체를 적는다.
      커널을 고정한 데는 보통 이유가 있다 (OOT 모듈·드라이버·부트 문제)
- [ ] B-1 의 백업·복구 목록이 **갖춰진 뒤에** 손댄다. 순서를 바꾸지 않는다
- [ ] `apt -s full-upgrade` 로 **제거 예정 패키지를 먼저 검토**한다
- [ ] 유지보수 창에서 BSP 를 **한 릴리스로 통일**하고, 재부팅 후 bootloader·kernel·userspace 가
      일치하는지 확인
- [ ] 재부팅 후 smoke: CUDA · llama.cpp(`llama-bench` 1회) · TensorRT · OpenClaw · 타이머 7개
- [ ] 실패 시 **재플래시 + 데이터 복원** 경로를 준비해 둔다 (B-1 이 이걸 준다)

> **업데이트를 막는 것과 검증 없이 따라가는 것 둘 다 위험하다.** 지금은 전자에 가깝고,
> 그 상태가 **기록 없이** 유지되고 있다는 것이 문제다.

---

# C. 다시 세울 수 있게 한다

## C-1. 설치 스크립트가 지금 도는 상태를 재현하지 못한다 ✅ (리뷰보다 심각)

[runtime/README](runtime/README.md) 가 08-28 콜드 부팅 검증을 기록했다. 그건
**"지금 것이 다시 뜨는가"** 를 확인한 것이지 **"처음부터 세울 수 있는가"** 가 아니다. 실측:

| | [install-units.sh](Life_Trainer/scripts/install-units.sh) | [uninstall-units.sh](Life_Trainer/scripts/uninstall-units.sh) | 지금 |
|---|---|---|---|
| 타이머 7개 | enable | **nightly · nightly-stop 누락** | active |
| `lifetrainer-worker` | enable | disable | active |
| `lifetrainer-slack` | **enable 안 함** | disable | **active** |
| `lifetrainer-web` | **누락** | **누락** | **active** |
| `llama-embed` | **누락** | **누락** | **active** |

두 가지가 겹쳐 있다:

1. **install 이 안 켜는 것을 uninstall 도 안 끈다** — 설치→제거를 돌려도 지금 도는 6개 중
   셋(web · llama-embed · slack)이 남는다. *"자동화 전체 끄기"* ([HANDOFF §9](Life_Trainer/HANDOFF.md))
   가 실제로는 전체를 안 끈다
2. **install 의 주석이 무효한 전제 위에 있다** — *"지금은 Slack mode=notify 라 불필요"* 라고
   적혀 있는데 **운영은 mode=bolt** 다 (`lt doctor` 가 확인). 스크립트가 08-17 이전 세계를 설명한다

### 할 일

- [ ] **desired-state 목록 하나**를 만들고 install · uninstall · 상태검사가 **같은 목록을 읽게** 한다.
      이 저장소의 반복 실패 2번(*"같은 값을 여러 곳에서 각자 계산했다"*)이 바로 이 모양이다
- [ ] `lifetrainer-web` · `llama-embed` 를 양쪽에 넣는다
- [ ] slack 은 **설정(`[slack].mode`)을 읽어 분기**하게 하거나, 최소한 주석을 현실에 맞춘다
- [ ] [verify-boot.sh](bench/verify-boot.sh) 에 **desired-state 대조**를 추가한다 —
      지금은 "떠 있나"만 본다
- [ ] 유닛 12개의 `/home/user` 하드코딩 제거 → `%h` 또는 EnvironmentFile.
      **다른 사용자·다른 경로에서 전부 깨진다**

## C-2. `lt doctor` 의 WARN 이 상수가 되어간다 ✅ (v1 §2 + 리뷰 확장)

WARN 셋이 상시 켜져 있다. **헬스체크가 죽는 방식이 정확히 이거다** — 상시 노란불이면 사람이
진단기를 안 본다. 17항목을 만든 투자를 지키려면 **WARN 0 이 기본값**이어야 한다.

### C-2a. 큐 — 이미 해결된 것을 계속 노란불로 잡는다

[HANDOFF §4](Life_Trainer/HANDOFF.md) 가 08-24부터 *"203건이 08-21 수정 이전 잔재인지 안 갈렸다"*
고 열어 두고 있었다. **2026-08-28 에 갈렸다** — 실패 잡은 **전부 08-20 생성 · 08-22 종료**이고
그 이후 신규 실패가 없다. 08-21 수정은 먹었다.

```bash
Life_Trainer/.venv/bin/python -c "
import sqlite3, datetime as dt
c = sqlite3.connect('file:Life_Trainer/data/lifetrainer.db?mode=ro', uri=True)
d = lambda x: dt.datetime.fromtimestamp(x).strftime('%Y-%m-%d') if x else None
for st, mn, mx, n in c.execute('select state, min(created_at), max(created_at), count(*) from job group by state'):
    print(f'{st:10} n={n:5}  created {d(mn)} ~ {d(mx)}')
"
```

**판정식이 누적 비율이라 신규 실패가 0이어도 비율이 오른다.** `purge_done` 이 14일 뒤 걷어갈
때까지 신호가 죽어 있다.

**리뷰의 지적을 받아들인다** — 최근 창 하나만 봐도 부족하다. 목표는 *"오늘 초록불"* 이 아니라
**새 장애를 빨리 잡는 것**이다:

- [ ] 최근 7일 `finished_at` 창 실패율 (+ **최소 표본 수** — 표본이 3건일 때 33% 를 경보로 읽지 않게)
- [ ] `queued` 중 **가장 오래된 잡의 나이** ← backlog 는 실패율에 안 잡힌다
- [ ] `running` 인데 **lease 가 만료된 잡** ← 워커가 죽으면 여기 쌓인다
- [ ] **마지막 실패 시각** 과 잡 종류별 실패율
- [ ] HANDOFF·issues 의 *"안 갈렸다"* 서술을 위 조회 결과로 닫는다

### C-2b. 임베딩 — 손으로 돌려서 닫지 않는다

`lt embed --all` 한 줄로 오늘 숫자는 맞출 수 있다. **하지만 그건 증상이다.**
야간 종료 경로에 `embed_pending` 이 **이미 연결돼 있으므로**, 배선이 없는 게 아니라
**처리 한도와 유입량이 안 맞아 backlog 가 남는 것**이다.

- [ ] 야간 처리량 vs 일일 유입량을 비교해 **용량을 고친다**
- [ ] doctor 판정을 행 수 비교에서 넓힌다: 현재 model·dim 으로 만들어졌는지 ·
      `source_hash` 가 현재 본문과 일치하는지 · **가장 오래 밀린 문서의 나이**
- [ ] 임베딩 서버의 MemoryMax·OOM·재시작 이력 (08-23 에 누수로 상한을 내린 적이 있다)

### C-2c. OpenClaw 가 nvm 경로에 있다

`/usr/local/bin/node` 는 이미 설치돼 있는데 doctor 가 찾은 `openclaw` 는 여전히 nvm 경로다.
**전환 근거가 있다.**

- [ ] 전환 전 게이트웨이 유닛 백업 + OpenClaw 버전 기록
- [ ] [`deploy/use-system-node.sh`](Life_Trainer/deploy/use-system-node.sh) 적용
- [ ] system Node 에서 에이전트·Slack 위임 smoke test · rollback 명령 문서화
- [ ] OpenClaw 업그레이드 후 **cron schema 패치 재적용**이 필요하다는 것을 runbook 에

## C-3. 호스트 상태를 아무도 안 본다 ✅ (리뷰 §6 — 받아들임)

`lt doctor` 는 **애플리케이션** 진단기다. 기기 자체는 아무도 안 본다.

- [ ] **`make host-status`** (또는 `bench/host-status.sh`) 신설:

  | 항목 | 왜 |
  |---|---|
  | BSP·커널·부트로더 버전 일치 · hold · pending update | §B-2 가 조용히 재발한다 |
  | **NVMe SMART** (수명 · media error · unsafe shutdown) | 단일 디스크다. §B-1 의 유일한 매체 |
  | 디스크·inode 사용률과 **증가율** | rootfs 단일 파티션. journal 을 켜면 더 중요해진다 |
  | OOM kill · zram 사용 | 통합 메모리 16GB 에 6.7+1.8+0.3GB 를 얹고 있다 |
  | 유닛별 `NRestarts` · 마지막 종료 코드 · **readiness 소요 시간** | *"active"* 하나로는 당일 반복 장애가 숨는다 |
  | 팬 RPM · `nvfancontrol` · tj 온도 · 스로틀 이벤트 | |
  | 전력 모드 · GPU/EMC 상한 | 이 저장소의 모든 수치가 MAXN 전제다 |
  | **OC / 저전압 이벤트 카운터** ❓ | 리뷰가 값을 제시했으나 **내가 재현 못 했다.** 검사를 넣되 값은 인용하지 않는다 |

- [ ] [runtime/systemd/llama-server.service](runtime/systemd/llama-server.service) 의
      `ExecStartPost` + `TimeoutStartSec=600` 은 **의도된 설계**다 (주석이 이유를 적고 있다 —
      게이트웨이의 `After=` 를 의미 있게 만든다). **바꾸지 말고 관측만 붙인다**:
      readiness 에 몇 초 걸렸는지를 상태 출력에 남긴다

## C-4. 같은 기기가 보내는 알림은 그 기기의 죽음을 못 알린다 ✅

v1 §6 은 *"doctor 를 매일 돌려 Slack DM"* 이었다. **리뷰의 지적이 맞다** — 전원 단절 ·
네트워크 단절 · 부팅 실패 · user manager 미기동 · Slack 경로 자체 장애는 **그 방식으로 못 알린다.**

- [ ] **애플리케이션 doctor** — `lt doctor` 일 1회. WARN/FAIL 일 때만 알림 (**§C-2 를 먼저 끝낸다**)
- [ ] **호스트 doctor** — `make host-status` 일 1회
- [ ] **외부 dead-man heartbeat** — 기기가 주기적으로 신호를 보내고, **바깥이 그 부재를 알린다**.
      이게 없으면 위 둘 다 기기가 살아 있을 때만 동작한다

> **순서가 중요하다: C-2(WARN 0) → C-4(자동 알림).** 반대로 하면 첫날부터 노란불 셋이 울리고,
> 그러면 알림을 끄게 된다. 그때는 있으나 마나다.

---

# D. 미룰 수 없는 문서 작업

A-2 · A-3 이 여기 해당한다. **비용이 30분+20분이라 A 와 같이 처리한다.**

---

# E. 공개 준비

## E-1. Git 이력의 비밀 — ✏️ 정정: 회전은 이미 끝났다

**리뷰는 "키를 회전해야 한다"고 했지만 이미 됐다.** 이력의 키와 현재 키의 sha256 이 다르고,
[known-issues §17](Life_Trainer/docs/known-issues.md) 이 08-27 재발급과 옛 키 폐기 확인(HTTP 403)을
기록하고 있다. Slack `bot_token`·`app_token` 은 **그 백업들에서 빈 문자열**이었다.

**남은 것은 하나다: 40자짜리 죽은 문자열이 공개될 `origin/main` 이력에 있다.**
피해는 없지만 **모든 secret scanner 가 잡고**, 공개 저장소에서 그건 신뢰 문제다.

- [ ] 공개 전 **전체 ref 에 secret scan** (모든 branch·tag) — `gitleaks` 등
- [ ] `git filter-repo` 로 `Life_Trainer/config/*.bak-*` blob 제거 여부 결정.
      **이력 재작성은 되돌리기 어렵다** — 공개 시점에 한 번만 한다
- [ ] 스캔 대상을 키에서 넓힌다: 개인 IP · Tailscale 주소 · SSID · 사용자명 · 절대경로
- [ ] **§E-2 와 같은 작업으로 묶는다.** 이력 재작성은 두 번 하지 않는다

## E-2. 타사 스크린샷 재배포 ✅ (v1 §7-2 유지 + 이력 범위 추가)

[design/refs/10min_design/](Life_Trainer/docs/design/refs/10min_design/README.md) 에 타사 제품
캡처 17장(약 5MB)이 있다. 그 README 는 **상표 주의를 훌륭히 적어 뒀지만** 그건 *이름을 안 쓴다*
는 판단이고, **이미지 재배포는 별개 문제**다.

이 저장소는 같은 종류의 판단을 이미 두 번 내렸다 — `reference/`(클론 → URL+커밋만),
`models/`(가중치 → 선택 근거만). **같은 원칙을 여기에도 적용한다.**

- [ ] 출처 URL + 캡처 일자만 남기고 이미지는 트리 밖으로
- [ ] **현재 트리에서 지워도 이력에는 남는다** → §E-1 의 이력 정리 범위에 포함

## E-3. 측정 공백 ✅ (v1 §7-1 + 리뷰가 정확히 교정)

**리뷰가 v1 을 정확히 고쳤다: 5분 발열 시험은 24시간 가동의 근거가 아니다.**
v1 은 `thermal-test.sh 300` 하나로 그 주장을 닫으려 했다. 틀렸다. 두 종류로 나눈다.

또 [thermal-test.sh](bench/thermal-test.sh) `:74` 가 **92°C 를 "스로틀링 영역"** 이라 적는다.
그건 **이 프로젝트가 정한 경보값**이지 BSP 임계값이 아니다. 저장소 전체가 *"사양치와 실측을
구분한다"* 를 표방하는데 여기서 그 구분이 깨져 있다. (`:5` 의 "40W 모드" 도 이 보드에 없는 모드다.)

- [ ] `92°C` 라벨을 **"프로젝트 경보값"** 으로 바꾸고, BSP 의 실제 throttle threshold 는
      NVIDIA 문서를 확인해 **따로** 표기한다. 확인 못 하면 *"미확인"* 이라 쓴다
- [ ] **열 안정성 시험** — 30~60분 또는 평형 도달까지 · MAXN 과 15W 각각 ·
      주변 온도 · 케이스 · 팬 RPM · PSU 사양 기록 · **클럭 저하까지** 기록 · 시험 전후 OC 카운터 비교
- [ ] **24시간 애플리케이션 soak** — 실제 요청 반복 · 야간 큐 · 임베딩 · WAL checkpoint ·
      Slack/AW 단절과 복구 · 메모리·zram 추세 · NVMe 쓰기량 · **재부팅 후 전 유닛 자동 복구** ·
      **통제된 전원 차단 후 DB·큐 복구**
- [ ] **15W 1회 측정** — [README ①](README.md) 이 *"성능 수치가 안 붙어서 그림을 안 그린다"* 고
      스스로 적어 뒀다. 그 한 번이 ①의 빈 그림과 전력모드별 와트당 성능을 동시에 푼다
- [ ] 루트 [README](README.md) 에 **"1회 측정"** 명시 — [benchmarks/README](benchmarks/README.md) 는
      이미 적고 있는데 루트에는 없다

## E-4. 의존성·업그레이드 정책 ✅

[pyproject.toml](Life_Trainer/pyproject.toml) 의 의존성이 전부 하한(`>=`)이고 lock 이 없다.
CI 와 운영 기기가 서로 다른 최신 버전을 깔 수 있다.

- [ ] 운영용 constraints/lock 파일 (CI 는 lock 으로, 갱신은 의도적으로)
- [ ] llama.cpp **commit · 빌드 옵션 · CUDA arch** 기록 (§B-1 복구 목록과 같은 것)
- [ ] OpenClaw · Node 지원 버전 matrix + 업그레이드 후 smoke test
- [ ] GitHub Actions 서드파티 액션 버전 고정 정책

## E-5. 기기 보안 기준 ✅

지금 **`0.0.0.0:111`(rpcbind) 과 `0.0.0.0:631`(CUPS)** 이 전체 인터페이스에 열려 있다.

- [ ] rpcbind · CUPS · Avahi 가 **이 기기에 필요한지 판정**하고, 아니면 닫는다
- [ ] 방화벽 기본 정책 (UFW/nftables) · SSH 키 인증 전용 여부 · Tailscale ACL
- [ ] Cloudflare 터널 공개 범위 재확인 (지금 `/ingest/` 만)
- [ ] 서비스 유닛에 `NoNewPrivileges` · `PrivateTmp` · `ProtectSystem` · `ReadWritePaths` · `UMask=0077`
- [ ] Secure Boot · 디스크 암호화는 **적용 여부와 그 판단 근거·위협 모델을 기록**한다.
      개인 개발 장비라 안 할 수 있지만, **안 적으면 다음에 또 검토한다**
- [ ] LICENSE · NOTICE(타사 코드·이미지 출처) · 보안 신고 경로

---

# F. 미룬다

| 무엇 | 왜 |
|---|---|
| **원격 죽은 브랜치 3개 정리** | 셋 다 main 대비 고유 커밋 0 (확인함). 정리는 맞지만 운영 리스크가 아니다 |
| **`bench/` ↔ `benchmarks/` 이름 통일** | 링크 churn 이 크고, 미루는 동안 위험이 안 는다. **리뷰의 P3 판정에 동의한다** |
| **큰 파일 쪼개기** (`cli.py` 등) | 패키지 경계가 워크플로우와 맞아 읽기 어렵지 않다. `llm/tools.py` 만 예외 — 툴 예산이 상한이라 **다음에 툴을 붙일 때**가 그 시점이다 |
| **브랜치+PR 워크플로** | 1인 프로젝트에 과하다. §A-3 의 `make check-fast` pre-push 훅이 같은 값을 싸게 준다 |
| **rootfs A/B 이중화** | 개인 단일 장비에 복잡도가 값을 못 넘는다. 대신 §B-1 의 외부 백업 + 재플래시 runbook |
| **테스트에 네트워크 붙이기** | 픽스처가 빠른 것은 자산이다. 실기기 확인은 §C-4 가 맡는다 |

---

## 커밋 리듬 (v1 에서 유지)

초기에는 며칠치를 몰아 넣었다 (최초 Life Trainer 커밋 141파일, 08-19 가 102파일).
08-16·17·20·22 는 진행 기록은 있는데 커밋이 0건이다. 대가는 bisect 불가 · 리뷰 불가 ·
되돌리기 단위가 하루다. 08-24 에 *"패치가 옆 함수에 들어갔다"* 사고가 났는데 커밋이 작으면
diff 에서 보이는 부류다.

**08-24 이후로는 한 커밋에 한 가지이고 제목이 이유를 말한다. 그 리듬을 유지한다.**
커밋 메시지 언어(현재 한국어·영어 혼재)는 **공개 전에 하나로 정하고**
[CLAUDE.md](Life_Trainer/CLAUDE.md) 에 한 줄 남긴다. **과거 커밋은 고치지 않는다.**

---

## 최종 체크리스트

### P0 — 거짓과 소실

- [ ] `verify-jetpack.sh` 의 L4T · CUDA · DLA 판정 수정 (§A-1)
- [ ] Super Mode 안내를 장비 지원 여부에 맞게 제한 (§A-1)
- [ ] **journal 영속화 + 용량 상한** (§A-4) ← 5분, 나머지 전부의 전제
- [ ] handbook §8·§9 삭제, HANDOFF §4 흡수 (§A-2)
- [ ] `make check` / `check-docs` / pre-push (§A-3)
- [ ] 자동 백업 + 무결성 검사 + **외부 암호화 복제** (§B-1)
- [ ] **실제 복원 시험** 1회 + 복구 대상 runbook (§B-1)
- [ ] BSP 불일치와 `hold` 이유 판정 (§B-2)

### P1 — 재구축과 감시

- [ ] install/uninstall desired-state 통합 + web·llama-embed 추가 (§C-1)
- [ ] 유닛 12개의 `/home/user` 제거 (§C-1)
- [ ] doctor 큐 판정 (창 + backlog + lease + 최소 표본) (§C-2a)
- [ ] 임베딩 **용량** 수정 — 손으로 돌려서 닫지 않는다 (§C-2b)
- [ ] system Node 전환 + rollback 문서화 (§C-2c)
- [ ] `make host-status` — NVMe SMART · OOM · zram · 팬 · 재시작 이력 (§C-3)
- [ ] 외부 dead-man heartbeat (§C-4)
- [ ] rpcbind · CUPS 판정 (§E-5)

### P2 — 공개

- [ ] 전체 ref secret scan · 이력 재작성 여부 결정 (§E-1)
- [ ] 타사 스크린샷 — 현재 트리 + 이력 (§E-2)
- [ ] `thermal-test.sh` 의 92°C 라벨 정정 (§E-3)
- [ ] 30~60분 열 안정성 · **24시간 앱 soak** (§E-3)
- [ ] 15W 측정 · README 에 "1회 측정" 명시 (§E-3)
- [ ] 의존성 lock · 버전 matrix (§E-4)
- [ ] LICENSE · NOTICE · 보안 신고 경로 (§E-5)
- [ ] 디스크 암호화·Secure Boot **판단 기록** (§B-1 · §E-5)

### P3

- [ ] 원격 죽은 브랜치 3개 정리
- [ ] `bench/` ↔ `benchmarks/` 이름 결정

---

## 이 계획의 근거

리뷰를 옮겨 적은 것이 아니라 **주장을 하나씩 이 기기에서 재현한 것**이다.
재확인 명령:

```bash
make status && make links                     # 앱 현황 · 문서 링크
Life_Trainer/.venv/bin/lt doctor              # 17항목
find /usr/local/cuda/lib64 -name 'libcudart.so*'      # 비면 §A-1 재현
find -L /usr/local/cuda/lib64 -name 'libcudart.so*'   # 나오면 오판정 확정
head -1 /etc/nv_tegra_release                 # §A-1 의 L4T 정규식
ls /dev/nvhost*nvdla*                         # §A-1 의 DLA
apt-cache policy nvidia-l4t-core && apt-mark showhold # §B-2
ls -la Life_Trainer/data/backup/              # §B-1
ls -d /var/log/journal; journalctl --user --disk-usage # §A-4
ss -tulnp | grep -E '0\.0\.0\.0:(111|631)'    # §E-5
```

§C-2a 의 잡 상태 조회는 그 절에 그대로 적어 뒀다.
❓ 표시한 항목(OC 이벤트 카운터)은 **재현 못 했으므로 값을 인용하지 않았다.**
