# 테스트가 이 기계의 설정 위에서만 초록불이었다

원래 이슈: `docs/issues/0003`

- **발견**: `0001` 을 막으려고 `config/lifetrainer.toml` 의 `serper_api_key` 를 비웠더니
  **코드를 한 줄도 안 고쳤는데 테스트가 깨졌다.**

  ```
  키 있음 → tests/test_llm_converse.py  15 passed
  키 비움 → 같은 파일 1 failed (IndexError: client.calls 가 비어 있음)
  ```

- **증상**: "테스트 1,168개 통과" 가 **이 기계의 이 설정에서만** 참인 문장이었다.
  README·HANDOFF 가 그 숫자를 내걸고 있다.

- **원인**: 픽스처가 운영 설정을 읽는다.

  ```python
  @pytest.fixture()
  def cfg(tmp_path):
      base = load_config()          # ★ config/lifetrainer.toml 실물
      return dataclasses.replace(base, db_path=tmp_path / "lt.db", data_dir=tmp_path)
  ```

  `db_path`·`data_dir` 만 갈아끼우고 **나머지는 운영 값 그대로**다. 같은 모양이
  **23개 파일에 복붙**돼 있었고 `conftest.py` 는 없었다.

  깨진 경로 자체는 정상 동작이었다 — `_must_refuse_web()` 이 "검색 수단이 없으면
  모델을 아예 안 부른다" 로 설계돼 있다. **코드는 맞고 테스트가 환경에 묶여 있었다.**

  숨은 겹이 하나 더 있었다: `bot_token` 이 비면 `openclaw_config` JSON 에서 읽는데,
  그 경로가 이 기기의 진짜 `~/.openclaw/openclaw.json` 이다. **example.toml 로
  바꿔도 진짜 Slack 토큰이 들어온다.**

- **★ 이미 한 번 겪고, 한 파일만 고쳤다**

  `tests/test_websearch.py` 의 픽스처 주석에 그 기록이 있다:

  > *"전에는 `load_config()` 를 그대로 썼다. … **2026-08-21 에 Serper 키를 넣자
  > '키 없음' 을 전제로 한 테스트 3개가 한꺼번에 깨졌다.** 키가 필요한 테스트는
  > `_with_keys` 로 직접 넣는다 — **기기 상태와 무관해야 한다.**"*

  `tests/test_cli.py` 도 자기 toml 을 따로 쓰며 같은 문제를 피하고 있었다.
  **진단도 처방도 정확했는데 그 파일에만 적용됐다.** 08-21 은 키를 *넣어서*,
  08-27 은 *빼서* 깨졌다 — 같은 결함의 양방향이다.

- **수정**: `tests/conftest.py` 신설. 세 겹을 한 곳에서 막는다.
  - `$LT_CONFIG` 를 `lifetrainer.example.toml` 사본으로 고정
  - 그 사본의 `openclaw_config` 를 **없는 경로**로 바꾼다 (Slack 토큰 폴백 차단)
  - `LT_<SECTION>_<KEY>` 환경변수를 전부 지운다

  모듈 수준에서 실행한다 — `conftest.py` 가 테스트 모듈 import 보다 먼저 읽히므로
  최상단에서 `load_config()` 를 부르는 파일까지 덮인다.

- **방어**: `tests/test_config_isolation.py` 4개.
  격리 사본을 쓰는가 · **바깥으로 나갈 자격증명이 전부 비어 있는가** ·
  `LT_*` 환경변수가 안 남았는가 · 토큰 폴백 경로가 실기기를 안 가리키는가.

  ★ **운영 파일을 옮겨서 확인하지 않는다.** 그 방식을 쓰다가 중간에 죽으면 서비스가
  설정을 잃는다. 실제로 이 수정을 검증하다 그렇게 됐다(복구함).

- **교훈**: **고친 것을 퍼뜨리지 않으면 같은 버그가 반대 방향으로 돌아온다.**

  08-21 에 원인을 정확히 진단하고 처방까지 썼다. 그런데 그 파일만 고쳤고, 왜
  다른 22개는 같은 구조가 아닌지 묻지 않았다. **한 파일에 적용한 처방은 그 파일의
  버그만 고친 것이지 그 부류를 고친 게 아니다.**

  → 픽스처·설정 로드처럼 **모든 테스트가 공유하는 것**에서 버그가 나오면,
    고치기 전에 **몇 곳이 같은 모양인지 먼저 센다.** 하나면 고치고, 여럿이면
    공용으로 올린다.
