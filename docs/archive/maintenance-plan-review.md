# 정비 계획 리뷰 — Jetson 장기 운영 관점

> ## ★ 2026-08-31 — **아카이브**
>
> 이 문서는 장기 운영 관점의 **외부 리뷰**다. 여기 담긴 주장은
> [`maintenance-plan.md`](maintenance-plan.md) 가 **하나씩 이 기기에서 재현해**
> ✅ 확인 / ✏️ 정정 / ❓ 미검증 으로 판정했다 — 두 건은 과장이었다.
>
> **그러니 이 문서를 사실로 읽지 말 것.** 판정본이 정비 계획 쪽에 있고,
> 거기서 살아남은 항목은 다시 [`docs/issues/`](../../Life_Trainer/docs/issues/) 로 갔다.
> 이 저장소의 반복된 실패 1번이 *"조사 문서를 사실로 믿었다"* 이고,
> 이 파일이 바로 그 조사 문서다.


> 작성일: 2026-08-28  
> 검토 대상: [`maintenance-plan.md`](maintenance-plan.md)  
> 범위: 저장소 신뢰성, Jetson BSP, 장기 운영, 복구, 보안, 공개 준비

## 결론

`maintenance-plan.md`의 방향은 좋지만 현재 상태로 그대로 적용하면 안 된다.

기존 계획은 **문서와 진단기가 현재 상태를 정직하게 말하도록 만드는 작업**에는 강하다.
반면 Jetson 장비의 장기 운영에 필요한 **BSP 일관성, 백업·복원, 전원·스토리지 상태,
장애 감시, 비밀정보 이력 제거**가 부족하다.

따라서 기존 §1~4를 바로 시작하기보다 다음 네 항목을 P0으로 먼저 처리하는 것을 권한다.

1. `verify-jetpack.sh`의 오판정 수정
2. JetPack과 L4T/BSP 버전 불일치 판정
3. 자동·외부 백업과 복원 시험 구축
4. Git 전체 이력의 비밀정보 회전·제거

권장 전체 순서는 다음과 같다.

```text
검증기 수정
→ 비밀 회전·Git 이력 정리
→ 백업·복원 구축
→ BSP 업데이트 정책 확정
→ doctor 확장
→ 문서 정리
→ CI
→ 장기 시험
→ 공개 준비
→ 폴더 이름 정리
```

---

## 검토 시 확인한 현재 상태

```text
make links       링크 435개 · 깨진 것 0개
lt doctor        OK 14 / WARN 3 / FAIL 0
SQLite           PRAGMA quick_check = ok · WAL
서비스           상시 서비스 6개 active
타이머           Life Trainer 타이머 7개 active
```

현재 초록불만으로 정상 운영을 단정할 수는 없다.

- 같은 부팅 세션에서 `llama-server` 기동 타임아웃과 강제 종료가 여러 번 발생했다.
- DB 백업의 최신 파일은 2026-08-23 것이며 원본과 같은 NVMe에만 있다.
- `nvidia-jetpack`은 6.2.3이지만 BSP·커널·부트로더는 36.5.0이다.
- 커널과 커널 헤더 패키지는 hold 상태이며 36.5.2 후보가 존재한다.
- rootfs는 암호화되지 않은 단일 ext4 파티션이다.
- 현재 부팅에서 Orin OC 이벤트 카운터 `oc2=15`, `oc3=37`이 관찰됐다.

---

## P0-1. `verify-jetpack.sh`가 현재 거짓 결과를 생성한다

계획의 핵심은 시스템이 자기 상태를 정직하게 말하게 하는 것이다. 그러나 현재
[`bench/verify-jetpack.sh`](../../bench/verify-jetpack.sh)가 다음 항목을 잘못 판정한다.

### L4T 버전이 빈칸으로 나온다

[`verify-jetpack.sh:21`](../../bench/verify-jetpack.sh)의 정규식이 실제
`/etc/nv_tegra_release` 형식을 읽지 못한다.

실제 값:

```text
# R36 (release), REVISION: 5.0
```

### CUDA 라이브러리가 없다고 나온다

[`verify-jetpack.sh:32`](../../bench/verify-jetpack.sh)는 심볼릭 링크인
`/usr/local/cuda/lib64`를 따라가지 않는다.

실제로 다음 라이브러리는 설치돼 있고 `ldconfig`와 `llama-server`에서도 정상적으로
사용되고 있다.

```text
libcudart
libcublas
libcufft
libcurand
```

파일 탐색보다 `ldconfig -p`, `dpkg-query`, 실제 CUDA smoke test를 조합하는 편이 안전하다.

### DLA가 없다고 나온다

[`verify-jetpack.sh:48`](../../bench/verify-jetpack.sh)은 `/dev/nvhost-nvdla*`만 찾는다.
현재 기기에는 다음 항목이 존재한다.

```text
/dev/nvhost-ctrl-nvdla0
/dev/nvhost-ctrl-nvdla1
DLA0/DLA1 clock cap sysfs
libcudla.so
```

따라서 `DLA 없음`이라고 결론 내릴 근거가 없다. 디바이스 노드 존재 여부만 보지 말고
TensorRT에서 작은 엔진을 DLA 대상으로 빌드·실행하는 smoke test로 판정해야 한다.

### Super Mode 안내가 현재 장비 상태와 충돌한다

[`verify-jetpack.sh:91`](../../bench/verify-jetpack.sh)은 conf 심링크를 바꾸고 Super Mode를
활성화하라고 안내한다. 그러나 이 저장소는 해당 장비의 디바이스 트리와 전력 제한 때문에
그 설정이 부팅 시 되돌아간다고 이미 기록하고 있다.

이 안내는 다음 중 하나로 바꿔야 한다.

- 장비 ID·carrier board·DTB가 지원 조건과 일치할 때만 절차를 노출한다.
- 이 장비에서는 지원되지 않는다고 판정하고 일반 활성화 명령을 숨긴다.

이 수정은 기존 계획의 handbook 정리보다 먼저 해야 한다. 잘못된 생성기가 살아 있으면
문서를 정리한 뒤 다시 틀린 상태를 생성한다.

---

## P0-2. JetPack과 BSP 버전 불일치를 판정한다

현재 설치 상태:

| 구성 | 설치 버전 | APT 후보 |
|---|---:|---:|
| `nvidia-jetpack` | 6.2.3 | 6.2.3 |
| `nvidia-l4t-core` | 36.5.0 | 36.5.2 |
| `nvidia-l4t-bootloader` | 36.5.0 | 36.5.2 |
| `nvidia-l4t-kernel` | 36.5.0 | 36.5.2 |

추가로 `nvidia-l4t-kernel`과 `nvidia-l4t-kernel-headers`가 hold 상태다.

NVIDIA 공식 기준으로 JetPack 6.2.3은 Jetson Linux 36.5.2를 포함한다. 또한 BSP OTA는
펌웨어, 커널 드라이버와 사용자 공간 패키지가 일관된 버전을 유지하도록 설계되어 있으며,
서로 다른 릴리스 패키지를 혼합하는 것을 권장하지 않는다.

- [NVIDIA JetPack 6.2.3](https://developer.nvidia.com/embedded/jetpack-sdk-623)
- [Jetson Linux 36.5.2 — Software Packages and Update Mechanism](https://docs.nvidia.com/jetson/archives/r36.5.2/DeveloperGuide/SD/SoftwarePackagesAndTheUpdateMechanism.html)

무작정 hold를 풀고 업그레이드하지 말고 다음 절차를 계획에 넣는다.

1. 커널 hold 이유와 당시 회피하려던 문제를 확인한다.
2. DB, 설정, systemd 외부 배선과 에이전트 설정을 백업한다.
3. `apt` 변경 목록과 제거 예정 패키지를 검토한다.
4. 유지보수 창에서 BSP 전체를 같은 릴리스로 갱신한다.
5. 재부팅 후 bootloader, kernel, userspace 버전이 일치하는지 확인한다.
6. CUDA, llama.cpp, TensorRT/DLA, OpenClaw, Life Trainer 타이머를 smoke test한다.
7. 실패 시 재플래시와 데이터 복원으로 돌아가는 절차를 준비한다.

`lt doctor` 또는 별도 `jetson-doctor`가 다음도 검사해야 한다.

- JetPack 메타 패키지와 L4T 구성요소 버전의 대응
- hold된 NVIDIA 패키지
- 설치 버전과 APT 후보 버전
- 재부팅이 필요한 업데이트의 존재

---

## P0-3. 백업을 실제 운영 체계로 만든다

[`lt backup`](../../Life_Trainer/lifetrainer/cli.py)은 SQLite 온라인 백업 API를 사용하므로
구현 방향은 올바르다. 그러나 현재 다음이 없다.

- 자동 실행 타이머
- 원본 NVMe 밖의 복제본
- 암호화
- 보존 정책
- 백업 무결성 검사
- 실제 복원 시험
- 복원 성공 여부 알림

현재 백업은 원본 DB와 같은 NVMe의 `Life_Trainer/data/backup/`에만 있다. 이 사본은
실수로 DB를 수정했을 때는 유용하지만 NVMe 고장, 도난, rootfs 손상에는 도움이 되지 않는다.

### 권장 백업 정책

- 매일 `lt backup` 실행
- 생성 직후 백업 DB에 `PRAGMA quick_check`
- 해시 파일 생성
- 암호화된 별도 장치 또는 off-device 저장소로 복제
- 일간 7개, 주간 4개, 월간 3개 등 보존 정책 적용
- 월 1회 임시 경로에 복원
- 복원 DB의 schema version과 주요 테이블 행 수 확인
- 성공·실패 기록과 외부 알림

DB뿐 아니라 다음도 복구 대상에 포함한다.

- `config/lifetrainer.toml`
- OpenClaw 설정과 에이전트 등록 정보
- systemd 활성화 목록과 drop-in
- 세션·ingest secret을 다시 만드는 절차
- Node, llama.cpp, OpenClaw의 정확한 버전
- 모델 파일명과 검증 해시

개인 활동 기록과 창 제목을 저장하므로 rootfs 또는 데이터 파티션 암호화 여부도 명시적으로
결정해야 한다. Jetson Orin은 OP-TEE와 LUKS 기반 디스크 암호화를 지원한다.

- [NVIDIA Jetson Disk Encryption](https://docs.nvidia.com/jetson/archives/r36.5.2/DeveloperGuide/SD/Security/DiskEncryption.html)

고가용성이 중요하다면 rootfs A/B도 선택지다. Jetson Linux는 Orin에서 bootloader와
rootfs A/B 연동 및 부팅 실패 시 fail-over를 지원한다. 다만 개인용 단일 장비에는 복잡도가
클 수 있으므로, 적용하지 않는다면 **외부 백업 + 재플래시 runbook**을 반드시 갖춘다.

- [NVIDIA Root File System Redundancy](https://docs.nvidia.com/jetson/archives/r36.5.2/DeveloperGuide/SD/RootFileSystem.html)

---

## P0-4. 공개 전 Git 전체 이력의 비밀정보를 제거한다

[`Life_Trainer/.gitignore`](../../Life_Trainer/.gitignore)는 실제 Serper API 키가 포함된 설정
백업 네 개가 과거 커밋 `49581fa`에 들어갔다고 기록한다. 이 커밋은 현재 `origin/main`
이력에 포함돼 있다.

현재 트리에서 파일을 지웠거나 `.gitignore`에 추가했어도 Git 이력의 blob은 남아 있다.
따라서 공개 전 다음을 수행해야 한다.

1. 백업 파일에 포함됐던 모든 API 키와 토큰을 식별한다.
2. Serper, Slack, OpenClaw, ingest/session 관련 비밀을 회전 또는 폐기한다.
3. 모든 branch와 tag를 대상으로 secret scan을 수행한다.
4. `git filter-repo` 등으로 민감 blob을 이력에서 제거한다.
5. 정리된 모든 ref를 다시 검사한다.
6. 원격 이력 교체가 필요한 경우 협업자와 clone 재생성 절차를 공지한다.

타사 스크린샷도 마찬가지다. §7-2처럼 현재 트리에서 이미지만 제거해도 과거 Git 이력에는
남는다. 재배포 판단이 `제거`라면 이력 정리 범위에도 포함해야 한다.

공개 전 체크리스트에는 다음도 추가한다.

- 라이선스 파일
- 타사 코드·이미지 NOTICE와 출처
- 전체 이력 secret scan
- 개인 IP, SSID, 사용자명, 절대경로 검사
- 재현 가능한 설치 절차
- 지원 JetPack/L4T 범위
- 보안 문제 신고 경로

---

## 기존 계획 항목별 판정

| 항목 | 판정 | 보완점 |
|---|---|---|
| §1 문서 현황 제거 | 적용 | 잘못된 `environment.md` 생성기부터 수정한다 |
| §2 doctor WARN 제거 | 수정 후 적용 | 초록불 자체가 아니라 정확한 장애 신호가 목적이어야 한다 |
| §3 숫자 CI | 적용 | 단순 grep보다 `make check-docs`와 명시적 예외 목록을 둔다 |
| §4 브랜치 정리 | 적용 가능 | 세 브랜치 모두 main 대비 고유 커밋 0을 확인했지만 우선순위는 낮다 |
| §5 폴더 이동 | 보류 | 운영·복구 항목보다 가치가 낮고 링크 churn이 크다 |
| §6 doctor 자동화 | 크게 확장 | 앱 doctor, 호스트 doctor, 외부 heartbeat로 나눈다 |
| §7 공개 준비 | 재작성 | 비밀 이력, 라이선스, 복구성, 장기 시험을 포함한다 |

---

## §2 doctor 보완 의견

### 큐 판정

누적 실패율을 최근 7일 `finished_at` 창으로 바꾸는 방향은 맞다. 다만 최근 실패율 하나만
보면 다음 장애를 놓칠 수 있다.

- `queued` 중 가장 오래된 잡의 나이
- `running` 상태로 멈춘 잡과 만료된 lease
- 잡 종류별 실패율
- 대표 오류뿐 아니라 마지막 실패 시각
- 재시도 횟수와 실패 후 성공 여부
- 표본이 매우 작을 때 비율을 과대 해석하지 않기 위한 최소 표본 수

`오늘 초록불이 되게 한다`가 아니라 **새 장애를 빠르게 발견하는 판정식**을 목표로 해야 한다.

### 임베딩 판정

단순히 `doc_embedding` 행 수와 문서 수를 비교하는 것만으로는 부족하다.

- 현재 설정의 model과 dim으로 만들어졌는지
- `source_hash`가 현재 `title + abstract + summary`와 일치하는지
- 가장 오래 밀린 문서가 언제 들어왔는지
- 야간 처리량이 일일 유입량보다 큰지
- 서버 재시작과 MemoryMax/OOM 발생 여부

야간 종료 경로에는 이미 [`embed_pending`](../../Life_Trainer/lifetrainer/cli.py)이 연결돼 있다.
현재 문제는 배선이 아예 없는 것이 아니라 처리 한도와 유입량이 맞지 않아 backlog가 계속
남을 수 있다는 점이다. 수동 `lt embed --all` 실행만으로 닫지 말고 용량 계획을 고쳐야 한다.

### OpenClaw 시스템 Node 전환

현재 `/usr/local/bin/node`는 설치돼 있지만 doctor가 찾은 `openclaw`는 여전히 nvm 경로다.
따라서 시스템 Node 전환을 적용할 근거가 있다. 다만 다음을 함께 확인한다.

- 전환 전 현재 gateway unit 백업
- OpenClaw 버전 기록
- system Node에서 에이전트와 Slack 위임 smoke test
- rollback 명령
- OpenClaw 업그레이드 후 cron schema 패치와 에이전트 재설치 검증

---

## §3 CI 보완 의견

금지 패턴 검사는 유효하지만 정규식 하나로 모든 숫자를 막으면 오탐과 예외가 빠르게 늘어난다.

권장 구조:

```text
make check
├── make links
├── make check-docs
├── systemd-analyze verify 또는 정적 unit 검사
├── shellcheck
└── 빠른 단위 테스트
```

`check-docs`는 다음 원칙을 갖는 것이 좋다.

- 변하는 운영 숫자의 종류를 명시한다.
- 예외 경로를 코드에 흩뿌리지 않고 한 목록에 둔다.
- snapshot·HISTORY·progress가 왜 예외인지 테스트한다.
- 금지해야 할 예제와 허용해야 할 예제를 fixture로 둔다.
- 문서 숫자를 가능하면 명령 출력 또는 생성 블록으로 대체한다.

pre-push도 `make links` 하나보다 빠른 검사를 묶은 `make check-fast`를 호출하도록 한다.

---

## §6 doctor 자동화는 세 계층으로 나눈다

### 1. 애플리케이션 doctor

현재 `lt doctor`의 역할을 유지한다.

- DB와 schema
- ActivityWatch
- LLM model ID
- Slack
- 큐
- 임베딩
- 에이전트 배선과 예산

### 2. Jetson 호스트 doctor

별도 `jetson-doctor` 또는 `make host-status`로 다음을 검사한다.

- BSP, kernel, bootloader 버전 일치
- NVIDIA 패키지 hold와 pending update
- NVMe SMART 수명, media error, unsafe shutdown
- ext4 오류와 읽기 전용 remount
- 디스크 용량, inode, 증가율
- OOM kill과 zram 사용량
- systemd 재시작 횟수와 마지막 실패 결과
- `nvfancontrol` 상태와 fan RPM
- 온도와 thermal throttling 이벤트
- 전력모드와 GPU/EMC 상한
- OC·저전압 이벤트 카운터
- 최근 부팅 원인과 watchdog 상태
- 보안 업데이트 지연

현재 부팅에서 다음 값이 관찰됐다.

```text
oc1_event_cnt=0
oc2_event_cnt=15
oc3_event_cnt=37
fan RPM≈2140
```

NVIDIA는 Orin의 저전압·평균 과전류·순간 과전류 이벤트를 sysfs 카운터로 확인하도록
제공한다. 단순 온도 측정 외에 전원 어댑터, 케이블, carrier board와 MAXN 순간부하를 함께
조사해야 한다.

- [NVIDIA Platform Power and Performance](https://docs.nvidia.com/jetson/archives/r36.5.2/DeveloperGuide/SD/PlatformPowerAndPerformance/JetsonOrinNanoSeriesJetsonOrinNxSeriesAndJetsonAgxOrinSeries.html)

### 3. 외부 heartbeat

같은 장비가 보내는 Slack DM은 다음 장애를 알릴 수 없다.

- 장비 전원 단절
- 전체 네트워크 단절
- OS 부팅 실패
- systemd user manager 미기동
- Slack 발송 경로 자체 장애

따라서 외부 시스템이 주기적인 heartbeat를 기대하고, 일정 시간 들어오지 않으면 알리는
dead-man monitor가 필요하다.

권장 주기:

- 5분: 서비스 liveness와 외부 heartbeat
- 1시간: 큐 backlog, 최근 수집, 메모리와 디스크
- 1일: 전체 app/host doctor, 패키지와 NVMe 상태
- 1개월: 실제 백업 복원 시험

---

## systemd와 설치 재현성

현재 [`Life_Trainer/scripts/install-units.sh`](../../Life_Trainer/scripts/install-units.sh)은
실제로 활성화된 서비스를 완전히 재현하지 못한다.

누락 또는 불일치 항목:

- `lifetrainer-web` 활성화
- `llama-embed` 활성화
- 현재 bolt 모드에서 필요한 `lifetrainer-slack`
- runtime과 gateway 설치 순서
- 서비스별 smoke test

반대로 [`uninstall-units.sh`](../../Life_Trainer/scripts/uninstall-units.sh)은 다음을 누락한다.

- nightly와 nightly-stop
- web
- llama-embed

설치·제거·상태 검사가 같은 desired-state 목록을 공유하도록 바꾸는 것이 좋다.

또한 unit 파일의 `/home/user` 절대경로는 새 사용자나 다른 설치 위치에서 깨진다. `%h`,
EnvironmentFile 또는 설치 시 unit을 생성하는 방식으로 경로를 한 곳에서 관리해야 한다.

[`runtime/systemd/llama-server.service`](../../runtime/systemd/llama-server.service)의 readiness 처리도
보완 대상이다. 현재 `ExecStartPost`가 `/health`를 최대 600초 동안 반복하고, 실패하면
systemd가 프로세스를 강제 종료한다. 실제 journal에도 이 타임아웃이 여러 번 나타났다.

다음 지표를 doctor와 상태 출력에 포함한다.

- 현재 active 여부
- `NRestarts`
- 마지막 종료 코드
- 마지막 성공 기동 시각
- readiness 소요 시간
- 최근 24시간 timeout/OOM 횟수

현재 active라는 사실만 출력하면 당일 반복 장애가 숨겨진다.

---

## 보안 운영에서 빠진 부분

공개 전 개인정보 제거와 별개로 실제 장비 보안 기준도 정해야 한다.

- SSH 키 인증만 허용할지
- UFW/nftables 기본 정책
- Tailscale ACL과 관리자 권한
- Cloudflare tunnel의 공개 범위
- 사용하지 않는 rpcbind, Avahi, CUPS socket 비활성화
- 서비스 secret 파일 권한과 `UMask=0077`
- `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem`, `ProtectHome`, `ReadWritePaths`
- Secure Boot와 디스크 암호화 적용 여부
- 보안 업데이트 적용 주기

현재 네트워크에는 SSH 외에도 rpcbind와 CUPS 계열 포트가 전체 인터페이스에 열려 있다.
필요한 서비스인지 판정하고 필요 없으면 닫아야 한다.

Secure Boot는 단순 설정 변경이 아니라 키 관리와 fuse 작업을 포함하고 실수 시 복구 비용이
크다. 개인 개발 장비라 적용하지 않을 수 있지만, 그 판단과 물리적 위협 모델은 기록해 둔다.

- [NVIDIA Secure Boot](https://docs.nvidia.com/jetson/archives/r36.5.2/DeveloperGuide/SD/Security/SecureBoot.html)

---

## 5분 발열 테스트는 24시간 가동 근거가 아니다

[`bench/thermal-test.sh`](../../bench/thermal-test.sh)의 5분 실행은 빠른 냉각 이상 탐지에는 유용하다.
하지만 24시간 운영 주장의 근거로는 부족하다.

또한 스크립트의 92°C는 보수적 운영 경보값으로는 사용할 수 있지만 `스로틀링 영역`이라는
표기는 부정확하다. NVIDIA의 Orin NX/Nano BSP thermal specification은 CPU/GPU software
throttling을 99°C로 제시한다.

- [NVIDIA Orin Thermal Specifications](https://docs.nvidia.com/jetson/archives/r36.5.2/DeveloperGuide/SD/PlatformPowerAndPerformance/JetsonOrinNanoSeriesJetsonOrinNxSeriesAndJetsonAgxOrinSeries.html)

권장 시험을 두 종류로 나눈다.

### 열적 안정성 시험

- 30~60분 또는 온도가 평형에 도달할 때까지 실행
- MAXN과 15W에서 각각 반복
- 주변 온도, 케이스, 팬 프로필과 RPM 기록
- PSU와 케이블 사양 기록
- 온도뿐 아니라 CPU/GPU/EMC 클럭 저하 기록
- 시험 전후 OC 이벤트 카운터 비교
- `jetson_clocks` 사용 여부 명시

85°C와 92°C는 각각 `운영 경보`, `위험 여유 감소`처럼 프로젝트 자체 정책으로 표시하고,
NVIDIA BSP의 실제 throttle threshold와 구분한다.

### 24시간 애플리케이션 soak

- 실제 llama.cpp 요청 반복
- 야간 큐 적재와 worker 처리
- 임베딩 처리
- SQLite WAL 쓰기와 checkpoint
- Slack·ActivityWatch 일시 단절과 복구
- 서비스 재시작과 readiness
- 메모리·zram 증가 추세
- NVMe 쓰기와 SMART 변화
- 재부팅 후 모든 timer/service 자동 복구
- 통제된 전원 차단 후 DB와 큐 복구 확인

`5분 동안 뜨겁지 않았다`와 `24시간 서비스가 장애를 복구하며 유지됐다`는 서로 다른 주장이다.

---

## 의존성과 업그레이드 정책

현재 Life Trainer의 Python 의존성은 대부분 하한만 있고 lock 파일이 없다. CI와 운영 기기가
서로 다른 최신 버전을 설치할 수 있다. OpenClaw 업그레이드는 cron schema 패치를 다시
적용해야 하고, Node 설치는 `/usr/local`에 직접 병합한다.

추가 권장 사항:

- Python 운영 의존성 lock 또는 constraints 파일
- llama.cpp commit, build option과 CUDA arch 기록
- OpenClaw와 Node의 지원 버전 matrix
- 업그레이드 전 설정·unit 백업
- 업그레이드 후 자동 smoke test
- rollback 가능한 이전 바이너리 또는 설치 패키지 보관
- CI에서 깨끗한 환경 설치 시험
- GitHub Actions와 외부 action의 버전 고정 정책

업데이트를 막는 것과 검증 없이 최신으로 따라가는 것 모두 장기적으로 위험하다. 일정한
검토 주기와 되돌릴 수 있는 절차를 갖추는 것이 핵심이다.

---

## 최종 적용 체크리스트

### P0 — 기존 계획보다 먼저

- [ ] `verify-jetpack.sh`의 L4T, CUDA, DLA 판정 수정
- [ ] Super Mode 안내를 장비 지원 여부에 맞게 제한
- [ ] JetPack 6.2.3 / L4T 36.5.0 불일치와 hold 이유 판정
- [ ] 자동 SQLite 백업과 외부 암호화 복제
- [ ] 실제 복원 시험
- [ ] 과거 커밋에 들어간 모든 비밀 회전
- [ ] Git 전체 이력 secret scan과 필요 시 history rewrite

### P1 — 운영 안정성

- [ ] `lt doctor` 큐·임베딩 판정 개선
- [ ] Jetson host doctor 추가
- [ ] 외부 dead-man heartbeat 추가
- [ ] NVMe SMART, OOM, zram, fan, OC 이벤트 감시
- [ ] systemd 설치·제거 목록 통합
- [ ] unit 절대경로 제거
- [ ] llama-server readiness와 재시작 이력 개선
- [ ] 보안 업데이트 주기 확정
- [ ] 불필요한 네트워크 서비스와 포트 정리

### P2 — 문서와 공개

- [ ] 기존 계획 §1 문서 현황 정리
- [ ] 숫자·상태 CI와 `make check-fast`
- [ ] 30~60분 thermal soak
- [ ] 24시간 애플리케이션 soak
- [ ] 15W/MAXN 반복 측정과 환경 기록
- [ ] LICENSE, NOTICE, 보안 신고 경로 추가
- [ ] 타사 스크린샷의 현재 트리와 Git 이력 처리
- [ ] 공개 전 개인정보·IP·절대경로 전체 검사

### P3 — 낮은 우선순위

- [ ] main에 완전히 흡수된 원격 브랜치 정리
- [ ] `bench/`와 `benchmarks/` 폴더 구조 변경 여부 결정

폴더 이름 정리는 가장 나중에 해도 운영 리스크가 증가하지 않는다. 반대로 백업, BSP 버전,
비밀정보와 전원 이벤트는 미루는 동안 위험이 계속 남는다.
