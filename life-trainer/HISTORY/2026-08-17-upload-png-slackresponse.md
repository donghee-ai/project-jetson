# `dict(resp)` 한 줄이 일일 리포트를 통째로 막고 있었다

- **발견**: 슬래시 명령을 처음 실제로 쳐봤을 때. `/view` 가 **PNG 는 올리고 동시에
  "오류가 발생했습니다" 도 띄웠다.** 둘 다 나온 게 단서였다 — 업로드는 성공했는데
  그 다음에 터졌다는 뜻이다.

- **증상**:

  ```
  ValueError: dictionary update sequence element #0 has length 1; 2 is required
    notify.py:164  return dict(resp)
  ```

  같은 경로를 쓰는 곳이 하나 더 있었다. `report` 테이블을 열어보니:

  ```
  [3] daily   2026-08-17  발송=❌  [2] weekly  2026-08-16  발송=❌  [1] daily 2026-08-16 발송=❌
  lifetrainer-daily.service: Main process exited, code=exited, status=1/FAILURE
  ```

  **일일 리포트는 한 번도 나간 적이 없었다.** 리포트 본문은 로그에 멀쩡히 찍히고
  PNG 도 만들어진 뒤, 발송 직전에 죽고 있었다.

- **원인**: `slack_sdk` 의 `files_upload_v2` 는 **dict 가 아니라 `SlackResponse`
  객체**를 돌려준다. `SlackResponse.__iter__` 는 페이지네이션 이터레이터라
  `dict()` 의 재료가 될 수 없다. payload 는 `.data` 에 있다.

  깨진 가정: **"SDK 응답은 dict 다."** 응답이 `resp["ok"]`, `resp.get("file")` 로
  잘 동작하니 dict 라고 믿었다. 첨자 접근이 된다고 dict 인 것은 아니다.

  바로 옆 줄의 `if isinstance(resp, dict) and resp.get("ok") is False:` 도 같은
  착각의 흔적이다 — 이 조건은 **한 번도 참이 된 적이 없다.** 업로드 실패를 잡으려고
  넣은 방어가 처음부터 죽어 있었다.

- **왜 안 잡혔나 ★**: 테스트의 가짜 클라이언트가 **dict 를 돌려주고 있었다.**

  ```python
  self.files_upload_v2_result = {"ok": True, "file": {"id": "F123"}}   # 실제와 다름
  ```

  `dict({"ok": ...})` 는 당연히 성공한다. 그래서 `post_report` 테스트가 초록불인
  채로 실기기에서는 100% 실패했다. **가짜가 실물보다 착했다.**

  핸드오프에는 이 미발송이 "Slack `default_channel` 미설정 → 발송 대기" 로 적혀
  있었다. `default_channel` 은 `U0123456789` 로 멀쩡히 설정돼 있었다 — **증상을
  보고 원인을 짐작해 적어둔 것이 3일간 진짜 원인을 가렸다.**

- **수정**:
  1. `notify.py` — `getattr(resp, "data", resp)` 로 payload 를 꺼내고, dict 가
     아니면 경고 후 `{}` 를 돌려준다(업로드 자체는 이미 됐으므로 파일 id 없이 진행).
  2. `tests/test_notify.py` — 가짜를 `FakeUploadResponse` 로 바꿨다. `.data` 를 갖고,
     `__iter__` 는 **일부러 TypeError 를 낸다.** 누가 다시 `dict(resp)` 를 쓰면
     테스트에서 터진다.

- **방어**: 위 2번. 다만 **`files_upload_v2` 를 실제로 부르는 자동 테스트는 없다**
  (네트워크 금지). 가짜의 모양을 실물에 맞춰 두는 것이 유일한 방어선이다.

- **교훈**: **가짜는 실물보다 착하면 안 된다.** 페이크를 만들 때 "우리가 쓰는 필드만
  있으면 된다"고 줄이면, 실물의 **타입**이 다르다는 사실이 통째로 사라진다.
  페이크는 인터페이스를 흉내내는 물건이지 편의 자료구조가 아니다.

  그리고 — **"발송 대기" 같은 추측을 문서에 사실처럼 적지 말 것.** 확인 안 한 원인은
  "미확인" 이라고 쓴다. 그럴듯한 오진이 진짜 원인을 덮는다.
