# 폴더 구조 — 왜 이렇게 잡았나

> 2026-08-29 에 **목표안**으로 썼고, **2026-08-31 에 실제로 옮겼다.**
> 지금 구조의 정본은 [README §디렉토리 구조](../README.md) 다.
> 이 문서는 *"왜 이 모양인가"* 와 **옮기면서 나온 것**을 남긴다.

## 옮기고 나서 (2026-08-31)

`measure/`(잰 것) · `operate/`(도는 것) · `life-trainer/`(만든 것) · `docs/` · `refs/` 로 갈랐다.
`benchmarks/` 는 이름을 바꾼 게 아니라 **녹였다** — 아래 §복제본 진단이 그 이유다.

**아래 §언제 하나가 잡은 방아쇠는 안 당겨졌다.** 거기 적힌 시점은 *"공개 직전"* 인데
2026-08-31 에 **공개를 안 하기로** 정했다. 대신 근거를 다시 세웠다 —
`bench/` 혼재는 이미 비용을 냈고(운영 스크립트 9개가 하루에 잘못 들어갔다),
`benchmarks/` 는 저장소 구조의 복제본이었고, 참조가 두 번 끊긴 것을 아무도 몰랐다.
경위는 [`docs/plans/restructure-2026-08-31.md`](archive/restructure-2026-08-31.md).

### 옮기면서 나온 것 — 전부 "경로가 두 곳에 적혀 있었다"

| 무엇 | 어떻게 드러났나 |
|---|---|
| `operate/tools/install.sh` 가 유닛 목록을 **스크립트 안에** 갖고 있었다 | 심링크를 지웠다 다시 걸었더니 `jetson-daily-check`·`jetson-heartbeat` 둘이 안 돌아왔다. Life Trainer 쪽은 이미 `desired-state.txt` 를 읽게 고쳐져 있었는데 **이쪽만 안 고쳤다** |
| 스크립트가 저장소 루트를 **자기 깊이로** 계산한다 | `tools/` 로 한 칸 깊어지자 11개가 전부 저장소 밖을 가리켰다 |
| 저장소 밖 경로가 저장소 규칙에 걸렸다 | `~/models` 를 `~/refs/models` 로 바꿔 버려 llama-server 가 모델을 못 찾았다. **홈 아래는 이 저장소가 아니다** |
| 유닛이 `%h/…` 로 쓴 경로 | 위와 같은 사고가 `%h/refs/models` 로 한 번 더 났다 |

★ **재구조화의 실제 위험은 링크가 아니라 이 넷이었다.** 마크다운 링크는 `make links` 가
전부 잡았고, 코드→문서 참조는 [2026-08-31 에 만든 검사](../life-trainer/HISTORY/2026-08-31-the-references-did-not-follow-the-file.md)가
잡았다. 남은 것은 **검사가 없는 자리** — 유닛·venv·홈 아래 경로였다.

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

## 그리고 `measure/` 는 **저장소 구조의 복제본**이다

```
저장소:       bench/       results/       research/*.md
benchmarks/:  bench/       results/       benchmark-results.md
```

`bench` ↔ `measure/bench` 충돌은 **이름 문제가 아니라 같은 구조가 두 번 있는 것**이다.
이름을 바꿔도 복제는 남는다.

---

## 목표 구조

```
project-jetson/
├── measure/                  ← 잰 것. 결과는 불변 기록
│   ├── tools/                  membw.cu · *-bench.py · thermal-test · profile-decode
│   │                           verify-jetpack · build-llamacpp · plot · gh-*
│   ├── measure/results/                nsys · csv · tegrastats · raw/run-<타임스탬프>/
│   ├── measure/figures/                measure/results/ 에서 재생성
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
├── refs/                     ← 포인터만. refs/models/ · refs/reference/
└── CLAUDE.md  README.md  Makefile  environment.md
```

## 왜 이게 나은가 — 다섯

**① `measure/figures/` 가 `measure/results/` 옆으로 간다.**
지금은 최상위 형제인데 실제로는 **종속**이다 — `make figures` 가 `measure/results/` 를 읽는다.
떨어져 있어서 "그림은 왜 재생성되나" 가 안 보인다.

**② 해석이 그 근거 데이터 옆으로 간다.**
지금 `measure/findings/hardware.md` 가 `../measure/results/` 를 가리키며 트리를 가로지른다.
**잰 것과 그 해석은 같은 상자에 있어야 한다.**

**③ `measure/` 가 녹는다.**
도구는 `measure/tools/`, 원본은 `measure/measure/results/raw/run-20260818T*/`(이미 타임스탬프가 있다),
분석은 `measure/findings/model-suite.md`. **복제가 사라지므로 이름 충돌도 같이 사라진다** —
이름을 바꿔서 피하는 것이 아니라.

**④ "이 기기를 돌리는 법" 이 하나로 모인다.**
지금은 `operate/`(문서+유닛) · `bench/`(운영 스크립트) · `life-trainer/deploy/` 셋에 흩어져 있다.

**⑤ 새 파일을 어디 둘지 질문이 하나가 된다.**
*"이건 재는 건가, 돌리는 건가?"* — 2026-08-28 에 9개가 잘못 들어간 이유가 이 질문이 없어서였다.

---

## 안 바꾸는 것 — 근거가 있는 중복은 문제가 아니다

| | 왜 |
|---|---|
| `systemd/` 3곳 유지 | **소유자 기준**이다 (`operate/`=공유 · 앱 · 게이트웨이 드롭인). [operate/README](../operate/README.md) 가 근거를 적어 뒀다 |
| `apps/life-trainer/` 로 감싸지 않음 | **앱이 하나다.** 둘째가 생길 때 만든다 |
| `life-trainer/docs/` 삼분할 | `progress`(기능) · `HISTORY`(깨진 가정) · `issues`(안 고친 것) 는 이 저장소에서 **가장 잘 작동하는 부분**이다 |
| `measure/findings/` ↔ 앱의 `docs/measure/findings/` | 스코핑이라 정상. 다만 README 한 줄로 경계를 적는다 — **잰 것 / 읽은 것** |

## 같이 정리할 것 — `reference` 가 네 곳이다

**대문자·소문자·축약이 의미 차이를 나타내지 않는다.**

```
refs/reference/                        타 프로젝트 클론 (URL+커밋만, 실물은 ~/reference/)
life-trainer/refs/reference/           harugyeol·slackbot 흡수 (README 만 추적)
life-trainer/docs/refs/reference/      planner_example.png 하나  ← 폴더일 이유가 없다
life-trainer/docs/design/refs/    타사 제품 캡처 23개
```

→ 소문자로 통일하고, 파일 하나짜리 폴더는 없앤다.

---

## 폰 앱은 여기 없다 (확인함, 2026-08-29)

```
life-trainer/android/     문서 6개뿐 (README · fork-build · phone-titles · plan · research · runbook)
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
| **지금** | `reference` 4곳 정리 — 대소문자가 뜻을 안 나타내는 건 지금도 헷갈린다 — ✅ 했다 |
| **중간** | `bench/` 에 **운영 스크립트를 더 넣지 않는다**는 규칙 한 줄 ([CLAUDE.md](../CLAUDE.md)) — ✅ 했다 |
| ~~**공개 직전**~~ | ✅ **2026-08-31 에 했다.** 공개는 안 하기로 했고, 방아쇠는 위 §옮기고 나서 가 대신했다 |

> **구조는 헷갈림을 줄이지만 사고를 막지는 않는다.** 2026-08-28 에 만든 검사·감시
> 체계가 이것보다 값이 크다고 판단해 순서를 그렇게 뒀다.
