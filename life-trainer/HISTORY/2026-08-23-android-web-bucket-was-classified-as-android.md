# 폰 web 버킷이 `web` 이 아니라 `android` 로 분류됐다

- **발견**: 폰 수집 상태를 훑다가 `aw_bucket.type` 을 눈으로 보고. 테스트는 초록불,
  에러 로그도 없었다.

- **증상**: 없다 — **그게 문제였다.** 폰 브라우징이 `rules.yaml` 의 URL 규칙을
  하나도 못 받고 앱 이름만으로 `browsing`/`away` 에 뭉뚱그려졌다.
  `arxiv.org → research`, `op.gg → gaming` 같은 규칙이 조용히 안 걸렸다.

- **원인**: **판별 순서에서 `web` 이 `android` 뒤에 있었다.**

  ```python
  if "unlock"  in bid: return "unlock"
  if "afk"     in bid: return "afk"
  if "android" in bid: return "android"   # ← aw-watcher-android-web 이 여기서 잡힌다
  if "web"     in bid: return "web"       # ← 도달 불가
  ```

  docstring 은 "순서가 곧 우선순위다"라며 `unlock`·`afk` 를 앞에 둔 이유를 적어놨다.
  **`web` 만 같은 처리를 못 받았다.** 롤업은 URL 을 `dev.buckets["web"]` 에서만
  꺼내므로 폰의 web 버킷은 영원히 비어 있었다.

  근본 원인은 그 위층에 있다 — `aw-watcher-android-media`/`-web` 두 워처를
  08-22 에 붙이면서 **`android/docs/` 의 버킷 규격을 안 거쳤다.** 문서가 참조
  사슬 안에 있어서 이 저장소의 문서-코드 정합성이 유지돼 왔는데, 이 둘만 사슬
  밖에 있었다.

- **수정**: `web` 을 `android` 위로 한 줄 이동. `aw_bucket.type` 1건 갱신,
  08-12~23 재롤업, 서비스 재시작(폰 push 를 받는 `lifetrainer-web` 이 옛 코드를
  메모리에 들고 있었다).

  **효과는 예상보다 훨씬 작았다.** 처음엔 "폰 크롬 929분이 URL 분류를 새로 받는다"고
  봤는데, 크롬 **사용 시간**과 web 워처가 **뽑아낸 URL** 을 혼동한 것이었다.
  실제 web 이벤트는 9건·0.1분뿐이고 재롤업 후 변화는 1.3분이었다.
  **지금 이득이 아니라 잠복 버그 제거다.**

- **방어**: `tests/test_aw_sync.py`
  - `test_bucket_type_android_web_is_web_not_android`
  - `test_bucket_type_android_media_is_android`
  - `test_bucket_type_order_is_specific_before_general` — 폰 버킷 다섯 개의
    타입을 표로 검사한다. **새 워처를 붙이면 이 표에 한 줄 추가하는 것이 절차다**

  구조 쪽 방어로 `android/docs/fork-build.md` 에 버킷 목록표를 넣고,
  "여기 추가하면 젯슨의 판별 순서를 반드시 같이 확인" 을 명시했다.

- **교훈**: **부분문자열 판별은 순서가 곧 계약이다.** 이름이 서로를 포함하는
  집합(`android`, `android-web`, `android-afk`…)에서는 구체적인 것이 먼저 와야 하고,
  그 순서는 주석이 아니라 **테스트로** 고정돼야 한다.

  그리고 **규격 문서를 거치지 않은 확장은 상대편 코드를 조용히 깨뜨린다.**
  이 저장소의 문서가 지금까지 안 썩은 것은 부지런해서가 아니라 참조 사슬 안에
  있었기 때문인데, 사슬 밖으로 나간 두 워처가 정확히 그 대가를 치렀다.
