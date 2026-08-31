# 세대 전환 — 실행 계획과 참조 지도

> 작성 2026-08-31 · **상태: ✅ 전부 끝났다. 재부팅 검증까지 통과했다 (13:39 부팅)**
> 계획의 근거·측정값은 [§2](#2-왜-하는가--측정된-근거-셋) 에 있다.
> 지금 구조의 정본은 [README §디렉토리 구조](../../README.md),
> 왜 이 모양인지는 [`docs/folder-structure.md`](../folder-structure.md) 다.

---

## 1. 어디까지 왔나

```
✅ 단계 0   태그 pre-restructure-2026-08-31 + 백업 3종 + sha256
✅ 게이트   사용자가 3개 파일을 내려받고 sha256 대조
✅ 단계 1   ff-only 머지 · push · stale 브랜치 3개 · git gc (27M → 13M)
✅ 단계 2   끊긴 참조 복구 + 코드→문서 검사 신설 + HISTORY 한 건
✅ 단계 3   중복 제거 + 낡은 수치 정리 + 정비계획을 issues/ 로
✅ 단계 4   measure/ · operate/ · life-trainer/ · refs/ 로 재구조화
✅ 마지막   **재부팅 검증** — linger 로 로그인 없이 전부 떴다. 실패 유닛 0
            `verify-boot` 통과 · `lt doctor` 17/17 · :8080·:8081 응답 ·
            타이머 11개 전부 다음 실행 시각을 갖는다
```

### 무엇이 나왔나 — 계획에 없던 것들

계획은 *"참조를 고치고 폴더를 옮긴다"* 였는데, 실제로 값이 컸던 것은 **검사가 없던 자리**였다.

| 어디서 | 무엇 |
|---|---|
| 단계 2 | 코드→문서 참조 검사를 만들자 **틀린 절 번호 둘**이 같이 나왔다. 개명 때 깨진 게 아니라 **처음부터 틀려 있었다** |
<!-- check-docs: ok — 아래 줄은 **그때 서로 달랐던 옛 값**을 증거로 인용한다 -->
| 단계 3 | 중복을 지우자 **낡은 수치 다섯**이 나왔다. `lt doctor` 항목 수를 네 곳은 17, 한 곳은 15 로 적고 있었고 `check-docs` 는 전부 통과시켰다 |
| 단계 3 | `backup.sh` 가 **자기 검사가 만든 `-wal`·`-shm`** 을 매일 백업 옆에 남기고 있었다 ([HISTORY](../../life-trainer/HISTORY/2026-08-31-the-backup-kept-what-its-own-check-created.md)) |
| 단계 4 | `operate/tools/install.sh` 가 유닛 목록을 **본문에 박고** 있어, 심링크를 지우자 감시 타이머 둘이 안 돌아왔다 ([HISTORY](../../life-trainer/HISTORY/2026-08-31-the-symlinks-that-were-already-there-hid-the-gap.md)) |
| 단계 4 | 저장소 규칙이 **홈 아래 경로**까지 바꿔 `~/models` 가 `~/refs/models` 가 됐다. llama-server·llama-embed 가 모델을 못 찾았다 |

**공통점**: 전부 *"한 곳에서 고쳤으니 그 부류가 닫혔다"* 였다.

### 되돌리는 법

```bash
git reset --hard pre-move-2026-08-31        # 재구조화 직전
git reset --hard pre-restructure-2026-08-31 # 세션 시작 시점
```

★ **git 만으로는 안 돌아온다.** venv 의 셸뱅 23개 · `~/.openclaw/openclaw.json` ·
`~/.config/systemd/user/` 의 심링크는 저장소 밖이다. 되돌리면 이 셋도 같이 되돌린다
(`operate/tools/install.sh` · `life-trainer/scripts/install-units.sh` 를 다시 돌리면 된다).

### 백업 3종 (`~/` 에 있다)

| 파일 | 무엇 | 왜 |
|---|---|---|
| `jetson-history-2026-08-31.bundle` | 전체 git 이력 (브랜치·태그) | GitHub 없이도 복원 가능 |
| `jetson-tree-2026-08-31.tar.gz` | 작업트리 전부 (`.venv` 제외, `.git` 포함) | **DB·설정·nsys 프로파일이 git 에 없다** |
| `recovery-bundle-*.tar.gz` | 저장소 밖 자산 | `~/.openclaw` · 터널 자격증명 · WiFi 드라이버 |

뒤의 둘은 `0600` 이고 **토큰·개인 활동 기록이 들어 있다.** 옮길 때 암호화한다.

---

## 2. 왜 하는가 — 측정된 근거 셋

**공개는 하지 않는다. LICENSE 도 안 붙인다** (2026-08-31 결정).
그래서 [`docs/folder-structure.md`](../folder-structure.md) 가 재구조화의 방아쇠로 삼은
*"공개 직전"* 은 안 당겨진다. 근거를 다시 세운 것이 아래 셋이다.

1. **구조가 실제로 비용을 냈다.** `bench/` 가 측정·운영·검사·조사를 한 폴더에 담고 있고,
   2026-08-28 하루에 **운영 스크립트 9개가 규칙이 없어서 잘못 들어갔다.**
   `measure/` 는 `bench/ · measure/results/ · *.md` 라는 **저장소 구조의 복제본**이다.
2. **문서가 같은 말을 여러 번 한다.** "문서를 어디에 쓰는가" 표가 다섯 곳,
   "반복된 실패" 목록이 세 곳, 한국어 토크나이저 실측이 세 곳에 있다.
3. **★ 참조가 이미 두 번 끊겼는데 아무도 몰랐다.**

   ```
   05e2d5d (08-28)  openclaw-setup → operate/notes/agent-gateway.md 로 개명
                    코드 14곳은 그대로 → 지금 없는 파일을 가리킨다
   08-27            known-issues.md → docs/issues/ 로 분할
                    코드 14곳은 그대로 → §7 같은 절이 이제 없다
   ```

   `operate/tools/check-links.sh` 는 `*.md` 만 본다. **코드 주석 안의 문서 참조는 검사 대상이 아니다.**

---

## 3. 두 원칙 (2026-08-31 확정)

### 원칙 1 — 코드가 인용하는 문서는 **새 버전을 가리키게 올린다**

문서를 얼려두는 게 아니라 **코드 쪽 참조를 갱신한다.**
지키지 않은 결과가 §2-3 의 28곳이다.

### 원칙 2 — 완결된 것은 **간단히 남기고 넘어간다. 묵살은 안 된다**

- 중복을 한 곳으로 줄일 때 → 나머지에 **가리키는 한 줄**을 남긴다 (빈칸으로 두지 않는다)
- 완료 항목 → **완료 표시 한 줄**로 접는다
- 검사기가 못 잡는 걸 발견하면 → **제외 목록으로 침묵시키지 않는다. 검사를 넓힌다**

### 파생 규칙 — 세 종류의 내용

| | 무엇 | 새 버전과 맞아야 하나 |
|---|---|---|
| **A 코드** | py · sh · systemd 유닛 | ✅ |
| **B 기록** | `HISTORY/` · `docs/progress/` · `docs/archive/` · `measure/results/` | ❌ **옛 버전과 맞으면 된다** |
| **C 현황** | `README` · `HANDOFF` · `handbook` · `architecture` · `docs/issues/` · `desired-state.txt` | ✅ |

**단, 코드가 인용하면 폴더가 어디든 A 에 묶인다** (원칙 1).
`HISTORY/2026-08-18-search-fallback-noise.md` 는 기록 폴더에 있지만 소스가 인용한다.

---

## 4. 단계별 실행 — **무엇을 읽고 시작하나**

### ~~단계 1 — 머지와 이력 정리~~ ✅

```bash
git checkout main && git merge --ff-only llama-server-memory-growth
git push origin main
git branch -d llama-server-memory-growth
git push origin --delete docs-refactor life-trainer-web-agent qwen3-openclaw-agent
git branch -d docs-refactor life-trainer-web-agent qwen3-openclaw-agent
git gc --aggressive --prune=now
```

**읽을 것**: 없음. 세 브랜치는 전부 main 의 조상이고(`git branch --merged main -a` 로 확인),
`docs-refactor` 의 팁은 태그 `pre-agent-2026-08-23` 이 보존한다.

> ★ 이 저장소는 PR 을 한 번도 안 열었다. push 하면 CI 의 `pull_request` 가 아니라
> `push: main` 트리거가 돈다. **미머지 5커밋이 CI 를 처음 통과하는 시점이다.**

---

### ~~단계 2 — 끊긴 참조 복구 + 검사 신설~~ ✅

| 무엇 | 어디로 |
|---|---|
| `openclaw-agent.md §N` (코드 14곳) | [`operate/notes/agent-gateway.md`](../../operate/notes/agent-gateway.md) §N — 절 번호는 살아 있다 |
| `known-issues §N` (코드 14곳) | 해당 [`docs/issues/`](../../life-trainer/docs/issues/) 또는 옮겨간 [`HISTORY/`](../../life-trainer/HISTORY/) |

**읽을 것**

| 문서 | 왜 |
|---|---|
| [`operate/notes/agent-gateway.md`](../../operate/notes/agent-gateway.md) | 절 번호가 코드에서 인용된다. **§ 구조를 바꾸지 말 것** |
| [`life-trainer/docs/known-issues.md`](../../life-trainer/docs/known-issues.md) | 옛→새 리디렉트 표. `§N` 이 어느 이슈로 갔는지 여기 있다 |
| [`docs/README.md`](../README.md) | *"agent-gateway.md §4 는 코드에서 직접 참조된다 — 절을 재배열하지 말 것"* 경고의 출처 |
| [`operate/tools/check-docs.sh`](../../operate/tools/check-docs.sh) | `--self-test` 로 fixture 를 쓰는 방식. 새 검사도 같은 꼴로 만든다 |

**찾는 법**

```bash
git grep -nE 'openclaw-agent\.md|known-issues' -- '*.py' '*.sh'
git grep -ohE '[A-Za-z0-9_./-]+\.md' -- '*.py' '*.sh' '*.service' '*.timer' Makefile | sort -u
```

**만들 것**: `check-links.sh` 가 코드 주석의 `*.md` 참조도 `git ls-files` 와 대조하게 넓힌다.
`bench/fixtures/` 에 **없는 문서를 인용하는 must-fail 예제**를 같이 넣는다.

**남길 것**: 깨진 가정(*"문서를 옮기면 참조도 따라온다"*)이라 `HISTORY/` 에 한 건.

---

### ~~단계 3 — 중복 제거 + 정비계획 이관~~ ✅

| 중복 | 남길 곳 |
|---|---|
| "문서를 어디에 쓰는가" 표 (다섯 곳) | [`life-trainer/CLAUDE.md`](../../life-trainer/CLAUDE.md). 나머지는 링크 한 줄 |
| "반복된 실패" 목록 (세 곳) | `life-trainer/CLAUDE.md` (이미 있다) |
| 롤업 600초 계산 순서 (`handbook §3-2` · `architecture §2-2`) | [`architecture.md`](../../life-trainer/docs/architecture.md) — 판정 근거가 그쪽 역할 |
| 한국어 토크나이저 실측 (세 곳) | [`measure/findings/performance.md`](../../measure/findings/performance.md) |
| `HANDOFF` 의 `### B.`·`### C.` 가 각각 두 번 | 한 벌만 |

**정비계획 두 문서** — [`maintenance-plan.md`](../archive/maintenance-plan.md) 는 P0·P1 완료,
[`maintenance-plan-review.md`](../archive/maintenance-plan-review.md) 는 **인바운드 링크 0인 고아**.
**살아 있는 항목만 `docs/issues/` 로 옮기고 두 문서는 `docs/archive/` 로.**

| → 새 이슈 (`0020~`) | 등급 |
|---|---|
| `thermal-test.sh` 의 92°C 라벨이 틀렸다 | (없음) |
| 지속 부하 발열 미측정 — **"24시간 가동" 주장의 근거가 없다** | `p-` |
| 15W·25W 전력모드 미측정 | `p-` |
| 의존성 lock · 버전 matrix 없음 | `p-` |
| 디스크 암호화·Secure Boot **판단 기록**이 없다 | `p-` |
| `converse` 은퇴 판단 — 2~3주 뒤 실사용 빈도 보고 | `p-` |
| **`backup.sh` 가 `quick_check` 뒤 `-wal`·`-shm` 을 안 지운다** | (없음) |

> 마지막 항목은 2026-08-31 백업 중에 발견했다. `data/backup/` 에 0바이트 `-wal`·`-shm`
> 이 백업마다 남는다. `make-recovery-bundle.sh` 는 같은 자리에서 `rm -f` 를 하는데
> `backup.sh` 는 안 한다. [`CLAUDE.md §4`](../../CLAUDE.md) 가 적은 그 결함이다 —
> **복원본이 "안전하게 뜬 백업"인지 "돌아가는 WAL DB 를 복사한 것"인지 구분이 안 된다.**

**옮기지 않는 것**: LICENSE·NOTICE·타사 스크린샷·개인정보 일반화·이력 재작성 —
**전부 공개 전용이고 공개를 안 한다.** `h-0007`·`h-0008`~`h-0010` 은 **이미 issues/ 에 있다.**

**읽을 것**

| 문서 | 왜 |
|---|---|
| [`life-trainer/docs/issues/README.md`](../../life-trainer/docs/issues/README.md) | 파일명 접두어가 곧 등급. 번호는 `ls \| grep -oE '[0-9]{4}' \| sort -n \| tail -1` 의 +1 |
| [`life-trainer/HISTORY/README.md`](../../life-trainer/HISTORY/README.md) | 색인을 같이 갱신해야 한다 |
| [`life-trainer/CLAUDE.md`](../../life-trainer/CLAUDE.md) | 기록 규칙 — 버그는 고친 코드가 아니라 **깨진 가정**을 쓴다 |

---

### ~~단계 4 — 폴더 재구조화~~ ✅ (재부팅 검증만 남음)

목표 구조는 [`docs/folder-structure.md`](../folder-structure.md) 의 §목표 구조 그대로.
`measure/`(잰 것) · `operate/`(도는 것) · `life-trainer/`(만든 것) · `docs/` · `refs/`.

**읽을 것 — 이 넷은 반드시**

| 문서 | 무엇을 얻나 |
|---|---|
| [`docs/folder-structure.md`](../folder-structure.md) | 목표 구조 · **안 바꾸는 것**(systemd 3곳은 소유자 기준이라 유지) · `reference` 4곳 통일 |
| [`README.md`](../../README.md) `## 디렉토리 구조` | **지금 구조의 정본.** 이걸 새 구조로 바꾸는 게 마지막 작업 |
| [`CLAUDE.md`](../../CLAUDE.md) `§6` | *"재는 것인가 돌리는 것인가"* — 파일을 어디 둘지 가르는 질문 |
| [`operate/README.md`](../../operate/README.md) | `systemd/` 가 세 곳인 이유(공유·앱·게이트웨이). **합치면 안 된다** |

**치환 대상과 잡아주는 것**

| 종류 | 잡아주는 것 |
|---|---|
| 마크다운 링크 | ✅ `make links` |
| 코드→문서 참조 | ✅ **단계 2 에서 만든 검사** |
| 스크립트·유닛의 하드코딩 경로 | ❌ `git grep` 으로 직접 |
| CI 워크플로 `.github/workflows/ci.yml` | ❌ push 해야 안다 |
| systemd 유닛의 `%h/project/project-jetson/...` | ❌ **재부팅해야 안다** |

```bash
git grep -nE '(^|[^a-z/])(bench|benchmarks|research|runtime|results|figures|Life_Trainer)/' \
  -- '*.sh' '*.py' 'Makefile' '*.yml' '*.service' '*.timer'
```

**★ [`operate/notes/agent-gateway.md`](../../operate/notes/agent-gateway.md) 의 절 번호는 재배열하지 않는다.**
경로는 바뀌어도 되지만 `§4-6` 형태의 인용이 코드에 널려 있다.

**순서**

1. `git mv` (한 커밋 — 이름 변경 추적이 살아 있게)
2. 마크다운 링크 치환 → `make links`
3. 코드→문서 참조 치환 → 단계 2 의 검사
4. 스크립트·Makefile·CI·유닛 경로 치환 → `make check-fast` · `make check`
5. `.gitignore` 경로 갱신
6. 유닛 재설치 → [`operate/tools/install.sh`](../../operate/tools/install.sh) · [`life-trainer/scripts/install-units.sh`](../../life-trainer/scripts/install-units.sh)
7. `README §디렉토리 구조` · `CLAUDE.md §6` · `docs/folder-structure.md` 를 새 구조로

---

## 5. 검증 — 매 단계마다

```bash
make check-fast                          # 링크 + 문서 지표 + shellcheck (즉시)
make check                               # + systemd-analyze verify + 테스트
bash operate/tools/check-docs.sh --self-test     # 검사기 자기시험지
bash operate/tools/daily-check.sh --self-test    # 알림 전이
```

**단계 4 의 마지막 관문은 재부팅이다.** `%h/project/project-jetson/...` 이 유닛에
박혀 있어 **재기동만으로는 부족하다.** [`operate/tools/verify-boot.sh`](../../operate/tools/verify-boot.sh) 가
실제로 올라오는지 본다.

```bash
systemctl --user daemon-reload && systemctl --user list-timers
make status && make host-status
curl -s localhost:8080/health && curl -s localhost:8081/health
```

---

## 6. 이번에 안 하는 것

| | 왜 |
|---|---|
| LICENSE · NOTICE | **공개를 안 한다** |
| 개인정보 일반화 (`/home/user` · Tailscale IP · 폰 이름) | 같음 |
| 타사 스크린샷 정리 | 같음. `.git` 이 작아서 급하지도 않다 |
| `git filter-repo` | 노출된 Serper 키는 **무효 확인됨**. 브랜치 4개 + force-push 대비 얻는 게 없다 |
| 저장소 분리 (공개/비공개) | 계측 쪽이 `Life_Trainer` 를 **하드 의존**한다 — `Makefile` · CI · `check-docs` · `check-links` · `status` · `daily-check` · `verify-boot` · `make-recovery-bundle` · `operate/tools/install.sh` · `desired-state.txt` |
| `docs/archive/` 삭제 | 중복 제거만 하기로 결정. `contracts-v1~v2` 는 현 `contracts.md` 의 유래이고, speech 결론 하나는 `llm/trigger.py` 가 인용 중이다 |

---

## 7. 다음 세션이 처음 읽을 것

```
1. 이 문서 §1     어디까지 왔나
2. docs/folder-structure.md   목표 구조와 "안 바꾸는 것"
3. CLAUDE.md §1·§6       검사를 붙이는 규칙 · 파일을 어디 두는 질문
4. life-trainer/CLAUDE.md  기록 규칙 (깨진 가정을 쓴다)
```

현황 수치는 **문서에 없다.** `make status` · `make host-status` · `lt doctor` 로 뽑는다 —
그게 이 저장소의 규칙이다.
