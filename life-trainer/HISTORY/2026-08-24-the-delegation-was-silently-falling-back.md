# 위임이 조용히 강등된 채로 "잘 되고 있었다"

- **발견**: 사용자가 Slack 화면을 캡처해 보내며 **"답변 빠른데 엄청"** 이라고 했다.
  약 30초로 재둔 경로가 5초에 답할 리 없어서 로그를 봤다.

  ```
  WARNING lifetrainer.slackio.app: openclaw CLI 가 없어 대화 경로로 갑니다 (channel=D0123456789)
  INFO    대화 처리 완료 (5819ms, 라운드 1, 툴 [], 트리거 [])
  ```

  **오픈클로가 아니라 예전 경로가 답하고 있었다.** 봇 표시명이 "OpenClaw" 라
  화면만 봐서는 구분이 안 됐다.

- **증상**: 없다. 그게 문제다. 답은 빠르고 그럴듯했고 에러도 없었다.
  기능이 통째로 안 붙은 채 몇 시간을 돌았는데 **아무도 눈치채지 못했다.**

- **원인**: **테스트한 환경과 도는 환경의 PATH 가 달랐다.**

  `delegate.available()` 이 `shutil.which("openclaw")` 로 찾았다. 내 터미널에는
  nvm PATH 가 잡혀 있어 찾아졌지만, **systemd 사용자 서비스의 PATH 에는 없다.**

  ```
  $ command -v openclaw
  /home/user/.nvm/versions/node/v22.23.2/bin/openclaw   ← nvm 에만 있다
  $ ls /usr/local/bin/openclaw
  없음
  ```

  `node` 는 `/usr/local/bin` 으로 옮겨 뒀다(`openclaw-agent.md §4-10`).
  **`openclaw` CLI 래퍼는 같이 안 옮겼다.** 한쪽만 옮긴 것을 아무도 몰랐다.

- **수정**: PATH 에 기대지 않는다. `delegate.resolve_bin()` 이
  `/usr/local/bin` → `/usr/bin` → `which` → nvm glob 순으로 뒤져 **절대 경로**를
  찾고, 그 경로를 그대로 `subprocess` 에 넘긴다. `[agent] openclaw_bin` 으로
  못 박을 수도 있다.

- **방어**: `lt doctor` 에 항목을 더했다(16항목). 못 찾으면 **FAIL**, nvm 같은
  버전 매니저 경로면 WARN 이다.

  ★ **로그 경고만으로는 부족하다는 것이 이 사고의 핵심이다.** WARNING 은 실제로
  찍히고 있었다. 아무도 journalctl 을 안 봤을 뿐이다. **강등이 조용하면 강등이
  아니라 고장이다** — 사람이 보는 자리(`doctor`)에 올려야 한다.

- **교훈**: **개발 셸에서 되는 것은 서비스에서 된다는 뜻이 아니다.** 특히 PATH.
  이 저장소는 이미 같은 부류를 두 번 적었다 — 유닛이 nvm node 를 가리키던 건
  (§4-10), 감사가 드롭인을 못 보던 건(§4-11). 세 번째다.

  그리고 **"빠르다"가 좋은 소식이 아닐 때가 있다.** 성능이 예상보다 좋으면
  의심해야 한다 — 대개 일을 덜 하고 있다는 뜻이다. 이번엔 사용자의 그 한마디가
  아니었으면 계속 몰랐을 것이다.
