# Life Trainer

**Jetson Orin NX 위에서 상시 실행되는 개인 활동 로깅·플래너 겸 대화 에이전트.**

하루 활동을 10분 단위로 자동 계측해 SQLite 에 쌓고, 집계는 전부 SQL 로 하고,
LLM 은 이미 계산된 숫자를 문장으로 바꾸는 역할만 한다. 결과는 Slack 카드(텍스트 +
타임라인 PNG)와 웹 플래너로 받는다. **Slack DM 으로 물어보면 자기 기록을 근거로 답한다.**
활동 원본과 전체 이력은 Jetson의 로컬 SQLite에 저장한다. 사용자가 설정한 Slack 리포트·대화와
외부 검색 질의는 해당 서비스로 전송되며, 검색 질의는 전송 직전에 개인정보 패턴을 검사한다.

**상태: 실사용 중** — 타이머와 상시 서비스가 재부팅을 넘겨 돈다 (`make status`).
현황 수치(테스트 수·DB·doctor)는 [HANDOFF §1](HANDOFF.md) 하나에만 적는다.

설계 근거·결정 기록은 여기서 반복하지 않는다 — 필요하면 링크를 따라가라.

### 어디부터 읽나

| 문서 | 내용 |
|---|---|
| **[HANDOFF.md](HANDOFF.md)** | **지금 상태 · 돌고 있는 것 · 다음에 할 일. 이어받는다면 여기부터** |
| **[docs/handbook.md](docs/handbook.md)** | **전체 설명서 — 아키텍처·파이프라인·기능·현황** |
| [CLAUDE.md](CLAUDE.md) | 작업 규칙 — 하드웨어가 강제하는 제약, 프롬프트 규칙 |

### 기록

| 문서 | 내용 |
|---|---|
| [docs/progress/](docs/progress/) | **날짜별 진행 기록.** 큰 기능이 추가되면 여기에 쓴다 |
| [HISTORY/](HISTORY/) | **버그 기록.** 고친 코드가 아니라 **깨진 가정**을 남긴다 (색인은 그 폴더의 README) |
| [docs/issues/](docs/issues/) | **열려 있는 문제.** 한 건에 한 파일. 고치면 HISTORY 로 옮긴다 |
| [docs/known-issues.md](docs/known-issues.md) | ↑ 의 옛 단일 파일. **2026-08-27 이관 완료 — 이정표만 남았다** |

### 설계·계약

| 문서 | 내용 |
|---|---|
| [docs/life-trainer-design.md](docs/life-trainer-design.md) | 설계 3원칙, 아키텍처, 스키마 설계 근거 (원본) |
| [docs/implementation-plan.md](docs/implementation-plan.md) | 실제 기기 실측, 설계서에서 **바꾼 것**, 사람이 할 일 |
| [docs/contracts.md](docs/contracts.md) | **모듈 계약서 통합본.** 시그니처 189건을 코드와 대조해 합쳤다 (2026-08-28) |
| [docs/archive/](docs/archive/) | 역할이 끝난 문서 — 합치기 전의 계약서 3종 + 검증기 |
| [docs/rag-plan.md](docs/rag-plan.md) | Phase 4 RAG — 단계별 상태는 그 문서의 §1 표가 정본 |

> **2026-08-28 에 하나로 합쳤다.** 전에는 시대순 3층이라 현재 계약을 알려면 셋을 읽고
> 직접 무효화 판정을 해야 했다. 실제 충돌은 `timeutil.py` 함수 4개뿐이었다.

### 조사·설계 자료

| 문서 | 내용 |
|---|---|
| [docs/research/activitywatch.md](docs/research/activitywatch.md) | AW API 조사 — **겹침 사고의 출처** |
| [docs/measure/findings/slack.md](docs/research/slack.md) | Slack Bolt·Block Kit 조사 |
| [docs/design/README.md](docs/design/README.md) | 플래너 UI 개편 — 시안 3종, HTML→PNG 실측, 최종 결정 |
| [android/README.md](android/README.md) | 폰 사용 기록 수집 — **동작 중** (2026-08-22). 앱 소스는 별도 비공개 저장소 `donghee-ai/lt-phone` |

### 실행 지시서 (다른 세션에 그대로 넘길 수 있게 쓴 것)

| 문서 | 내용 |
|---|---|
| [docs/runbook-search-and-plans.md](docs/runbook-search-and-plans.md) | **Serper 키 넣기 · 실제 계획 넣기** — 젯슨에서 |
| [android/docs/fork-build.md](android/docs/fork-build.md) | **aw-android 포크 빌드** — Windows PC. 툴체인부터 HMAC 규격까지 |

기기 실측치(대역폭·속도·메모리)는 저장소 상위의 [`measure/findings/`](../measure/findings/) 에 있다.

---

## 아키텍처

```
ActivityWatch(Windows, Tailscale)   RSS/arXiv        수동 입력(/log)   폰(POST /ingest/aw)
        │ aw_sync.py                   │ feeds/arxiv      │                │ 미착수
        ▼                              ▼                  ▼                ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                    SQLite (WAL·FTS5) — data/lifetrainer.db                │
│   aw_event → rollup(classify.py) → slot / slot_breakdown (10분×144)       │
│     └ 기기 차원 + 중재: 마지막 상호작용이 그 시간을 소유한다               │
│   plan/plan_instance(의도) · slot_override(사람 정정) — slot 과 절대 안 섞음│
│   doc(+score/summary/tag) · job(GPU 큐) · report · manual_entry           │
└───────────────────────────────────────────────────────────────────────────┘
        │ stats.py(SQL 집계) → timeline.py·planner.py(PNG) → blocks.py(Block Kit)
        ├──▶ report/daily.py (build_daily/build_weekly) ──▶ Slack (--post 일 때만)
        ├──▶ web/app.py — 플래너 웹 (HMAC 원타임 링크 · 세션 · 외부 마스킹)
        └──▶ slackio/app.py — 슬래시 10개 + **DM 대화**
                                  │ trigger.py(규칙 게이트) → converse.py(툴 루프)
                                  ▼
   llm/worker.py — GPU 큐 하나만 직렬로 소비 (127.0.0.1:8080 의 llama-server 공유)
                   대화 턴은 interactive_turn 락으로 배치보다 우선권을 갖는다
```

- **집계는 SQL, 문장화는 LLM.** Phase 1 리포트는 LLM 을 아예 안 쓴다(`report/stats.py`,
  `report/daily.py`).
- **GPU 는 단일 자원.** `llm/worker.py` 가 `fcntl` 프로세스 락으로 동시 실행을 막고,
  기존 OpenClaw 의 `llama-server(127.0.0.1:8080)`를 그대로 공유한다 — Life Trainer 전용
  모델 서버는 띄우지 않는다 (이유: [implementation-plan.md §1-1](docs/implementation-plan.md)).
- **Slack 발송은 명시적으로만.** `--post` 플래그 없이는 어떤 명령도 Slack 에 아무것도
  보내지 않는다.
- **슬래시가 주 경로, 자연어는 폴백.** `/lt`·`/plan` 은 밀리초에 GPU 를 안 쓴다.
  자연어는 2~9초이고 소형 모델이 "했습니다"로 끝내는 실패 모드가 있다.
- **의도와 측정을 절대 안 섞는다.** `slot`(기계 측정) / `plan_instance`(사람 의도) /
  `slot_override`(사람 정정) — **DB 뿐 아니라 프롬프트에서도.** 한쪽만 실으면 모델이
  그걸로 나머지를 지어낸다.

---

## 빠른 시작 — 합성 데이터로 5분 안에 결과 보기

ActivityWatch 없이도 파이프라인 전체(수집 → 롤업 → 통계 → PNG → 리포트)를 검증할 수 있다.

```bash
cd /home/user/project/project-jetson/life-trainer

.venv/bin/lt doctor                       # 환경 점검부터
.venv/bin/lt init-db                      # 스키마 생성 (doctor 가 없다고 하면)
.venv/bin/lt synth --days 14              # 합성 활동 14일치 생성
.venv/bin/lt rollup --range 2026-08-02 2026-08-15   # 10분 슬롯 롤업
.venv/bin/lt stats --day 2026-08-15       # 콘솔 요약
.venv/bin/lt report daily --day 2026-08-15          # 텍스트 + PNG (Slack 발송 안 함)
.venv/bin/lt report weekly --end 2026-08-15         # 주간 리포트 (Slack 발송 안 함)
```

PNG 는 `data/png/`에 생긴다. 실제로 Slack 에 보내고 싶을 때만 `--post` 를 붙인다:

```bash
.venv/bin/lt report daily --post
```

`.venv/bin/lt` 가 없으면(패키지를 editable 설치하지 않은 경우) 대신 이걸 쓴다:

```bash
.venv/bin/python -m lifetrainer.cli doctor
```

---

## 실제 데이터 연결 — Windows PC 에 ActivityWatch 설치

관리자 PowerShell 에서:

```powershell
scripts\setup-activitywatch-windows.ps1
```

ActivityWatch 를 설치하고, Tailscale CGNAT 대역(`100.64.0.0/10`)으로만 방화벽을 열고,
`address = "0.0.0.0"` 로 바인딩한다(로컬 워처가 `localhost:35600` 으로 붙어야 해서
Tailscale IP 단독 바인딩은 쓰지 않는다 — 자세한 이유는
[implementation-plan.md §1-5](docs/implementation-plan.md)). 브라우저 확장 설치는
스크립트가 출력하는 스토어 링크를 따라가면 된다(선택, 없으면 URL 없이 창 제목만으로 분류).

> **★ 포트는 5600 이 아니라 `35600` 이다 — 5600 으로 되돌리지 마라.**
> `5600` 은 ActivityWatch 의 유명 기본값이라 젯슨·폰·다른 PC 어디서든 같은 번호가 나온다.
> 그 상태에서 VS Code Remote-SSH 가 원격의 5600 을 윈도우 PC 의 `127.0.0.1:5600` 으로
> 자동 포워딩하면, 윈도우는 `127.0.0.1` 바인딩을 aw-server 의 `0.0.0.0` 바인딩보다
> 우선하므로 **PC 의 수집이 조용히 멈춘다** (2026-08-21 에 9시간 날렸다).
> 바꿀 일이 생기면 **양쪽(젯슨 `adb forward` 는 `5610`)과 안 겹치는지 먼저 확인한다.**
> 포트를 옮겨도 워처 death·DB 잠김은 여전히 조용히 실패하므로, PC 의 `AW-Guard`
> 예약 작업이 15분마다 감시한다.

설치 후:

```bash
.venv/bin/lt doctor       # ActivityWatch 항목이 OK 로 바뀌는지 확인
.venv/bin/lt sync         # 실제 이벤트 동기화
.venv/bin/lt rollup --yesterday
```

---

## 명령표

```
lt doctor                      환경 점검 (DB · AW · LLM · Slack · 폰트 · 디스크 · 큐 ·
                               임베딩 · 검색 · 수집 소스 · 에이전트 · CI 도달)
lt dev-db [--refresh]          운영 DB 의 **개발 사본**을 뜬다 → 이후 `lt --dev <명령>`
lt init-db                     스키마 생성
lt sync                        ActivityWatch 동기화
lt synth --days 7              합성 데이터 생성
lt rollup [--day D | --yesterday | --today | --range A B]
lt stats [--day D]             콘솔 요약 (기본: 오늘)
lt timeline --day D [--out P]  타임라인 PNG 생성
lt report daily [--day D] [--post]
lt report weekly [--end D] [--post]
lt log <카테고리> <기간> [메모] [--at HH:MM]   수동 입력 (영향받는 날짜 자동 재롤업)
lt import <파일>               폰에서 뽑은 ActivityWatch export 넣기
lt plan add|list|rm|check      계획 CRUD (사람이 선언한 의도)
lt slot <day> <slot> <카테고리> 타임테이블 칸 수동 보정 (slot_override)
lt planner --day D [--out P]   하루 플래너 PNG (계획 + 실측을 겹쳐 그린다)
lt web                         플래너 웹 서버 (상시. systemd 가 띄운다)
lt collect                     피드 수집 1회 (Phase 2)
lt score                       미채점 문서 스코어링 (Phase 2)
lt bodies [--limit N]          **초록이 짧은 문서만** 전문 수집 (긴 초록은 안 건드린다)
lt backfill-abstracts          초록이 빈 문서 메우기
lt embed                       문서 임베딩 적재 (야간 배치가 부른다)
lt digest [--post]             아침 다이제스트, 키워드 랭킹만 (Phase 2, LLM 없음)
lt nightly [--dry-run|--stop]  야간 배치 적재 (요약·태깅 잡을 큐에. --stop 은 남은 잡 비우기)
lt worker [--once]             GPU 잡 워커 (Phase 3)
lt queue stats                 GPU 잡 큐 상태
lt backup [--out P]            DB 온라인 백업
lt private on|off|purge|undo   프라이빗 구간 · 기록 삭제 · **되돌리기**
lt aw-compact [--apply]        쌓인 중복 이벤트 합치기 (기본은 미리보기)
lt slack serve                 Socket Mode 상시 실행 (mode="bolt" 일 때만)
lt slack test                  테스트 메시지 발송

lt agent mcp                   MCP stdio 서버 (게이트웨이가 띄운다. 손으로 부를 일 없음)
lt agent budget                에이전트 프롬프트 토큰 예산 (예산 넘으면 종료코드 1)
lt agent prompt [--print]      워크스페이스 AGENTS.md 생성·설치
lt agent call <툴> k=v …       툴 하나를 직접 호출 (게이트웨이·모델 없이 몸통만 확인)
```

모든 명령은 `--config`(설정 파일 경로)와 `-v/--verbose`(DEBUG 로그)를 받는다.
그리고 **`--dev`** — 운영이 아니라 개발 사본(`data/dev/`)에 대고 돈다.
`lt --dev doctor` 처럼 앞에 붙여도 되고 `lt doctor --dev` 도 된다.
왜 환경변수가 아니라 플래그인지는 [`lifetrainer/devdb.py`](lifetrainer/devdb.py) 머리말에.
종료 코드: 성공 0, 사용자 오류(잘못된 인자·설정) 2, 런타임 실패 1.

`lt report daily/weekly`, `lt digest` 는 `--post` 를 **직접 붙이지 않는 한 Slack 에
아무것도 보내지 않는다.**

---

## 설정

실제 설정은 `config/lifetrainer.toml` (git 미추적, `.gitignore` 참조). 처음 설치라면:

```bash
cp config/lifetrainer.example.toml config/lifetrainer.toml
```

주요 항목:

| 섹션 | 키 | 의미 |
|---|---|---|
| `[activitywatch]` | `base_url` | Windows PC 의 Tailscale 주소:35600 |
| `[rollup]` | `slot_minutes` | 슬롯 크기(기본 10분 → 하루 144슬롯) |
| `[report]` | `png_dir`, `font_family` | PNG 출력 경로, 한글 폰트 |
| `[slack]` | `mode` | `"notify"`(발송 전용, 기본) \| `"bolt"`(Socket Mode 슬래시 커맨드) |
| `[slack]` | `default_channel` | 리포트를 보낼 채널/사용자 ID |
| `[llm]` | `base_url`, `model` | 기존 llama-server 재사용 (`127.0.0.1:8080`, `qwen3-8b`) |
| `[collect]` | `sources_path` | Phase 2 RSS/arXiv 소스 목록 (`config/sources.yaml`) |
| `[search]` | `serper_api_key` | 웹 검색 키. **Serper 단독** — 네이버는 발급 불가([§4](docs/known-issues.md)) |
| `[nightly]` | `summary_limit` | 새벽 배치가 한 번에 넣는 요약 수 (기본 600 ≈ 3.3시간) |
| `[web]` | `host`, `port` | 플래너 웹 바인딩 (기본 `127.0.0.1:8770`). **외부에 열려면 `0.0.0.0`** |
| `[web]` | `external` | `true` 면 응답에서 창 제목·앱 이름을 제거 (실측 22KB→13KB) |
| `[web]` | `session_secret`, `link_ttl_sec` | HMAC 원타임 링크·세션. 비우면 자동 생성 |
| `[ingest]` | `enabled`, `secret` | 폰 → `POST /ingest/aw` 수신. **기본 꺼짐**, 본문 HMAC 서명 |

`[slack].bot_token` 을 비워두면 `openclaw_config`(기본
`~/.openclaw/openclaw.json`)에서 기존 OpenClaw 봇 토큰을 자동으로 읽어온다 — 전용
Slack 앱을 만들기 전에도 오늘부터 리포트를 받을 수 있는 이유다.

환경변수로도 덮어쓸 수 있다: `LT_<SECTION>_<KEY>` (예: `LT_SLACK_BOT_TOKEN`).

분류 규칙은 `config/rules.yaml`, 관심사/소스는 `config/sources.yaml` — 둘 다 직접
편집해서 튜닝한다.

---

## systemd (상시 운영)

사용자 유닛(`~/.config/systemd/user/`)으로 등록한다 — Docker 를 쓰지 않는다
([implementation-plan.md §1-3](docs/implementation-plan.md) 참고).

```bash
scripts/install-units.sh      # 심링크 + daemon-reload + 타이머/워커 기동
scripts/uninstall-units.sh    # 되돌리기 (DB/설정은 안 건드림)
```

| 유닛 | 주기 | 실행 |
|---|---|---|
| `lifetrainer-sync` | 10분 | `lt sync && lt rollup --today` |
| `lifetrainer-daily` | 매일 23:30 | `lt report daily --post` |
| `lifetrainer-weekly` | 일요일 22:00 | `lt report weekly --post` |
| `lifetrainer-collect` | 1시간 | `lt collect && lt score` |
| `lifetrainer-nightly` | 매일 02:10 | `lt nightly` (요약·태깅 잡 적재, 처리는 워커가) — 02:00 8B 재기동 뒤 |
| `lifetrainer-nightly-stop` | 매일 05:50 | `lt nightly --stop` (남은 배치 잡 비우기 — 낮까지 안 새게) |
| `lifetrainer-digest` | 매일 07:30 | `lt digest --post` |
| `lifetrainer-worker` | 상시 | `lt worker` (`Restart=always`) — GPU 잡 + 예약 알림 + 야간 배치 소비 |
| `lifetrainer-web` | 상시 | `lt web` — 플래너 웹 |
| `lifetrainer-slack` | 상시 | `lt slack serve` — Socket Mode. **`mode="bolt"` 일 때만 의미가 있다** |

> ★ **Slack 소켓은 워크스페이스 앱당 하나뿐이다.** `lifetrainer-slack` 과 OpenClaw 의
> Slack 채널을 **둘 다 켜면 서로 뺏는다.** 지금은 Life Trainer 가 갖고 있다.

배치 유닛은 `MemoryMax=512M`, 워커는 `1G` 로 제한한다 — 이 기기는 가용 메모리가
4.5GB 이고 llama-server 가 6.7GB, 임베딩 서버가 1.8GB 를 쓰는 중이라, 파이썬 쪽이
폭주해 모델 서버를 밀어내면 안 되기 때문이다.
(08-23 ctx 축소 전에는 가용 2.8GB · llama-server 10.4GB 였다.)

**`loginctl enable-linger` 가 안 되어 있으면 재부팅 후 타이머가 하나도 안 뜬다.**
`install-units.sh` 가 상태를 확인해 알려주지만, sudo 가 필요해서 스크립트가 대신
실행하지는 않는다 — 안내가 뜨면 사람이 직접:

```bash
sudo loginctl enable-linger aisw
```

---

## 현재 구현 상태

- [x] **Phase 0 — 뼈대**: venv, SQLite 스키마(WAL·FTS5, 현재 schema v11 — 일반 테이블 31
      + FTS5 1 + 뷰 1), 설정 계층, 시간 유틸
- [x] **Phase 1 — 활동 로깅**: AW 수집기, 합성 데이터, 규칙 분류기, 144슬롯 롤업,
      타임라인 PNG, 일일/주간 리포트, Slack 발송(notify 모드) — **LLM 없음**
- [x] **Phase 2 — 수집기**: 예의 HTTP 계층(도메인별 레이트리밋·조건부 GET·robots.txt),
      RSS/arXiv 수집, SHA-256/SimHash 중복 제거, 키워드 관심사 스코어링 — **LLM 없음**
- [x] **Phase 3 — LLM 골격**: OpenAI 호환 클라이언트(스키마 `pattern` 검증, thinking
      끄기, 계측), GPU 잡 큐 + `fcntl` 락, 핸들러 2종(활동 태깅/문서 요약)
- [x] **플래너** (08-16~17): 계획 계층, 웹 UI, 플래너 PNG, 검증된 팔레트,
      하루 경계 06:00, 사용자 정의 subject, HMAC 원타임 링크 + 세션 + 외부 마스킹
- [x] **Phase 3-C — 대화** (08-17): 3층 프롬프트, 툴 콜링, 규칙 트리거 게이트,
      슬래시 10개, Socket Mode
- [x] **웹 읽기** (08-18): `fetch_url` — SSRF 가드(IP `is_global` 기준), 출처 라벨,
      리다이렉트 홉마다 재검증
- [x] **Phase 3-D — 야간 배치 가동** (08-19): `lt nightly` 적재 + 02:00 타이머 +
      **대화 우선권 락**(배치가 도는 중에도 Slack 질문이 먼저 간다)
- [x] **웹 검색** (08-19): `web_search` — **Serper 단독**(08-21), 한국어는 `gl=kr`. 키 투입 완료
- [x] **게임 카테고리** (08-19): 9번째. 열거가 아니라 **모양**으로 잡는다
      (`-Win64-Shipping.exe` → 런처 → 개별 타이틀)
- [x] **폰 수신 · 기기 중재** (08-19): `POST /ingest/aw`(HMAC·재전송 방지·기본 꺼짐),
      `lt import`, 롤업 기기 차원 — **마지막 상호작용이 그 시간을 소유한다**
- [x] **플래너 UI 리디자인** (08-19): 2/3 타임테이블 + 1/3 사이드바, 연속 활동 병합,
      데스크톱/모바일/다크 ([docs/design/README.md](docs/design/README.md))
- [ ] **Phase 4 — RAG**: [rag-plan.md](docs/rag-plan.md) 의 **1·3단계 완료**.
      다음은 2단계(`search_docs` 결과에 요약 첫 문장 싣기). 임베딩은 FTS5 대비 이득을
      재기 전에는 시작하지 않는다
- [ ] **폰 쪽** ([android/](android/)): 젯슨 수신부는 끝났고 **앱이 아직 없다**

모듈은 전부 구현·테스트 완료 상태다. 실데이터는 2026-08-17 부터 쌓이고 있다
(현황은 `make status`).

---

## 사람이 해야 할 일

코드로 대신할 수 없는 것들 (자세한 내용은
[implementation-plan.md §4](docs/implementation-plan.md)).

**이 기기에서는 1~4 가 이미 끝났다.** 아래는 새로 세울 때의 순서이고, 현재 남은
것은 [HANDOFF §6](HANDOFF.md) 에 우선순위로 정리돼 있다.

| # | 할 일 | 없으면 | 이 기기 |
|---|---|---|---|
| 1 | Windows PC 에 ActivityWatch 설치 (`scripts/setup-activitywatch-windows.ps1`) | 실데이터가 없다. 합성 데이터로만 돈다 | ✅ 08-17 |
| 2 | `config/lifetrainer.toml` 의 `[slack].default_channel` 지정 | 리포트를 어디로 보낼지 모른다 | ✅ |
| 3 | `loginctl enable-linger aisw` (sudo 필요) | 재부팅 후 타이머가 안 뜬다 | ✅ |
| 4 | 전용 Slack 앱 생성 후 `mode="bolt"` 전환 | 슬래시 커맨드·DM 대화 없음. 발송(notify)은 그대로 됨 | ✅ 08-17 |
| 5 | **Serper 키** (`[search]`) | 대화가 주소를 추측한다 | ✅ 08-21 (Slack 에서 답 확인은 남음) |
| 6 | 실제 계획으로 교체 (테스트용 5건이 들어 있다) | 달성률이 의미 없다 | ❌ **남음** |
| 7 | (선택) 헤드리스 전환(`sudo systemctl set-default multi-user.target`) | GUI 가 계속 ~0.5GB 를 먹는다 | ❌ |

---

## 개발

**런타임은 Python 3.14.6** 이다 (2026-09-08 에 3.10 에서 올렸다). 시스템 파이썬이
아니라 `uv` 가 사용자 영역에 깐 것이고, 절차와 **되돌리는 법**은
[docs/runbook-python-upgrade.md](docs/runbook-python-upgrade.md) 에 있다.

```bash
uv venv --python 3.14 --relocatable .venv                       # 환경 만들기
uv pip install --python .venv/bin/python -e '.[dev]' -c constraints.txt
.venv/bin/python -m pytest tests/ -q    # 네트워크로 못 나간다 (conftest 가 소켓을 막는다)
.venv/bin/ruff check lifetrainer tests  # 린트 (make lint · pre-push · CI 가 부른다)
.venv/bin/lt doctor                     # 환경 점검
```

**실험은 사본에서 한다.** 이 시스템의 표본은 사람의 실생활이라 운영 DB 가 곧 유일한
데이터셋이다 — 그래서 분리하는 방식이 **복사**다:

```bash
.venv/bin/lt dev-db                     # 운영 → data/dev/ 로 사본 (WAL 안전)
.venv/bin/lt --dev rollup --today       # 사본에 대고 실행 (stderr 로 알려준다)
```

**에이전트 쪽은 테스트가 못 잡는다.** `tests/` 는 모델을 안 부르므로 "툴을 고르는가"
는 초록불로 안 나온다. 그건 채점기가 본다 — 실기기·실모델로 돈다:

```bash
bash scripts/install-agent.sh                        # 배선 (여러 번 돌려도 안전)
.venv/bin/python scripts/eval_agent.py --trials 2    # 케이스 9개 × 2회, 약 20분
```

**테스트는 네트워크를 타지 않는다.** 전부 페이크/픽스처다 — 그래서 실기기를 붙이기
전까지 [겹침 버그](HISTORY/2026-08-17-aw-overlap-inflation.md) 같은 것을 원리적으로
못 잡는다. 초록불을 동작 확인으로 착각하지 말 것 ([CLAUDE.md](CLAUDE.md) 반복 실패 #4).

파일 소유권은 [docs/contracts.md §1](docs/contracts.md) 참고 — 담당별로 건드릴 수
있는 파일이 고정되어 있다.
