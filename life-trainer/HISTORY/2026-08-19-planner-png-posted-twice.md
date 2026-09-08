# 플래너 PNG 가 Slack 에 두 번 떴다

- **발견**: 사용자가 Slack 화면을 보고. 스크린샷에 같은 플래너가 두 번 떠 있었다.
  ("또 2번 뜨더라" — 처음이 아니었다.)
- **증상**: `/view` 와 일일 리포트에서 카드 아래 같은 PNG 가 두 장 붙는다.
  하나는 파일 첨부(`플래너 2026-08-19 (76kB)`), 하나는 카드 안의 이미지 블록.

## 원인 — 무엇을 사실이라고 잘못 믿었나

**"업로드는 파일을 올리는 것이고, 게시는 메시지를 보내는 것"** 이라고 믿었다.

`files_upload_v2` 에 `channel` 을 주면 **슬랙이 그 파일을 채널에 하나의 메시지로
게시한다.** 업로드가 곧 게시다. 우리는 거기에 더해 반환된 file id 를
`slack_file` 이미지 블록에 넣어 카드를 또 올렸다. 그래서 두 번이다.

```python
upload = notifier.upload_png(png, channel=target)   # ← 여기서 이미 한 번 게시됨
blocks.append(image_block(file_id))                 # ← 같은 파일이 두 번째
notifier.post(text, blocks=blocks)
```

슬랙이 문서에서 권하는 순서는 **"업로드해서 file id 를 받고 → 그 id 를 image
블록으로 참조"** 이고, 업로드 시 채널 공유는 **선택 인자**다. 우리는 선택 인자를
"어디에 올릴지 알려주는 것" 으로 읽었는데 실제로는 "여기에 게시해 달라" 였다.

## 수정

`upload_png(..., share: bool = True)` 를 추가하고, **블록에 실을 목적의 업로드는
`share=False`** 로 바꿨다 (`post_report`, `/view`). 채널에 파일 메시지가 따로
생기지 않고 카드 하나만 남는다.

기존 `share=True` 경로는 그대로 뒀다 — "파일만 던지는" 용도가 따로 있고,
`initial_comment`·`thread_ts` 는 그쪽에서만 의미가 있다(공유 없는 업로드에 주면
슬랙이 조용히 무시하므로 경고 로그를 남긴다).

## 검증 — 실제 워크스페이스에서

카드를 한 장 보내고 `files.info` 로 그 파일의 공유 상태를 확인했다:

```json
"shares": {"private": {"D0123456789": [
    {"ts": "1787118320.616189", "source": "BK_IMAGE", ...}
]}}
```

**공유가 정확히 하나이고 그 출처가 `BK_IMAGE`** — 이미지 블록으로만 붙었다는 뜻이다.
(고치기 전이면 여기에 업로드가 만든 공유가 하나 더 있었다.)
이 필드가 "두 번 떴다/한 번 떴다" 를 눈이 아니라 **데이터로** 확인해 준다.

## 방어

`tests/test_notify.py` 3건 — 가짜 WebClient 에 들어온 인자를 직접 본다:

- `test_upload_for_block_does_not_share_to_channel` — `share=False` 면 호출 인자에
  `channel` 이 **없어야** 한다
- `test_upload_with_share_still_posts_to_channel` — 기존 경로는 그대로
- `test_post_report_uploads_without_sharing` — 리포트 발송 시 업로드 1회(채널 없음)
  + `chat.postMessage` 1회 + 이미지 블록 1개

## 교훈

**부수효과가 있는 인자를 "설정" 으로 읽으면 안 된다.** `channel=` 은 목적지 정보가
아니라 **동작 지시**였다. API 인자 하나가 메시지를 만들 수 있다.

이 저장소가 슬랙 업로드에서 두 번째로 밟은 함정이다 —
[첫 번째는 `dict(resp)`](2026-08-17-upload-png-slackresponse.md)로, 그때도
"업로드는 이미 성공한 뒤" 문제가 드러났다. 업로드 경로는 **부수효과가 먼저 일어나고
결과 처리가 나중** 이라 실수가 조용히 남는다.
