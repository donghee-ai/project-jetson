# 무엇이 설치되는지를 고정하는 것이 없다

- **상태**: `p-` — **2026-08-31 에 pip·npm 둘 다 고정했다.** 남은 것은 apt 한 갈래와 playwright 다
- **발견**: 2026-08-28 정비 계획 리뷰
- **증상**: [`pyproject.toml`](../../pyproject.toml) 의 의존성이 **전부 `>=`** 이고 lock 이 없었다.
  CI(ubuntu-latest, 매번 새로 설치)와 이 기기(`.venv`, 몇 주 전 설치)가 서로 다른 판을 썼다.
- **원인**: 1인 프로젝트라 "설치되는 것은 늘 같다"고 가정했다.
  두 곳에서 각자 설치하는 순간 그 가정은 깨진다.
- **영향**: CI 가 초록불인데 기기에서 깨지는(또는 그 반대) 상황에서 **버전이 용의선상에서
  빠진다.** 이 저장소가 이미 겪은 부류다 —
  [테스트가 이 기계의 설정 위에서만 초록불이었다](../../HISTORY/2026-08-27-the-suite-was-green-on-this-machine-only.md).

## ★ 2026-08-31 — 조사하다 보니 **의존성이 세 갈래**였다

한 파일로 못 묶는 이유가 여기 있다. 갈래마다 설치 주체가 다르다.

| 갈래 | 무엇 | 고정 상태 |
|---|---|---|
| **pip** | `life-trainer` 앱 — 직접·전이 의존성 | ✅ **닫혔다** (아래) |
| **apt** | `measure/tools/plot.py` 의 matplotlib. 시스템 python3 에 `python3-matplotlib` 로 있다 | ⚠️ apt 가 정한다. **pip 으로 고정하면 안 된다** — dist-packages 를 가린다 |
| **npm** | `openclaw` CLI · `gh-research*.mjs` 의 playwright | ✅ **닫혔다** (아래) |

★ 전에는 루트에 `requirements.txt` 가 있었는데 `matplotlib==3.5.1` 이라고 **pip 문법**으로
적혀 있었다. 실제로는 apt 가 깐 것이라 그대로 `pip install -r` 하면 apt 판을 가리거나
externally-managed 로 막힌다 — **형식이 내용과 다른 것을 주장하고 있었다.** 지웠고,
사실은 [`measure/README.md`](../../../measure/README.md) 의 표로 옮겼다.

## 닫힌 것 — pip

`life-trainer/constraints.txt` 가 이 기기의 `.venv` 를 정본으로 판을 고정한다.
CI 가 `pip install -e '.[dev]' -c constraints.txt` 로 설치하고 `pip check` 까지 돈다.

```bash
make lock      # 이 기기의 .venv 에서 다시 뽑는다 (operate/tools/lock-deps.sh)
```

**무엇을 설치할지는 여전히 `pyproject.toml` 이 정한다.** constraints 는 *어느 판*만 정한다 —
둘을 한 파일에 합치면 "왜 깔렸나" 와 "왜 이 판인가" 가 섞인다.

고정본이 `pyproject.toml` 과 안 맞으면 **CI 설치 단계에서 시끄럽게 실패한다.** 그게 목적이다.

> **왜 검사를 따로 안 만들었나.** *"`.venv` 가 `constraints.txt` 와 다르다"* 는 검사를
> 생각했지만 **언제 안 울리는지를 못 정했다** — 로컬에서 뭘 하나 깔면 바로 울리고,
> 그때마다 `make lock` 이 맞는 대응인 것도 아니다 ([CLAUDE.md §1](../../../CLAUDE.md)).
> 대신 CI 가 **고정본으로 실제 설치를 해 본다.** 안 맞으면 거기서 깨진다.

## 닫혀 있던 것 — 확인해 보니 이미 돼 있었다

- **llama.cpp** commit·빌드 옵션·CUDA arch → [`operate/notes/llm-runtime.md §1`](../../../operate/notes/llm-runtime.md)
  이 `b1-a94d563` · `CMAKE_CUDA_ARCHITECTURES=87` · CUDA 12.6 을 갖고 있다.
  실물과 대조했다 (`git -C ~/llama.cpp log -1` → 같은 커밋)
- **Node · OpenClaw 판** → [`operate/notes/agent-gateway.md §1`](../../../operate/notes/agent-gateway.md)
  이 CLI 판·빌드 해시·node 판을 갖는다. 실물과 일치
- **GitHub Actions 서드파티 액션 고정** → **서드파티가 없다.** `actions/checkout` ·
  `actions/setup-python` 둘 다 first-party 다. 2026-08-31 에 Node 20 deprecation 을
  없애려고 major 를 올렸다

## 닫힌 것 — npm

`operate/npm-globals.txt` 가 전역 패키지를 **prefix 별로** 기록한다. `make lock` 이 같이 뽑는다.

★ **`npm ls -g` 를 안 쓴다.** 이 기기에는 node 가 둘이고, 사용자 셸에서는 nvm 이 prefix 를
깔아 `npm -g` 가 **nvm 쪽**을 가리킨다. 그런데 게이트웨이가 실행하는 openclaw 는
**시스템 쪽**에 있다. 한 줄을 믿으면 엉뚱한 트리를 기록한다 — 실제로 그렇게 한 번 잘못 셌다.
그래서 **경로를 직접 읽는다** ([CLAUDE.md §2](../../../CLAUDE.md): 경로가 아니라 실물을 본다).

## 닫힌 것 — 업그레이드 후 smoke test

```bash
make smoke        # 기기만 (몇 초) — 실물 · 고정본 대조 · verify-boot
make smoke-live   # + 실제로 한 턴 돌려 MCP 툴이 불리는지 (30~100초)
```

`npm install -g openclaw@…` 는 **유닛도 헬스체크도 안 깨뜨리면서** 배선을 바꾼다.
`lt doctor` 와 `verify-boot` 은 *"지금 되나"* 를 묻지 *"올린 뒤에도 되나"* 를 안 묻는다.
이 관문이 그 자리를 맡는다 — 알림이 아니라 **사람이 업그레이드 뒤 부르는 것**이다.

고정본을 일부러 어긋내 빨간불이 나오는 것, 실제 한 턴에서 `lt__get_plans` 가 불리는 것을
둘 다 확인했다.

## 남은 것

- **apt 갈래는 고정 안 한다** — `python3-matplotlib` 은 apt 가 정한다.
  pip 으로 덮으면 dist-packages 를 가린다. 재현이 필요하면 **apt 판을 적는 것**이 답이지
  pip 으로 옮기는 것이 답이 아니다. 지금은 [`measure/README.md`](../../../measure/README.md) 의 표가 그 역할
- **`playwright` 가 아예 없다.** `measure/tools/gh-research*.mjs` 는 `import { chromium } from 'playwright'`
  인데 전역에는 `@playwright/mcp` 만 있다 — **그 셋은 지금 안 돈다.**
  조사용 일회성 스크립트라 급하지 않지만, 돈다고 믿고 있으면 안 된다
- **정책** — first-party 액션은 deprecation 경고가 뜨면 올린다. 서드파티를 쓰게 되면 SHA 로 고정한다
  ([ci.yml](../../../.github/workflows/ci.yml) 머리말)
