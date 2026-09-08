# `.gitignore` 의 `*.bak` 그물에 접미사 모양 구멍이 있었다 — API 키가 커밋됐다

- **발견**: 코드 리뷰 중에 `git ls-files | grep bak` 를 쳐 봤다. 가동 지도
  문서에는 "지금은 untracked 라 안전하지만 `git add -A` 한 번이면 영구히 남는다"
  라고 적혀 있었는데, **이미 남아 있었다.** 커밋 `49581fa`
  ("Add backup configuration files for Life Trainer with detailed settings")
  가 백업 6개를 통째로 넣었다.

- **증상**: 없다. 이것이 이 버그의 성질이다 — 아무것도 안 깨지고, 아무도 안 보고,
  파일은 조용히 이력에 남는다.

- **원인**: `.gitignore` 에 `*.bak` 만 있었다.

  ```
  config/slack-app-manifest-merged.json.bak     → 막힘   (*.bak)
  config/lifetrainer.toml.bak-portmove          → 통과   ← 접미사가 붙었다
  config/lifetrainer.toml.bak-before-ingest     → 통과
  config/lifetrainer.toml.bak-before-tunnel     → 통과
  config/lifetrainer.toml.bak-before-slack-agent→ 통과
  config/rules.yaml.bak-before-android          → 통과
  android/docs/runbook.md.bak-portfix           → 통과
  ```

  `*.bak` 는 **`.bak` 로 끝나는 이름**만 막는다. 백업을 만들 때 손이 붙인
  `-portmove`·`-before-ingest` 같은 꼬리표가 확장자를 확장자가 아니게 만들었다.
  같은 손이 만든 파일인데 그물의 반대편으로 떨어졌다.

  들어간 것: **Serper API 키(40자)가 백업 4개에.** Slack 토큰은 그 시점 백업에는
  비어 있었다(실설정 `config/lifetrainer.toml` 은 `.gitignore:9` 가 정확히
  막고 있고, 지금도 추적되지 않는다).

- **수정**: 두 줄을 더해 그물을 넓히고(`*.bak-*` · `*.bak.*`), 추적만 끊었다.

  ```bash
  git rm --cached <백업 6개>     # 인덱스에서만 뺀다 — 디스크의 파일은 그대로
  ```

  ★ **백업 파일은 지우지 않았다.** 사용자가 손으로 만든 것이고, 무엇을 언제
  바꿨는지의 유일한 기록이다. 작업 전후로 md5 를 대조해 6개 전부 동일함을 확인했다.

> **★ 2026-08-31 — 닫혔다. 다만 전제 둘이 그 사이 바뀌어 있었다.**
>
> **① "원격이 없다" 가 더 이상 사실이 아니다.** 2026-08-28 에 원격이 붙고 push 됐다.
>
> ```
> git branch -r --contains 49581fa   → origin/main · origin/qwen3-openclaw-agent
> gh repo view --json visibility     → PRIVATE
> git ls-tree -r origin/main | .bak  → (없음)   ← rm --cached 상태가 반영됨
> ```
>
> 현재 트리에는 없지만 **키 블롭이 GitHub 이력에 올라가 있다.** 비공개라 노출은
> 아니지만 *"기기 밖으로 나간 적 없다"* 는 틀린 문장이 됐다.
>
> **② 그런데 키는 이미 갈려 있었다.**
>
> ```
> git 이력 · 디스크 백업 4개   앞4 411a…  sha256 0e7343926bcd   ← 노출된 것
> 현재 운영 설정               앞4 cad6…  sha256 a7d9ebdde911   ← 다른 키
>                              config/lifetrainer.toml 최종수정 08-28 17:48
> ```
>
> **사용자 확인 결과 `411a…` 는 Serper 대시보드에 없다 — 무효다.**
> 위 선택지 중 **①(재발급)이 이미 실행돼 있었고**, 그래서 `filter-repo` 는 안 한다.
> 죽은 키가 비공개 저장소 이력에 남는 것은 감수한다.
>
> ★ **문제는 그 교체가 어디에도 안 적혀 있었다는 것이다.**
> `config/lifetrainer.toml` 이 gitignore 라 자격증명 변경은 **git 에 흔적을 안 남긴다.**
> 그래서 08-31 에 이 문서를 다시 읽고 **이미 한 일을 또 제안했다.**
> 이 저장소가 아는 부류다 — [바깥으로 나간 것이 자기 흔적을 안 남겼다](2026-08-27-what-went-out-left-no-trace-of-its-own.md)
> 의 자격증명판. → `config/CREDENTIALS.md` 에 **값 없이 교체 이력만** 남기기로 했다.

- **방어**: `.gitignore` 패턴 자체가 방어다. 확인은 `git add -A --dry-run` 에
  `.bak` 가 하나도 안 뜨는 것으로 했다.

- **교훈**: **`*.확장자` 패턴은 "그 확장자로 끝나는 것"만 막는다.** 사람이 파일을
  복사할 때 붙이는 꼬리표(`-before-X`)는 확장자를 문자열 중간으로 밀어 넣는다.
  비밀이 든 파일을 이름 규칙으로 막을 때는 **접미사가 붙은 형태를 같이 적는다.**

  더 중요한 것은 따로 있다. 이 저장소의 문서가 "untracked 라 안전하다"고
  적고 있었고 그것이 틀렸다. **위험을 문서에 적은 시점의 상태로 믿지 말고,
  적을 때마다 `git ls-files` 로 다시 확인한다** — 이 저장소가 이미 네 번 겪은
  "같은 값을 여러 곳에서 각자 관리한다"의 보안판이다.
