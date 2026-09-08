# 실행 지시서 — 검색 키 넣기 · 실제 계획 넣기

> **이 문서 하나만 읽고 시작할 수 있게 썼다.** 다른 세션·다른 에이전트에게 그대로 넘겨도 된다.
> 작성 2026-08-21. 두 작업은 서로 독립이라 순서를 바꿔도 되고, 하나만 해도 된다.
>
> 저장소: `/home/user/project/project-jetson/life-trainer` (젯슨 본체에서 실행)
> 파이썬은 **반드시 `.venv/bin/`** 을 쓴다. 시스템 파이썬에는 의존성이 없다.

**둘 다 코드를 고치는 일이 아니다.** 설정값과 데이터를 넣고, 실제로 되는지 확인하고,
문서에서 "안 됨" 표시를 지우는 일이다.

```bash
cd /home/user/project/project-jetson/life-trainer
.venv/bin/lt doctor        # 시작 전 기준선 — OK / WARN / FAIL 을 여기서 본다
```

---

## 작업 A — 검색 API 키

### 왜 하는가

`web_search` 툴은 **코드·트리거·doctor 배선이 전부 끝났고 키만 없다.** 키가 없는 동안
대화가 URL 을 추측하고, 그게 실제 사고를 냈다:

```
사용자: 인터넷 검색을 통해 롤체가 어떤 게임인지 알아봐
답변  : 롤체(VALORANT Champions Tour)는 발로란트라는 …
        출처: VALORANT 공식 홈페이지 (https://www.valorant.com/)
```

롤체 = 롤토체스 = **Teamfight Tactics** 다. 발로란트가 아니다.
자세한 원인 분석은 [known-issues.md §1](known-issues.md).

### 키 발급 — Serper 하나만 받는다 (2026-08-21 변경)

serper.dev 가입 → API 키. **2,500건(6개월) 무료, 카드 등록 없음.** 이거 하나면 끝이다.

> ★ **네이버는 빠졌다.** 검색 API 가 NAVER API HUB(네이버 클라우드 플랫폼)로 이관됐고
> 개발자센터 신규 신청은 2026-07-31 에 닫혔다. 우리는 기존 키가 없어서 지금 신청하면
> HUB 키가 나오는데, **코드에 박힌 도메인·헤더가 옛것이라 그 키는 401 이 난다.**
> 그래서 이건 설정 작업이 아니라 코드 작업이고, [known-issues.md §4](known-issues.md)
> 로 넘겼다. 이 지시서에서 네이버는 하지 않는다.
>
> 구글 Custom Search 도 신규 가입이 닫혀 있다(기존 고객도 2027-01-01 종료).
> 시도하지 말 것.

**한국어 질의는 어떻게 되나.** `_pick_provider` 가 한국어면 네이버를 먼저 찾고,
없으면 있는 쪽으로 넘어간다. 즉 Serper 로 간다. 결과 라벨에 "구글 검색 (Serper 경유)"
라고 드러나므로 조용한 대체가 아니다. 다만 실패 사례("롤체")가 국내 게임 용어라
**한국어 결과 품질은 이 지시서의 확인 단계에서 반드시 눈으로 봐야 한다.**

### 넣기

```bash
cd /home/user/project/project-jetson/life-trainer
$EDITOR config/lifetrainer.toml
```

```toml
[search]
serper_api_key = "..."
```

> ★ **`config/lifetrainer.toml` 은 gitignore 되어 있다.** 키를 저장소의 다른 파일이나
> 커밋 메시지에 넣지 마라. `config/lifetrainer.example.toml` 에도 넣지 마라 —
> 그건 추적되는 파일이다.

환경변수로도 덮어쓸 수 있다(`LT_SEARCH_SERPER_API_KEY`). 다만 systemd 유닛이
읽어야 하므로 **설정 파일 쪽이 맞다.**

### 반영

```bash
systemctl --user restart lifetrainer-slack
```

### 확인 — 두 단계 다 해야 한다

**1) doctor 가 OK 로 바뀌는가**

```bash
.venv/bin/lt doctor | grep 웹\ 검색
```

```
[  OK] 웹 검색           Serper (구글 경유) — 한국어 질의는 gl=kr 로 간다   ← 목표
[WARN] 웹 검색           키 없음 — …                                        ← 아직 안 된 것
```

Serper 하나가 **정상 구성**이다 (2026-08-21 변경). 네이버를 발급받을 수 없게 된
이상 그걸 권하는 WARN 은 못 하는 일을 시키는 것이라 OK 로 바꿨다. 여기까지 오면
doctor 가 **OK 10 / WARN 0 / FAIL 0** 이 된다.

**2) ★ 사고가 났던 그 질문을 다시 물어본다**

Slack DM 에서:

```
인터넷 검색을 통해 롤체가 어떤 게임인지 알아봐
```

**Teamfight Tactics / 롤토체스** 가 나오고 **출처 URL 이 붙으면** 성공이다.
발로란트가 나오면 키가 안 먹은 것이다 — `journalctl --user -u lifetrainer-slack -f` 를 본다.

> **doctor 만 보고 끝내지 마라.** 이 프로젝트의 반복 실패 4번이 "테스트 통과 ≠ 동작"이다.
> 설정이 읽혔다는 것과 답이 맞다는 것은 다른 문제다.

### 끝나면 문서를 고친다

| 파일 | 무엇 |
|---|---|
| [issues/0006](issues/h-0006-the-model-cites-one-source-and-stays-there.md) | 확인됐으면 **파일을 지우고**, 가정이 깨진 것이면 `HISTORY/` 로 옮긴다 |
| [../HANDOFF.md](../HANDOFF.md) §6 | "★ 지금 해야 할 것 ① — 검색 키 넣기" 를 완료로 |
| [../README.md](../README.md) "사람이 해야 할 일" | 5번 행 ❌ → ✅ |

HISTORY 로 옮길 때는 **고친 코드가 아니라 깨진 가정**을 쓴다. 양식은
[../HISTORY/README.md](../HISTORY/README.md).

---

## 작업 B — 실제 계획 넣기

### 왜 하는가

지금 `plan` 테이블의 5건은 **테스트로 넣은 것이라 실제 일정이 아니다.**
그래서 달성률이 아무 의미가 없고, 일일·주간 리포트의 "계획 대비 실제" 절이 죽어 있다.

이 앱의 존재 이유가 **의도(`plan`)와 실측(`slot`)을 겹쳐 보는 것**이라, 한쪽이
가짜면 절반이 놀고 있는 셈이다.

### 지금 뭐가 들어 있는지 본다

```bash
cd /home/user/project/project-jetson/life-trainer
.venv/bin/lt plan list
```

### 지우고 넣는다

```bash
.venv/bin/lt plan rm <id>          # 하나씩

.venv/bin/lt plan add "딥워크" 09:00-12:00 --category coding --days 평일
.venv/bin/lt plan add "운동"   18:00-19:00 --category ops    --days 월수금
.venv/bin/lt plan add "병원"   14:00-15:00 --day 2026-08-25   # 일회성
```

- `--days` 는 한국어를 받는다: `평일` `주말` `매일` `월수금`, 그리고 `mon,wed,fri` · `1-5`
- `--category` 는 **9개 중 하나**여야 한다:
  `coding` `research` `writing` `communication` `learning` `ops` `browsing`
  `entertainment` `gaming`
- **웹 플래너에서 해도 된다** — `http://100.64.0.2:8770` (tailnet 기기에서만).
  드래그로 범위를 잡고 카테고리를 고르는 쪽이 시간대 감각이 낫다.

> `plan`(반복 템플릿)과 `plan_instance`(그날의 실체)는 다른 테이블이다.
> `lt plan add` 는 템플릿을 만들고, 인스턴스는 롤업이 그날짜에 만들어낸다.
> **지운 계획의 과거 인스턴스는 소프트 삭제라 주간 통계가 깨지지 않는다.**

### 확인

```bash
.venv/bin/lt plan list --day $(date +%F)
.venv/bin/lt stats --day $(date +%F)
.venv/bin/lt report daily --day $(date +%F)     # --post 없으면 Slack 에 안 보낸다
```

- 달성률이 **0% 나 100% 로 몰려 있지 않은지** 본다. 몰려 있으면 계획 시간대가
  실제 생활과 안 맞는 것이다
- PNG 를 **실제로 열어본다** (`data/png/`). 계획 테두리가 격자와 맞는지는
  숫자로는 안 보인다 — 이 저장소는 그걸로 사고를 두 번 냈다

### 끝나면

| 파일 | 무엇 |
|---|---|
| [../HANDOFF.md](../HANDOFF.md) §4 | "`plan` 5건은 아직도 테스트용이다" 문장을 지운다 |
| [../HANDOFF.md](../HANDOFF.md) §6-③ | "실제 계획 넣기" 완료 표시 |
| [../README.md](../README.md) "사람이 해야 할 일" | 6번 행 ❌ → ✅ |

---

## 하지 말 것

- **`config/lifetrainer.toml` 을 커밋하지 마라.** gitignore 되어 있지만 `git add -f` 로
  뚫지 마라
- **코드를 고치지 마라.** 이 두 작업은 설정·데이터만 건드린다. 고칠 것이 보이면
  [issues/](issues/) 에 한 건으로 적고 넘어간다
- **`systemctl --user restart lifetrainer-worker` 를 함부로 하지 마라** — 야간 배치가
  도는 중(02:00~05:50)이면 진행 중인 GPU 잡이 끊긴다

## 끝나고 보고할 것

```
A. 검색 키    Serper 키 · doctor 결과 · "롤체" 답이 맞았는지 · 한국어 결과 품질
B. 계획        넣은 개수 · 달성률이 말이 되는지 · PNG 를 눈으로 봤는지
문서           위 표대로 고친 파일
그 외          하다가 발견한 이상한 것 (known-issues 에 적었으면 그 항목 번호)
```
