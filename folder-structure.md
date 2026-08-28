# 이상적인 폴더 구조 — 목표안

> 2026-08-29 · **이동 비용을 빼고** "다시 만든다면 어떻게 둘까" 를 적은 것이다.
> 지금 하자는 제안이 아니다 — 언제 하는 게 값이 큰지는 [§언제 하나](#언제-하나) 에 있다.
> 지금 구조는 [README §디렉토리 구조](README.md) 가 정본이다.

---

## 진단 — 최상위에 **생애주기 셋**이 섞여 있다

| | 성격 | 예 |
|---|---|---|
| **잰 것** | 한 번 재면 불변 기록. 다시 돌 일이 드물다 | `membw.cu` · nsys 프로파일 · llama-bench |
| **도는 것** | 매일·매 push 돈다. 지금 상태를 말한다 | `heartbeat` · `doctor` · `check-*` |
| **만든 것** | 계속 자란다. 자체 문서·테스트를 갖는다 | Life Trainer |

**`bench/` 가 앞의 둘을 한 폴더에 담고 있다.** `membw.cu`(한 번 잼)와 `heartbeat.sh`(15분마다)가
같은 자리에 있다. 2026-08-28 에 운영·검사 스크립트 9개가 들어가면서 그렇게 됐다 —
**규칙이 없어서 들어갔다.**

```
측정 10 · 운영 9 · 검사 3 · 조사 5      ← bench/ 27개의 성격 분포
```

## 그리고 `benchmarks/` 는 **저장소 구조의 복제본**이다

```
저장소:       bench/          results/          research/*.md
benchmarks/:  bench/          results/          benchmark-results.md
```

`bench` ↔ `benchmarks/bench` 충돌은 **이름 문제가 아니라 같은 구조가 두 번 있는 것**이다.
이름을 바꿔도 복제는 남는다.

---

## 목표 구조

```
project-jetson/
├── measure/                  ← 잰 것. 결과는 불변 기록
│   ├── tools/                  membw.cu · *-bench.py · thermal-test · profile-decode
│   │                           verify-jetpack · build-llamacpp · plot · gh-*
│   ├── results/                nsys · csv · tegrastats · raw/run-<타임스탬프>/
│   ├── figures/                results/ 에서 재생성
│   └── findings/               hardware · performance · llm-models · decode-profile
│                               model-suite   ← benchmark-results.md 가 여기로 녹는다
│
├── operate/                  ← 도는 것. 지금 상태
│   ├── tools/                  status · host-status · daily-check · heartbeat · verify-boot
│   │                           check-docs · check-links · pre-push · recovery-bundle
│   │                           harden-network · enable-persistent-journal · gen-environment
│   ├── systemd/                llama-server · jetson-daily-check · jetson-heartbeat
│   └── notes/                  llm-runtime.md · agent-gateway.md
│
├── life-trainer/             ← 만든 것 (소문자로)
│
├── docs/                     ← 저장소 차원의 기획만. plans/ · archive/
├── refs/                     ← 포인터만. models/ · reference/
└── CLAUDE.md  README.md  Makefile  environment.md
```

## 왜 이게 나은가 — 다섯

**① `figures/` 가 `results/` 옆으로 간다.**
지금은 최상위 형제인데 실제로는 **종속**이다 — `make figures` 가 `results/` 를 읽는다.
떨어져 있어서 "그림은 왜 재생성되나" 가 안 보인다.

**② 해석이 그 근거 데이터 옆으로 간다.**
지금 `research/hardware.md` 가 `../results/` 를 가리키며 트리를 가로지른다.
**잰 것과 그 해석은 같은 상자에 있어야 한다.**

**③ `benchmarks/` 가 녹는다.**
도구는 `measure/tools/`, 원본은 `measure/results/raw/run-20260818T*/`(이미 타임스탬프가 있다),
분석은 `measure/findings/model-suite.md`. **복제가 사라지므로 이름 충돌도 같이 사라진다** —
이름을 바꿔서 피하는 것이 아니라.

**④ "이 기기를 돌리는 법" 이 하나로 모인다.**
지금은 `runtime/`(문서+유닛) · `bench/`(운영 스크립트) · `Life_Trainer/deploy/` 셋에 흩어져 있다.

**⑤ 새 파일을 어디 둘지 질문이 하나가 된다.**
*"이건 재는 건가, 돌리는 건가?"* — 2026-08-28 에 9개가 잘못 들어간 이유가 이 질문이 없어서였다.

---

## 안 바꾸는 것 — 근거가 있는 중복은 문제가 아니다

| | 왜 |
|---|---|
| `systemd/` 3곳 유지 | **소유자 기준**이다 (`runtime/`=공유 · 앱 · 게이트웨이 드롭인). [runtime/README](runtime/README.md) 가 근거를 적어 뒀다 |
| `apps/life-trainer/` 로 감싸지 않음 | **앱이 하나다.** 둘째가 생길 때 만든다 |
| `Life_Trainer/docs/` 삼분할 | `progress`(기능) · `HISTORY`(깨진 가정) · `issues`(안 고친 것) 는 이 저장소에서 **가장 잘 작동하는 부분**이다 |
| `research/` ↔ 앱의 `docs/research/` | 스코핑이라 정상. 다만 README 한 줄로 경계를 적는다 — **잰 것 / 읽은 것** |

## 같이 정리할 것 — `reference` 가 네 곳이다

**대문자·소문자·축약이 의미 차이를 나타내지 않는다.**

```
reference/                        타 프로젝트 클론 (URL+커밋만, 실물은 ~/reference/)
Life_Trainer/REFERENCE/           harugyeol·slackbot 흡수 (README 만 추적)
Life_Trainer/docs/reference/      planner_example.png 하나  ← 폴더일 이유가 없다
Life_Trainer/docs/design/refs/    타사 제품 캡처 23개
```

→ 소문자로 통일하고, 파일 하나짜리 폴더는 없앤다.

---

## 폰 앱은 여기 없다 (확인함, 2026-08-29)

```
Life_Trainer/android/     문서 6개뿐 (README · fork-build · phone-titles · plan · research · runbook)
.apk · .aar · gradle      트리에 없음
```

앱 소스는 별 저장소 **[`donghee-ai/lt-phone`](https://github.com/donghee-ai/lt-phone)** 에 있다.
`android/` 는 **젯슨 쪽 수신 설계와 포크 결정 기록**이고, 앱 자체가 아니다.
목표 구조에서도 `life-trainer/android/` 로 그대로 둔다 — 옮길 이유가 없다.

---

## 언제 하나

구조 개선의 값은 **다음 파일을 어디 둘지 헷갈리지 않는 것**이고, 그 값은
**사람이 늘거나 파일이 늘 때** 커진다. 지금은 1인이고, 진짜 병목은 구조가 아니다:

```
근거 없는 주장 "24시간 가동" 하나 남음      E-3 측정
LICENSE 가 없음                             공개 전 필수
에이전트 26~106초 (이제 기본 경로)           issues/0007
채점기와 실사용이 반대                       issues/0008~0010
```

**구조를 바꿔도 이 넷은 그대로다.**

| 시점 | 무엇 |
|---|---|
| **지금** | `reference` 4곳 정리 — 대소문자가 뜻을 안 나타내는 건 지금도 헷갈린다 |
| **중간** | `bench/` 에 **운영 스크립트를 더 넣지 않는다**는 규칙 한 줄 ([CLAUDE.md](CLAUDE.md)) |
| **공개 직전** | 위 목표 구조로 한 번에. 그때가 **남이 처음 보는 시점**이라 값이 최대이고, 링크도 어차피 전면 점검된다 |

> **구조는 헷갈림을 줄이지만 사고를 막지는 않는다.** 2026-08-28 에 만든 검사·감시
> 체계가 이것보다 값이 크다고 판단해 순서를 그렇게 뒀다.
