# 알려진 문제 — **2026-08-27 에 [`issues/`](issues/) 로 쪼개졌다**

이 파일은 **이정표만 남기고 비웠다.** 내용은 각 항목의 실제 상태를 다시 재서
`HISTORY/`(가정이 깨진 것) 와 `docs/issues/`(아직 열린 것) 로 나눠 옮겼다.

> **지우지 않은 이유**: 이 파일을 가리키는 링크가 60곳 있고, 그중 다수가
> `docs/progress/` 와 `HISTORY/` 다. **그것들은 당시의 사실을 적은 기록이라
> 고치면 안 된다.** 링크는 살려두고 여기서 새 위치를 알려준다.

## 어디로 갔나

| 옛 § | 무엇 | 지금 |
|---|---|---|
| §1 | 단일 출처 앵커링 | [`issues/0006`](issues/h-0006-the-model-cites-one-source-and-stays-there.md) — 세 겹 중 둘은 막힘 |
| §2 | 야간 배치가 503 로 죽는다 | [`HISTORY 08-21`](../HISTORY/2026-08-21-a-loading-model-burned-the-whole-queue.md) — 닷새 무실패로 검증 |
| §3 | 못 읽는 사이트들 | [`issues/0012`](issues/n-0012-what-the-fetcher-cannot-read.md) — 명시된 한계 |
| §4 | 네이버 엔드포인트 은퇴 | [`HISTORY 08-25`](../HISTORY/2026-08-25-it-worked-because-the-key-was-empty.md) — 고치지 않고 지웠다 |
| §5 | 틀어둔 미디어가 기기를 밀어냄 | [`HISTORY 08-25`](../HISTORY/2026-08-25-a-video-left-playing-outranked-the-work.md) |
| §6 | example.toml 키 누락 | **해결 (2026-09-03)** → [HISTORY](../HISTORY/2026-09-03-the-template-kept-falling-behind-the-code.md) · 키 집합 검사가 지킨다 |
| §7 | 에이전트 경로가 느리다 | [`issues/0007`](issues/w-0007-the-agent-path-takes-half-a-minute.md) |
| §8 | 채점기가 실데이터에 쓴다 | [`issues/0008`](issues/h-0008-the-scorer-writes-to-the-real-database.md) |
| §9 | 에이전트가 Slack 에 안 붙음 | 해결 (08-24) — [진행 기록](progress/2026-08-24.md) |
| §11 | 정리 안 한 파일들 | `.bak` 은 [`HISTORY 08-25`](../HISTORY/2026-08-25-the-gitignore-net-had-a-suffix-shaped-hole.md) · 세션 누적은 `issues/0011` (**해결 2026-09-07** — 일주일 보존 타이머) |
| §12 | `[id]` 를 명령 번호로 쓴다 | [`HISTORY 08-24`](../HISTORY/2026-08-24-fixing-the-model-cost-more-than-accepting-what-it-sent.md) |
| §13 | 대화 맥락의 날짜를 놓친다 | [`HISTORY 08-24`](../HISTORY/2026-08-24-an-enum-swallowed-the-date.md) |
| §14 | 이름으로 지목하면 첫 항목 | [`issues/0010`](issues/h-0010-the-model-picks-the-first-item-when-told-a-name.md) |
| §15 | 채점 계획을 손으로 심어야 | [`issues/0009`](issues/h-0009-the-scorer-needs-plans-planted-by-hand.md) |
| §16 | `summary_limit` 600 vs 30 | [`issues/0005`](issues/p-0005-the-default-is-a-number-measured-on-one-machine.md) — **아직 그대로** |
| §17 | 백업 커밋의 Serper 키 | **해결 (08-27)** — 키 재발급 + 옛 키 폐기 확인(HTTP 403). 이력의 값은 죽은 문자열이다 |

§10 은 원래 없었다 (번호만 건너뜀).

## 이관에서 나온 것

**옮기는 작업이 아니라 하나씩 실제로 재는 작업이었다.** 해결됐다고 적힌 것 중
안 된 것(§2 의 복구분 203건)과, 안 됐다고 적힌 것 중 이미 된 것이 섞여 있었다.

새로 쓰는 것은 [`issues/`](issues/) 에 **한 건에 한 파일**로 쓴다.
이 파일에는 더 쓰지 않는다.
