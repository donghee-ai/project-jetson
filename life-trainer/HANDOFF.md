# Life Trainer — 핸드오프

> 작성: 2026-08-17 21:40 KST · 갱신: 2026-09-09 (공개 범위·스키마 설명 정정)
> 이 문서는 **다음 사람(또는 다음 세션)이 이어받기 위한 것**이다.
> 전체 설명서는 [docs/handbook.md](docs/handbook.md), 작업 규칙은 [CLAUDE.md](CLAUDE.md).

---

## 1. 지금 이 물건은 무엇인가

**Jetson Orin NX 위에서 상시 실행되는 개인 활동 로깅·플래너 겸 대화 에이전트.**
노트북과 **폰** 활동을 10분 단위 144슬롯으로 자동 계측하고, 계획과 대조해, Slack으로 리포트를 보낸다.
**Slack DM 으로 물어보면 자기 기록과 모아 둔 문서를 근거로 답한다** (RAG — 하이브리드 검색).
활동 원본과 전체 이력은 Jetson의 로컬 SQLite에 보관한다. 사용자가 설정한 Slack 리포트·대화와
외부 검색 질의는 해당 서비스로 전송된다. 검색 질의는 전송 직전에 개인정보 패턴을 검사한다.

**상태: 실사용 중.** 실기기가 연결되어 자동으로 돌고, Slack 소켓도 이쪽이 갖고 있다.

```
★ 여기에 숫자를 옮겨 적지 않는다. 뽑는 명령이 정본이다.

  make status     서비스 · 실데이터 · schema · 분류 규칙 · 문서 링크   (즉시)
  make test       테스트 개수                                        (약 100초)
  lt doctor       환경 점검

  2026-08-29 01:30 스냅숏 — doctor OK 17 / WARN 0 / FAIL 0
                  L4T 36.5.2 · 커널 5.15.199-tegra · 에이전트가 대화 기본값
                  ★ 숫자는 위 명령으로 뽑는다. 이건 날짜 붙은 참고값이다
```

> **왜 이렇게 두는가.** 이 저장소는 같은 숫자를 서로 다르게 적은 사고를 **네 번** 겪었다
> (테스트 수 두 번 · 분류 규칙 수 한 번 · 실데이터 한 번). 08-28 에 "적을 자리를 하나로
> 줄인" 뒤에도 **두 커밋 만에 또 어긋났다.** 자리를 줄이는 걸로는 부족하다 —
> **옮겨 적는 행위 자체**가 원인이라 명령으로 대체한다. 위 스냅숏은 날짜가 붙은 참고값이다.

> **이 블록의 숫자는 2026-08-24 14:20 에 직접 조회한 값이다.** 고칠 때는 눈대중하지 말고
> `lt doctor` 와 `pytest` 를 실제로 돌려서 바꾼다 — 이 문서가 한동안 세 군데에서
> 서로 다른 테스트 수를 말하고 있었다.

### 입력 경로 — 무엇을 쳤느냐로 갈린다

```
자연어 DM     → **OpenClaw 에이전트** → 모델이 툴 고름   약 30s   ★ 기본값 · §11
/lt /plan …   → 파싱 → SQL                            밀리초    빠른 접근
─────────────────────────────────────────────────────────────
converse      → 규칙 게이트 → 툴 → SQL                 2~9s     ★ 강등 전용
```

**기본은 에이전트다 (2026-08-24~).** 값은 속도가 아니라 **Slack 밖으로 안 나가도
된다는 것**이다 — 계획을 고치거나 기록을 물으려고 웹 플래너(§3)를 열지 않아도 된다.
모델이 툴을 고르므로 미리 명령 이름을 알 필요도 없다.

**슬래시는 남는다 — 빠른 접근용이다.** 밀리초이고 GPU 를 안 쓰고, 소형 모델이
"했습니다"로 끝내는 실패 모드가 **구조적으로 불가능**하다. 무엇을 할지 이미 알 때는
이쪽이 낫다. Slack 에서 다른 이벤트라 에이전트와 무관하게 그대로 돈다.

**`llm/converse.py` 는 경로가 아니라 강등이다.** 게이트웨이가 죽었을 때만 탄다.
규칙이 툴을 고르므로 에이전트보다 정확도가 낮고, **과거의 주 경로가 남은 것**이다
(§7-A). 여기로 내려갔다는 것 자체가 고장 신호다.

> **왜 순위가 바뀌었나 (2026-08-28).** 사용자 판단이다 — 실사용에서 에이전트가
> 정확성 면에서 낫다는 것이 근거이고 **채점기 수치가 아니다.** 오히려
> [`issues/0010`](docs/issues/h-0010-the-model-picks-the-first-item-when-told-a-name.md)
> 이 *"채점 0/3 인데 실사용은 동작한다"* 고 적고 있어, **채점기 쪽을 의심할 근거**가 된다.
> 08-17 판 기록([progress](docs/progress/2026-08-17.md))은 그날의 사실이라 그대로 둔다 —
> 그때는 에이전트가 없었고 자연어가 곧 `converse` 였다.

---

## 2. 지금 돌고 있는 것

```bash
systemctl --user list-timers | grep lifetrainer          # 무엇이 언제 도는지
systemctl --user list-units --type=service | grep life   # 상시 서비스
```

| 유닛 | 주기 | 하는 일 | 상태 |
|---|---|---|---|
| `lifetrainer-sync.timer` | **10분** | ActivityWatch 동기화 + 오늘 롤업 | active |
| `lifetrainer-collect.timer` | 1시간 | RSS/arXiv 수집 + 스코어링 | active |
| **`llama-server-restart.timer`** | **02:00 · 06:10** | **8B 재기동 — 자라는 익명 메모리 회수** (`operate/`, [경위](HISTORY/2026-08-31-the-swap-i-cleared-as-harmless-killed-the-machine.md)) | **active** (08-31 신설) |
| **`lifetrainer-nightly.timer`** | **02:10** | **문서 요약 30건 + 태깅 잡 적재** (600 → 60 → 30 으로 내려왔다) · 02:00 재기동 뒤 10분 | **active** (08-19 신설) |
| **`lifetrainer-nightly-stop.timer`** | **05:50** | 남은 배치 잡 비우기 (낮까지 안 새게) | **active** (08-19 신설) |
| **`lifetrainer-backup.timer`** | **06:20** | **DB 백업 + 무결성 검사 + 보존 정리** ([runbook](docs/runbook-backup-restore.md)) | **active** (08-28 신설) |
| `lifetrainer-daily.timer` | 23:32 | 일일 리포트 + PNG → Slack DM | active |
| `lifetrainer-digest.timer` | 07:34 | 아침 다이제스트 | active |
| `lifetrainer-weekly.timer` | 일 22:00 | 주간 비교 | active |
| `lifetrainer-web.service` | 상시 | 웹 플래너 | active |
| `lifetrainer-worker.service` | 상시 | GPU 잡 큐 + 예약 알림 + **야간 배치 소비** | active |
| `lifetrainer-slack.service` | 상시 | **Socket Mode — 슬래시 + DM 대화** | **active** (mode=bolt) |
| **`llama-embed.service`** | 상시 | **RAG 임베딩 서버** (:8081, Qwen3-Embedding-0.6B, CPU) | **active** (08-23 신설) |

`loginctl` linger 활성 → **재부팅해도 뜬다.**

> **감시 (08-28 신설, `operate/` 소유)** — `jetson-daily-check.timer`(07:00, 앱+기기,
> **상태가 바뀔 때만** 알린다) · `jetson-heartbeat.timer`(15분, 바깥 dead-man switch,
> **아직 URL 미설정**). 설계 근거는 [operate/README](../operate/README.md).

### 함께 도는 것 (Life Trainer 소유 아님)

| | 상태 | 메모리 |
|---|---|---|
| `llama-server` (Qwen3-8B Q4_K_M, ctx **20480**) | active | **6.7 GB** |
| `openclaw-gateway` | active (Slack 채널은 **꺼짐**) | 292 MB |

가용 메모리 약 4.5 GB (`free -h` 의 available). **Life Trainer 는 llama-server 를 공유한다** (전용 서버를 띄우지 않는다).

> **ctx 40960 → 20480 으로 낮췄다 (2026-08-23).** KV(q8_0)는 토큰당 76.5 KiB 라
> 40,960 이면 **2.99GB 를 시동 시 전액 선불**한다 — 대화 길이와 무관하게 항상.
> 한 턴 실측 최악이 2,980 토큰이고 `MAX_HISTORY_MESSAGES=6` 으로 상한이 구조적이라
> 나머지 3GB 가 영구히 놀고 있었다. 회수 후 가용 200MB → 4,566MB (스왑 연쇄까지 풀렸다).
> 드롭인: `operate/systemd/llama-server.service.d/ctx.conf`.
> ★ OpenClaw 쪽 `contextWindow` 도 20480 으로 **같이** 맞췄다 — 한쪽만 바꾸면 조용히 잘린다.

> ★ **Slack 소켓은 워크스페이스 앱당 하나뿐이다.** 지금도 Life Trainer 가 갖고 있고,
> OpenClaw 는 `channels.slack.enabled=false` 로 손을 뗐다. **둘 다 켜면 소켓을 뺏고 뺏긴다** —
> 반드시 한쪽만.
>
> ★ **그런데 대화는 이미 에이전트가 한다** (08-24 배선, §11). 소켓은 Life Trainer 가 쥔 채
> `_dispatch_chat` 이 `openclaw agent` 로 **위임**하는 구조다 — 소켓 소유와 대화 처리가
> 다른 층이라 둘이 안 부딪친다. 즉 *"게이트웨이는 살아만 있다"* 가 아니라
> **대화의 기본 경로가 거기다** (§1).

---

## 3. 접속 지점

| | 주소 | 비고 |
|---|---|---|
| **웹 플래너** | `http://100.64.0.2:8770` | 젯슨 Tailscale IP. tailnet 기기에서만 |
| Slack | DM `U0123456789` (user) | 워크스페이스 `Example_Workspace` |
| ActivityWatch | `http://100.64.0.3:35600` | 노트북 `DESKTOP-EXAMPLE` |
| llama-server | `http://127.0.0.1:8080/v1` | 모델 id `qwen3-8b` (대화·요약. **공유 자산**) |
| llama-embed | `http://127.0.0.1:8081/v1` | RAG 임베딩 (0.6B, CPU). 08-23 신설 |
| openclaw-gateway | `http://127.0.0.1:18081` | 에이전트. 대화의 기본 경로가 여기로 간다 |
| 터널 | `https://lt.example.com` | ★ **`/ingest/` 만 공개**, 나머지는 404. 폰 수집 경로 |

**Slack 봇 토큰은 `~/.openclaw/openclaw.json` 에서 자동으로 읽는다** (파일 읽기일 뿐,
OpenClaw 프로세스와 무관). `config/lifetrainer.toml` 은 gitignore 되어 있다.

---

## 4. 실데이터 — **여기 적지 않는다**

```bash
make status     # 기간 · 이벤트 · 문서 · 요약 · GPU 잡 · schema · 기기
```

> **왜 이 절이 비었나 (2026-08-28).** 여기에는 08-24 에 조회한 숫자 블록이 있었고,
> 나흘 만에 이벤트 수가 1.5배가 되어 **전부 틀린 값이 되어 있었다.** §1 을 명령으로
> 바꾸면서 이 절을 놓친 것이 원인이다 — **적을 자리가 하나라도 남으면 거기서 어긋난다.**

### 읽을 때 같이 볼 것

- **미분류 상위**는 야간 태깅이 카테고리 후보를 달아 두므로, 그걸 보고
  `config/rules.yaml` 승격을 판단한다:

  ```bash
  .venv/bin/python -c "import sqlite3;c=sqlite3.connect('data/lifetrainer.db');\
  print(c.execute('select app,llm_category,llm_confidence from unclassified \
  where llm_category is not null order by seconds_total desc limit 20').fetchall())"
  ```

- **계획은 2026-08-24 에 실제 일정으로 교체됐다** (템플릿 3개에서 펼친 인스턴스).
  달성률이 의미를 갖는다 (§6-③).
- **폰이 08-22 에 합류했다** — `phone-example` 가 5분마다 민다. 슬롯이 기기별로 갈린다.
  폰 앱은 공개 저장소 [`donghee-ai/LT-Phone`](https://github.com/donghee-ai/LT-Phone)에 있다.

### ~~★ 야간 배치 실패 203건~~ ✅ 2026-08-28 판정 완료

**잔재가 맞다.** 실패 잡은 **전부 08-20 생성 · 08-22 종료**(재시도 소진)이고 그 이후
신규 실패가 없다. 08-21 수정은 먹었다. 다시 뽑는 법:

```bash
.venv/bin/python -c "
import sqlite3, datetime as dt
c = sqlite3.connect('file:data/lifetrainer.db?mode=ro', uri=True)
d = lambda x: dt.datetime.fromtimestamp(x).strftime('%Y-%m-%d') if x else None
for st, mn, mx, n in c.execute('select state, min(created_at), max(created_at), count(*) from job group by state'):
    print(f'{st:10} n={n:5}  created {d(mn)} ~ {d(mx)}')"
```

★ **`lt doctor` 도 08-28 에 같이 고쳤다.** 판정식이 **누적 실패율**이라 원인을 고쳐도
`purge_done` 창(14일)이 지나야 노란불이 꺼졌다 — 그 사이 새 실패가 들어와도 구분이 안 된다.
판정 축을 **"지금 실패하고 있나"** 로 바꿨다: 마지막 실패 시각 · 그 뒤 연속 성공 수 ·
backlog · 만료된 lease ([HISTORY](HISTORY/2026-08-28-a-ratio-that-could-not-fall.md)).

---

## 5. 여기까지 온 경로

| 날짜 | 내용 | 테스트 |
|---|---|---|
| [08-16](docs/progress/2026-08-16.md) | Phase 0~2 완주 + Phase 3 골격 + 플래너(계획·웹·PNG) | 442 |
| [08-17 낮](docs/progress/2026-08-17.md) | 레퍼런스 3종 흡수, 하루 경계 06:00, 실기기 연결 | 590 |
| [08-17 밤](docs/progress/2026-08-17.md) | **OpenClaw 통합** — 자연어 대화·툴 콜링·트리거 게이트·자체 cron | 687 |
| [08-18](docs/progress/2026-08-18.md) | 웹 읽기(SSRF 가드·출처 라벨) + 환각 3종 + 기기 전환 흡수 | 767 |
| [08-19](docs/progress/2026-08-19.md) | **야간 배치 가동** · **웹 검색** · **게임 카테고리** · 밤: **폰 수신·기기 중재** + **플래너 UI 리디자인** | 868 |
| [08-21](docs/progress/2026-08-21.md) | **폰 게이트 확정**(포크 필요) · 문서 구조 재배치 · **야간 배치 503 재시도 수정** | 883 |
| [08-22](docs/progress/2026-08-22.md) | **폰 합류** — 마이그레이션 003(기기별 슬롯) · 수신 즉시 롤업 · 웹 "어디서 썼나" | 905 |
| [09-01](docs/progress/2026-09-01.md) | **프라이빗 모드**(저장 관문·소급 삭제·웹 토글) · 팔레트 라이트/다크 고정 · 차트·원그래프 · **마이그레이션 러너 복구** · **안 불리는 함수 13개 색출·처분** · **마이그레이션 007(잘린 판 합치기)** · issues 0004·0020 해결 | 1235 |

설계 원본은 [docs/life-trainer-design.md](docs/life-trainer-design.md),
바꾼 것과 이유는 [docs/implementation-plan.md](docs/implementation-plan.md).

**꼭 알아야 할 설계 결정 4가지:**

1. **논리적 하루 = KST 06:00 → 다음날 06:00.** 자정이 아니다. `slot 0 = 06:00~06:10`
2. **집계 원천은 `slot_breakdown`.** `slot` 의 winner-takes-all 은 시각화 전용
3. **`slot`(기계 측정) / `plan_instance`(사람 의도) / `slot_override`(사람 정정)를 절대 섞지 않는다**
   — **DB 뿐 아니라 프롬프트에서도.** 한쪽만 실으면 모델이 그걸로 나머지를 지어낸다
4. **대화의 기본은 에이전트, 슬래시는 빠른 접근, `converse` 는 강등.**
   셋을 같은 층으로 보면 안 된다 — 앞의 둘은 경로이고 마지막은 고장 신호다 (§1 · §7)

---

## 6. 사람이 해야 할 일 (우선순위 순)

> **①검색 키 · ②실제 계획**은 [docs/runbook-search-and-plans.md](docs/runbook-search-and-plans.md) 에
> 단계·확인법·끝나고 고칠 문서까지 정리해 뒀다. **폰 포크**는
> [android/docs/fork-build.md](android/docs/fork-build.md) (Windows PC).
> 둘 다 **다른 세션에 그대로 넘길 수 있게** 자족적으로 썼다.

### ~~① Slack 앱 매니페스트 등록~~ ✅ 08-17 완료

사용자가 `config/slack-app-manifest-merged.json` 으로 교체했다. 명령 11개
(`/openclaw` + 우리 10개). 스코프·이벤트는 기존과 **차이 0** 이라 재설치 불필요했다.

### ~~② bolt 모드 전환~~ ✅ 08-17 완료

`⚡️ Bolt app is running!` 확인. DM 대화 핸들러(`message.im`·`app_mention`)도 함께 붙였다.

### ~~★ 지금 해야 할 것 ① — Serper 키 넣기~~ ✅ **08-21 넣었다** (남은 것은 확인)

**Serper 키를 넣었고 `lt doctor` 가 OK 10 / WARN 0 이다.** 검색 계층은 정답을
물어온다(실측). 다만 그날 실제로 물어보니 **트리거가 안 걸려 검색이 죽는 표현**이
드러나 패턴을 넓혔다 — 경위는 [progress/2026-08-21](docs/progress/2026-08-21.md).
**Slack 에서 한 번 더 물어 답이 맞는지 보는 것**이 남았다
([issues/0006](docs/issues/h-0006-the-model-cites-one-source-and-stays-there.md) — 단일 출처 앵커링).

> ★ **네이버는 안 쓴다 — 경로를 08-25 에 코드에서 지웠다.** 검색 API 가 NAVER API HUB
> 로 이관되고 개발자센터 신규 신청이 2026-07-31 에 닫혔다. `naver_client_id` 를 채우면
> **한국어 질의만 조용히 죽는 함정**이라, 고치는 대신 **삭제**했다
> ([HISTORY 08-25](HISTORY/2026-08-25-it-worked-because-the-key-was-empty.md)).
> 한국어 질의는 Serper 로 가고 `gl=kr&hl=ko` 를 붙여 국내 결과를 받는다.

```toml
# config/lifetrainer.toml
[search]
serper_api_key = "..."
```

**확인 방법**: Slack DM 에서 `롤체는 무슨게임이야?` · `게임 tft는?` 을 물어본다.
Teamfight Tactics/롤토체스가 출처 URL 과 함께 나와야 한다. 로그에
`트리거 ['web']` 이 찍히는지 같이 본다 — 안 찍히면 또 패턴 누락이다.

### ~~★ 지금 해야 할 것 ③ — 실제로 쳐보기~~ ✅ 확인됨

08-24 실사용 첫날에 버그 6건이 나왔고 전부 고쳤다 (§11). 그 뒤로 계속 쓰고 있다.

### ~~③ 실제 계획 넣기~~ ✅ 08-24 완료

템플릿 3개에서 펼친 인스턴스가 들어 있다. 달성률이 의미를 갖는다 (§4).

### ★ 남은 것 ① — 슬래시 힌트 반영 (08-19 부터)

`config/slack-app-manifest-merged.json` 의 예시를 고쳤지만 **Slack 앱에는 아직
반영이 안 됐다.** 입력창에 옛 예시가 뜬다. 파일은 사본일 뿐이라 적용이 따로 필요하다.

```bash
# api.slack.com/apps 하단 "Your App Configuration Tokens" 에서 발급 (12시간 유효)
SLACK_CONFIG_TOKEN=xoxe-... bash scripts/apply-slack-manifest.sh
```

힌트만 바뀌므로 **재설치는 필요 없다.**

> ★ **우선순위가 내려갔다 (2026-08-29).** 대화의 기본값이 에이전트가 되면서
> 슬래시 입력창 힌트를 볼 일이 줄었다 (§1).

### ★ 남은 것 ② — 사람이 정할 몫 (2026-08-29)

기기 운영 쪽은 P0·P1 이 닫혔고, 남은 것은 **판단**이다.
전체 목록과 근거는 [`maintenance-plan.md`](../docs/archive/maintenance-plan.md) 에 있다.

| | |
|---|---|
| 디스크 암호화 · Secure Boot | 적용하든 안 하든 **근거를 기록**해야 재검토가 안 반복된다 |
| 방화벽 · SSH tailnet 제한 | Tailscale 이 죽었을 때 잠기는 위험과 같이 본다 |
| avahi(5353) | `ubuntu.local` 이 필요한가 |

★ **백업 외부 자동 복제는 안 하기로 했다** (2026-08-29). 대신
`make recovery-bundle` 로 **수동 묶음**을 떠서 기기 밖으로 옮긴다 —
저장소 밖 자산(openclaw 배선 · 터널 자격증명 · WiFi 드라이버)까지 들어간다.
BSP 를 건드리기 전처럼 **되돌릴 수 없는 작업 직전에 한 번씩** 뜬다.

### ~~④ 분류 규칙 보강 — 게임~~ ✅ 08-19 완료

게임 카테고리(9번째)를 신설했다. 실제 미분류 목록에서 게임 실행 파일이 반복됐지만,
개별 타이틀과 사용량은 공개본에서 제거했다. 열거 대신 **모양**으로 잡는다 —
`-Win64-Shipping.exe`(언리얼 규약) → 런처 규칙 → 개별 타이틀 규칙.

**다음 미분류 1위는 `bambu-studio.exe`(3D 프린팅 슬라이서)다.** 이제 야간 태깅이
후보로 집으므로, LLM 이 제안한 카테고리를 보고 규칙 승격을 판단하면 된다:

```bash
.venv/bin/python -c "import sqlite3;c=sqlite3.connect('data/lifetrainer.db');\
print(c.execute('select app,llm_category,llm_confidence from unclassified \
where llm_category is not null order by seconds_total desc limit 20').fetchall())"
```

---

## 7. 다음에 만들 것

### ~~A. 대화 핸들러 + 툴 콜링~~ ✅ 08-17 완료 — 구조를 알아둘 것

**프롬프트를 3층으로 나눈다. 변하는 주기가 짧은 것일수록 뒤에.** llama.cpp 가 공통
접두사를 앞에서부터 재사용하기 때문이다.

| 층 | 내용 | 어디에 | 토큰 |
|---|---|---|---|
| ① | 역할 + 오늘 **계획**(의도) + 관심사 | 시스템 메시지 | 165 |
| ② | 툴 스키마 — 트리거가 고른 것만 | `tools=` | 271~595 |
| ③ | 현재 시각 + 오늘 **실측 활동** + 질문 | user 메시지 | ~35 |

**①과 ③에 계획과 실측을 나눠 싣는 것이 핵심이다.** 계획만 싣고 "오늘 뭐 했냐"를
물으면 모델이 계획을 읽고 "다 했다"고 지어낸다 —
[실제로 겪었다](HISTORY/2026-08-17-plan-only-injection-hallucination.md).

**②가 ①의 캐시를 안 깨는 이유**: Qwen3 채팅 템플릿이 시스템 내용을 먼저 렌더링하고
`# Tools` 를 그 뒤에 붙인다. 우리가 순서를 지켜서가 아니라 템플릿이 그렇게 생겼다.

| 파일 | 역할 |
|---|---|
| `llm/context.py` | ①③층 조립 |
| `llm/trigger.py` | 규칙 게이트 (12/12, 0.154ms) |
| `llm/tools.py` | 툴 **10개** + 레지스트리 + 웹 출처 라벨 |
| `llm/webfetch.py` | 웹 읽기 — SSRF 가드·HTML→텍스트·메뉴 접기 |
| `llm/converse.py` | 툴 루프 (상한 3바퀴) |
| `slackio/app.py` | `_dispatch_chat` 등 |

**시스템 프롬프트에 문장을 더하고 싶으면 그게 몇 초짜리인지 먼저 생각할 것.**
프롬프트 처리 295 tok/s 다. OpenClaw 의 12,541 토큰이 곧 58초였다.

### A″. 웹 읽기 (08-18 추가) — 알아둘 것

- **허용목록이 없다.** 안전 경계는 도메인이 아니라 IP: `is_global` 이 참인 주소만.
  ★ `is_private` 로 막으면 **Tailscale(100.64/10)이 통과한다** — 이 기기 웹 플래너가
  거기 있다. 리다이렉트는 홉마다 재검증한다.
- **웹 트리거가 걸린 턴은 `tool_choice="required"`.** `auto` 로 두면 8B 가 기억으로
  지어낸다(조회수·날짜까지 만들어냈다).
- **툴 결과에 출처 라벨을 박는다** (`_SOURCE_LABELS`). 프롬프트로는 안 잡혔고
  온도를 낮추면 오히려 악화됐다.
- **못 읽는 것**: JS 렌더링(유튜브)·봇 차단(Stack Overflow)·robots 금지 사이트.
- **검색 엔진은 없다.** URL 을 알아야 읽는다. 봇에 정직하게 열린 무료 검색을 못 찾았다
  (DuckDuckGo 는 브라우저 UA 위장 필요, Mojeek 403).
  ★ **08-19: `web_search` 툴을 붙였다** — 08-21 부터 **Serper 단독**이고 키도 들어갔다.
  네이버 경로는 08-25 에 삭제했다 (§6). 구글 API 는 신규 가입이 닫혀 있다
  (기존 고객도 2027-01-01 종료).

### A′. 아직 안 한 것

- ~~**OpenClaw 위임 어댑터**~~ ✅ 08-24 에 붙었다 (§11). 다만 **CLI 기동 비용이 남아 있다** —
  게이트웨이 WS 에 파이썬으로 직접 붙으면 없앨 수 있다 (WS 101 upgrade 확인함).
  → [issues/0007](docs/issues/w-0007-the-agent-path-takes-half-a-minute.md)
- **문서 목록 답변이 11~19초** — URL 토큰이 원인. 링크를 모델이 아니라 Slack 블록으로
  붙이면 짧아진다
- **환각 자동 방어 없음** — 3층 배치는 테스트로 고정했지만 "지어내는가"는 실호출로만 확인된다

### B. 코딩 기능은 넣지 않기로 (결론)

실측: 8.6 tok/s → 함수 하나 6초, 파일 하나 5분. 깊은 컨텍스트에서 3.96 tok/s 까지 떨어진다.
게다가 `operate/notes/agent-gateway.md §4-4·§4-6` 에 이 기기에서 겪은 기록이 있다 —
경로를 헷갈리고(`workspace/workspace/`), 툴을 안 부르고 "하겠다"고 답한다.

→ **코딩은 Claude Code 에게, 활동 질의는 Life Trainer 에게.**
로컬 8B 만이 할 수 있는 건 "기기 밖으로 안 나가는 내 데이터에 답하는 것"이다.

일반 셸 대신 **화이트리스트된 도메인 동작**만 툴로 노출하는 절충안이 논의됐다.

### C. 그 외 대기 중

- **Cloudflare Access** — 지금 터널의 문지기는 **앱의 서명 링크 세션 하나뿐**이다.
  `/ingest/` 만 공개하고 있어 당장은 좁지만, 플래너를 열기로 하면 그 앞에 한 겹이 필요하다
  (2026-08-23 부터 대기)
- **폰에서 웹 플래너** — HMAC 원타임 링크·세션·외부 마스킹은 **구현 완료**.
  ★ 터널은 이미 서 있다(`lt.example.com`) — 다만 **`/ingest/` 만 공개**하고 나머지는 404 다.
  플래너까지 열지는 **사람이 결정할 몫**이다 (개인 활동 기록을 공개 인터넷에 붙이는 일). `cfg.web.external=True` 면
  창 제목·앱 이름이 응답에서 제거된다 (실측 22KB→13KB, 누출 0)
- ~~**폰 사용 기록 추적**~~ ✅ **08-22 배포·가동 중.** aw-android 포크(F0~F3)가
  붙었고 `phone-example` 가 5분마다 밀어 넣는다. 젯슨 수신부(`POST /ingest/aw` · `lt import` ·
  롤업 기기 차원과 중재)는 08-19 에 끝났다.
  경위·게이트 판정·빌드 절차는 [android/README.md](android/README.md).
  앱은 공개 저장소 [`donghee-ai/LT-Phone`](https://github.com/donghee-ai/LT-Phone)에 있다 —
  **이 저장소에 앱 소스는 없다** (`android/` 는 설계·결정 기록이다).
  남은 것은 웹 페이지 제목(F4·F5) 뿐 → [android/docs/phone-titles.md](android/docs/phone-titles.md)
- ~~**Phase 4 RAG**~~ ✅ — [rag-plan.md](docs/rag-plan.md) 의 **1~4단계 완료**
  (야간 배치 08-19 · 대화 우선권 08-19 · 요약 싣기 08-23 · 임베딩+하이브리드 08-23).
  본문 활용과 5단계(청킹)는 2026-09-09 에 범위에서 제외하고 Phase 4를 닫았다.
  선결 과제였던 **ctx 회수는 08-23 에 완료** (40960 → 20480, 가용 4.5GB,
  OpenClaw `contextWindow` 동시 조정).
  진입 조건이던 20질의 비교는 **08-23 저녁에 갚았다** (`scripts/eval_search.py`).
  하이브리드(hit@5 76%)가 키워드 단독(96%)보다 나빴고, 원인은 Qwen3-Embedding 의
  질의 지시문 접두 미사용이었다. 붙인 뒤 **98%**.
  같은 날 저녁에 함께 들어간 것: `search_docs` 기간 필터(`period`) · 초록 백필
  785 → 185건 · `estimate_tokens` 계수 실측 정정 · Slack 스트리밍

---

## 8. 이 프로젝트에서 반복된 실패 (읽고 시작할 것)

부류 목록은 [CLAUDE.md](CLAUDE.md) 에 한 벌만 둔다. 건별 사례는
[HISTORY/](HISTORY/) 색인이 정본이다 — **거의 전부 테스트가 초록불인 상태에서 나왔다.**

특히 [실기기 연결 5분 만에 나온 겹침 버그](HISTORY/2026-08-17-aw-overlap-inflation.md)는
**합성 데이터로는 원리적으로 못 잡는** 종류였다. 자기가 만든 가정 위에서 자기를 검증한 셈이었다.

---

## 9. 되돌리기

| 무엇 | 방법 |
|---|---|
| 자동화 전체 끄기 | `bash scripts/uninstall-units.sh` — 심링크가 걸린 유닛을 **전부** 내린다 (08-28 수정) |
| Slack 발송 끄기 | `config/lifetrainer.toml` 의 `default_channel = ""` |
| **OpenClaw 로 소켓 되돌리기** | `systemctl --user disable --now lifetrainer-slack` → `mode="notify"` → `openclaw config set channels.slack.enabled true` → `restart openclaw-gateway` |
| 대화만 끄고 슬래시는 유지 | `slackio/app.py` 의 `_register_conversation(app, cfg)` 호출 한 줄 제거 |
| **DB 복원** | 매일 06:20 백업이 `data/backup/` 에 있다. 절차는 [runbook §5](docs/runbook-backup-restore.md) — **복원 전 파일을 지우지 말고 옮긴다** |
| **기기 통째로 다시 세우기** | `make recovery-bundle` 로 뜬 묶음 + [runbook §5-2](docs/runbook-backup-restore.md). 저장소 밖 자산(openclaw 배선·터널 자격증명·WiFi 드라이버)까지 들어 있다 |
| 에이전트 끄고 규칙 게이트로 | `config/lifetrainer.toml` 의 `[agent] slack = false` → `restart lifetrainer-slack` |
| 웹 외부 차단 | `[web] host = "127.0.0.1"` → `restart lifetrainer-web` |

---

## 10. 빠른 확인

```bash
cd /home/user/project/project-jetson/life-trainer
.venv/bin/lt doctor                        # 여기부터 본다
.venv/bin/python -m pytest tests/ -q       # 전부
systemctl --user list-timers | grep life   # 타이머
journalctl --user -u lifetrainer-sync -n 20   # 마지막 동기화 로그
```

### 문서에 적는 숫자는 여기서 뽑는다

**눈대중 금지.** 이 저장소가 규칙 수를 47·56·66 세 가지로, 테스트 수를 네 가지로
말하고 있던 원인이 "각자 세기" 였다.

```bash
# 분류 규칙 · 피드 (config/ 의 실제 항목 수)
.venv/bin/python -c "import yaml;d=yaml.safe_load(open('config/rules.yaml'));\
print('규칙',len(d['rules']),'· 브라우저앱',len(d['browser_apps']),'· 카테고리',len(d['categories']))"
.venv/bin/python -c "import yaml,collections;s=yaml.safe_load(open('config/sources.yaml'))['sources'];\
print('피드',len(s),collections.Counter(x['kind'] for x in s))"

# 모듈 · 줄 수 · 테스트 수
find lifetrainer -name '*.py' | wc -l && find lifetrainer -name '*.py' -exec cat {} + | wc -l
.venv/bin/python -m pytest tests/ -q --collect-only | tail -1
```

**막히면 `lt doctor` 가 무엇을 하면 되는지까지 알려준다.**

---

## 11. OpenClaw 에이전트 (2026-08-24 신설)

**Life Trainer 가 OpenClaw 의 에이전트 하나(`lifetrainer`)로도 돈다.** 같은
`llama-server:8080` 을 쓰고, 우리 능력을 **MCP 서버**로 빌려준다.

```bash
bash scripts/install-agent.sh          # 배선 (여러 번 돌려도 안전)
.venv/bin/lt doctor                     # 에이전트 항목은 뒤쪽에 모여 있다
.venv/bin/lt agent budget               # 프롬프트 토큰 예산
.venv/bin/python scripts/eval_agent.py  # 채점 (케이스 9개, 약 10분)

openclaw agent --agent lifetrainer --message "오늘 계획이 뭐야?"
```

| | |
|---|---|
| 툴 13개 | `slash`(명령 10개) · 파일 3 · 플래너·활동·RAG·예약·웹 9 |
| 움직일 수 있는 폴더 | 읽기 `docs/ HISTORY/ config/ data/agent/` · 쓰기 `data/agent/` |
| 시스템 프롬프트 | 약 5,247 토큰 (OpenClaw 기본은 12,541) · 첫 호출 17.8초 |
| MCP 서버 상주 | 유휴 13.9MB · 천장 약 100MB · **누수 없음**(400회) |
| 실측 지연 | 26~106초 (중앙값 약 30초) |

### 알아야 할 것 5가지

1. **`~/.openclaw/openclaw.json` 에만 있는 배선이다.** 저장소 밖이라 기기를 다시
   세우면 사라지고, 증상은 "에이전트가 툴을 안 부른다" 뿐이다. `lt doctor` 가 본다
2. **워크스페이스의 범용 인격 파일은 비워 둔 것이다.** 지우면 게이트웨이가 다시
   만든다 — `SOUL.md` 등이 한 줄짜리인 것은 고장이 아니다 (`lt agent prompt` 가 한다)
3. **`experimental.localModelLean` 을 켜지 마라.** 툴 13개가 메타툴 3개로 바뀌는데
   8B 가 그 간접층을 못 넘어 호출이 실패한다 (`operate/notes/agent-gateway.md §7-3`)
4. **예산이 꽉 찼다.** 툴 2,950/3,000 · 프롬프트 973/1,000. 툴을 더 붙이려면
   무엇을 뺄지 먼저 정한다. 테스트가 이 선을 지킨다
5. **Slack 자연어 DM 이 여기로 온다** (`[agent] slack = true`, 08-24).
   소켓은 여전히 Life Trainer 가 쥐고 있고 슬래시도 그대로다 — 바뀐 것은
   `_dispatch_chat` 하나다. 되돌리기는 `slack = false` + `restart lifetrainer-slack`.
   ★ 기본값은 `false` 다. 설정이 없는 기기가 조용히 30초짜리가 되면 안 된다

### 실사용 첫날 (2026-08-24) — 버그 6건이 나왔고 다 고쳤다

**전부 사용자가 쓰면서 찾았다.** 채점기도 단위 테스트도 못 잡은 것들이다.

| 무엇 | 어떻게 드러났나 |
|---|---|
| 위임이 조용히 강등 (PATH) | "답변 빠른데 엄청" |
| Context overflow (한 시간 뒤) | 직접 겪음 |
| `/done 2 3` 이 절반만 처리 | 둘을 시켰는데 하나만 |
| 웹 체크박스가 계획을 못 찾음 | 손으로 되돌리려다 막힘 |
| 패치가 옆 함수에 (수정하면 지워짐) | 고쳤다던 삭제가 여전히 안 됨 |
| `[90]` 을 명령 번호로 씀 | "이 번호 왜 붙어?" |

★ **셋은 내가 만든 것**이고 전부 "조용히 다르게 동작하는 것"이었다.
기록은 [HISTORY](HISTORY/) 에. **실사용 하루가 채점기보다 많이 잡았다.**

### 남은 것

- **채점기와 실사용이 반대로 나온다** — 채점 0/3 인데 실사용은 정확하다.
  2026-08-29 에 **에이전트가 대화의 기본값이 되면서 질문이 바뀌었다** —
  *"모델이 왜 틀리나"* 가 아니라 **"채점기와 실사용 중 무엇이 현실인가"** 다.
  채점기가 실사용과 다른 조건에서 돌기 때문이고(`0008` 실데이터에 쓴다 ·
  `0009` 계획을 손으로 심어야 한다), **그 둘을 먼저 닫아야 갈린다**
  ([issues/0010](docs/issues/h-0010-the-model-picks-the-first-item-when-told-a-name.md))
- **한 턴이 26~106초다.** 그중 약 8초가 `openclaw` CLI(node) 기동이고 프로세스가 350MB 다.
  게이트웨이 WS 직결이 가장 값싼 개선이다
  ([issues/0007](docs/issues/w-0007-the-agent-path-takes-half-a-minute.md)).
  ★ **에이전트가 이제 대화의 기본 경로라 이 지연이 주 UX 문제다** (§1)
- 채점기가 `/done` 으로 바꾼 상태는 못 되돌린다
  ([issues/0008](docs/issues/h-0008-the-scorer-writes-to-the-real-database.md))

> **2026-08-29 갱신** — OpenClaw 를 **시스템 Node 로 옮겼다**. 게이트웨이·위임 CLI 둘 다
> `/usr/local/` 이고 nvm 사본은 지웠다. 그 전에는 **절반만 옮겨져 있었다**
> (게이트웨이는 시스템 Node 로 *실행*되는데 *스크립트*는 nvm 안) —
> `operate/notes/agent-gateway.md §10-B`.
