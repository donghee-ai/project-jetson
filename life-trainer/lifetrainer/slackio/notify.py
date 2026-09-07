"""Slack 발송 계층.

핵심 원칙(계약서 §6, 조사 문서 §5·§7·§9):
  - 토큰이 없으면 조용히 no-op 한다. 설치 직후 토큰 없이 CLI 를 돌려도 죽으면 안 된다.
  - 파일 업로드는 `files_upload_v2` 만 쓴다 (v1 `files.upload` 는 2025-11-12 완전 폐기).
  - 이미지 블록은 업로드한 파일의 `slack_file: {"id": ...}` 로 참조한다 (공개 URL 불필요).
  - 429 는 `Retry-After` 를 보고 최대 3회 재시도.
  - 운영 알림은 여기 없다. **`alert()` 를 2026-09-01 에 지웠다** — 만들어만 두고
    호출자가 한 번도 없었고, 쿨다운으로 중복을 늦추는 모델이라 "이미 아는 문제를
    매번 다시" 쪽이었다. 상태 알림은 `operate/tools/daily-check.sh` 가 한다 —
    **상태가 바뀔 때만** 보내고 전이 7가지 자기시험을 갖는다.
    `alert_state` 테이블은 스키마에 남겨 뒀다(0행). 지우려면 마이그레이션이 필요한데
    빈 테이블 하나가 주는 해는 없고, 옛 DB 와 새 DB 가 갈리는 쪽이 더 나쁘다.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from lifetrainer import timeutil
from lifetrainer.config import Config
from lifetrainer.db import transaction
from lifetrainer.slackio.blocks import image_block

logger = logging.getLogger(__name__)

# 업로드한 파일을 이미지 블록으로 참조했을 때 Slack 이 `invalid slack file` 을
# 돌려주는 경합의 대기 간격(초). 합계 3.5초 — 사용자가 카드를 기다리는 시간이므로
# 길게 잡지 않는다. 실패는 3일에 7번이라 이 지연을 무는 경우 자체가 드물다.
_IMAGE_RACE_BACKOFF_SEC = (0.5, 1.0, 2.0)


def _is_invalid_blocks(exc: Exception) -> bool:
    """Slack 이 블록 자체를 거부한 오류인지.

    `_call_with_retry` 가 원본 `SlackApiError` 를 `SlackError(str(exc))` 로 감싸므로
    구조화된 응답은 `__cause__` 에만 남는다. 그쪽을 먼저 보고(정확), 없으면 문구로
    판단한다(차선) — 문자열 매칭만 두면 SDK 가 메시지 형식을 바꿀 때 조용히 깨진다.
    """
    cause = getattr(exc, "__cause__", None)
    data = getattr(getattr(cause, "response", None), "data", None)
    if isinstance(data, dict) and str(data.get("error", "")) == "invalid_blocks":
        return True
    text = str(exc).lower()
    return "invalid_blocks" in text or "invalid slack file" in text


def _blocks_without_slack_file_images(blocks: list[dict] | None) -> list[dict] | None:
    """업로드 파일을 참조하는 image 블록만 걷어낸 목록. 뺄 게 없으면 None.

    None 을 돌려주는 것은 "재시도해도 결과가 같다"는 뜻이라 호출부가 원래 예외를
    그대로 올리게 한다 — 블록과 무관한 `invalid_blocks` 를 조용히 삼키면 안 된다.
    """
    if not blocks:
        return None
    kept = [b for b in blocks if not (b.get("type") == "image" and b.get("slack_file"))]
    return kept if len(kept) != len(blocks) else None


class SlackError(Exception):
    """SlackApiError 를 감싼 자체 예외. 재시도를 다 써도 실패했을 때 올린다."""


class SlackNotifier:
    """봇 토큰 기반 발송기.

    테스트에서는 `notifier.client = FakeWebClient()` 처럼 생성 후 `client`
    속성을 직접 교체해 주입한다 (생성자 시그니처는 계약서대로 `cfg` 하나만 받는다).
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        # 토큰이 비어 있으면 클라이언트를 아예 만들지 않는다 — enabled=False 로 이어진다.
        self.client: WebClient | None = WebClient(token=cfg.slack.bot_token) if cfg.slack.bot_token else None
        self._channel_cache: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        """봇 토큰이 있고 클라이언트가 준비돼 있어야 True."""
        return bool(self._cfg.slack.bot_token) and self.client is not None

    # ── 채널 해석 ────────────────────────────────────────────────────

    def resolve_channel(self, channel: str | None = None) -> str:
        """'U…' 사용자 ID 는 `conversations.open` 으로 DM 채널('D…')로 바꿔 캐시한다.

        'C…'/'D…' 는 그대로 반환. 빈 값이면 `cfg.slack.default_channel` 로 대체한다.
        """
        ch = channel or self._cfg.slack.default_channel
        if not ch:
            return ""
        if not ch.startswith("U"):
            return ch
        if ch in self._channel_cache:
            return self._channel_cache[ch]
        if not self.enabled:
            logger.warning("Slack 비활성화(토큰 없음) — 사용자 ID(%s) 를 채널로 변환하지 못함", ch)
            return ch
        resp = self._call_with_retry(self.client.conversations_open, users=ch)
        dm_channel = str(resp["channel"]["id"])
        self._channel_cache[ch] = dm_channel
        return dm_channel

    # ── 재시도 래퍼 ──────────────────────────────────────────────────

    def _call_with_retry(self, func, *, max_retries: int = 3, **kwargs) -> Any:
        """SlackApiError 를 잡아 429 는 Retry-After 만큼 대기 후 최대 max_retries 회 재시도.

        그 외 오류거나 재시도를 다 썼으면 `SlackError` 로 감싸 올린다.
        """
        last_exc: SlackApiError | None = None
        for attempt in range(max_retries + 1):
            try:
                return func(**kwargs)
            except SlackApiError as exc:
                last_exc = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status == 429 and attempt < max_retries:
                    retry_after = self._retry_after_sec(exc)
                    logger.warning(
                        "Slack 429 레이트리밋, %.1f초 후 재시도 (%d/%d)", retry_after, attempt + 1, max_retries
                    )
                    time.sleep(retry_after)
                    continue
                logger.warning("Slack API 호출 실패(%s): %s", getattr(exc, "response", None), exc)
                raise SlackError(str(exc)) from exc
        raise SlackError(str(last_exc)) from last_exc

    @staticmethod
    def _retry_after_sec(exc: SlackApiError) -> float:
        headers = getattr(getattr(exc, "response", None), "headers", None) or {}
        try:
            value = float(headers.get("Retry-After", 1))
        except (TypeError, ValueError):
            value = 1.0
        return max(0.0, min(value, 30.0))

    # ── 발송 원자 동작 ───────────────────────────────────────────────

    def post(
        self,
        text: str,
        *,
        blocks: list[dict] | None = None,
        channel: str | None = None,
        thread_ts: str | None = None,
    ) -> str:
        """`chat.postMessage`. 비활성 상태면 경고 로그 후 빈 문자열을 반환한다."""
        if not self.enabled:
            logger.warning("Slack 비활성화(토큰 없음) — 발송 생략: %s", text[:80])
            return ""
        target = self.resolve_channel(channel)
        if not target:
            logger.warning("Slack 채널이 지정되지 않아 발송 생략")
            return ""
        try:
            resp = self._call_with_retry(
                self.client.chat_postMessage,
                channel=target,
                text=text,
                blocks=blocks,
                thread_ts=thread_ts,
                # ★ 링크 미리보기를 끈다. 다이제스트가 링크 5개를 싣는데 Slack 이 각각을
                #   본문 수백 자짜리 카드로 펼쳐서 **화면의 절반 이상**을 먹었다.
                #   우리가 이미 제목·요약·출처를 블록에 담으므로 미리보기는 중복이다.
                unfurl_links=False,
                unfurl_media=False,
            )
        except SlackError as exc:
            retry_blocks = _blocks_without_slack_file_images(blocks) if _is_invalid_blocks(exc) else None
            if retry_blocks is None:
                raise
            resp = self._post_retrying_image_race(
                text, blocks=blocks, fallback_blocks=retry_blocks,
                target=target, thread_ts=thread_ts, first_exc=exc,
            )
        return str(resp.get("ts", ""))

    def _post_retrying_image_race(
        self,
        text: str,
        *,
        blocks: list[dict],
        fallback_blocks: list[dict],
        target: str,
        thread_ts: str | None,
        first_exc: Exception,
    ):
        """`invalid_blocks` 로 거부된 이미지 카드를 **그림을 지키면서** 다시 올린다.

        ## 무엇이 일어나는가 (2026-08-24 실측)

        `files_upload_v2` 직후 그 파일을 `slack_file` 로 참조하면 Slack 이 가끔
        `invalid slack file` 로 거부한다. 3일에 7번 났고, 그때마다 사용자에게는
        **사진만 빠진 카드**가 갔다. 사용자는 `/view` 를 한 번 더 쳤다 — 화면에
        같은 카드가 두 장 남은 것이 그 흔적이다.

        ## 왜 기다렸다 다시 하나

        원인을 우리 쪽에서 없앨 수 없다. 업로드는 성공했고 파일도 정상이다
        (`files.info` 가 `mode=hosted` 로 돌려준다). **썸네일 생성이 게이트일
        것으로 보고 쟀지만 아니었다** — 썸네일이 없는 상태에서도 통과했다(2/2).
        슬랙 내부의 짧은 반영 지연이고, 붙어 있는 것은 시간뿐이다.

        그래서 **이미지를 뺀 카드로 곧장 강등하지 않는다.** 이 저장소의 규칙이기도
        하다 — 조용히 다르게 동작하는 것이 가장 나쁘다. 세 번(합계 3.5초) 다시
        시도하고, 그래도 안 되면 그때 이미지를 뺀다.
        """
        for wait in _IMAGE_RACE_BACKOFF_SEC:
            time.sleep(wait)
            try:
                resp = self._call_with_retry(
                    self.client.chat_postMessage,
                    channel=target,
                    text=text,
                    blocks=blocks,
                    thread_ts=thread_ts,
                    unfurl_links=False,
                    unfurl_media=False,
                )
            except SlackError as exc:
                if not _is_invalid_blocks(exc):
                    raise
                continue
            logger.info("이미지 블록이 %.1f초 기다린 뒤 통과했습니다", wait)
            return resp

        # 여기까지 왔으면 경합이 아니라 정말 못 싣는 파일이다. 카드를 통째로
        # 잃는 것보다는 이미지만 빼는 편이 낫다.
        logger.warning(
            "이미지 블록이 %.1f초 재시도 뒤에도 거부돼(%s) 이미지 없이 게시합니다",
            sum(_IMAGE_RACE_BACKOFF_SEC), first_exc,
        )
        return self._call_with_retry(
            self.client.chat_postMessage,
            channel=target,
            text=text,
            blocks=fallback_blocks,
            unfurl_links=False,
            unfurl_media=False,
            thread_ts=thread_ts,
        )

    def upload_png(
        self,
        path: str | Path,
        *,
        title: str,
        initial_comment: str | None = None,
        channel: str | None = None,
        thread_ts: str | None = None,
        share: bool = True,
    ) -> dict:
        """`files_upload_v2` 로 PNG 를 올린다. 비활성/파일 없음이면 빈 dict.

        ## ★ `share=False` 를 언제 쓰나 (2026-08-19)

        `files_upload_v2` 에 `channel` 을 주면 **슬랙이 그 파일을 채널에 하나의
        메시지로 게시한다.** 그 뒤 같은 file id 를 `slack_file` 이미지 블록에 넣어
        카드를 또 올리면 **같은 그림이 두 번 뜬다.** 실제로 사용자가 두 번 봤다.

        슬랙이 문서에서 권하는 순서는 "업로드해서 file id 를 받고 → 그 id 를
        image 블록으로 참조" 이고, 업로드 시 채널 공유는 **선택**이다.
        그래서 블록에 실을 목적이면 `share=False` 로 올린다 — 채널에 파일 메시지가
        따로 생기지 않고 카드 하나만 남는다.
        """
        if not self.enabled:
            logger.warning("Slack 비활성화(토큰 없음) — PNG 업로드 생략: %s", path)
            return {}
        p = Path(path)
        if not p.exists():
            logger.warning("PNG 파일이 존재하지 않아 업로드 생략: %s", p)
            return {}

        kwargs: dict = {"file": str(p), "filename": p.name, "title": title}
        if share:
            target = self.resolve_channel(channel)
            if not target:
                logger.warning("Slack 채널이 지정되지 않아 PNG 업로드 생략")
                return {}
            kwargs["channel"] = target
            kwargs["initial_comment"] = initial_comment
            kwargs["thread_ts"] = thread_ts
        elif initial_comment or thread_ts:
            # 공유하지 않는 업로드에 코멘트/스레드를 주면 슬랙이 무시한다.
            # 조용히 사라지는 대신 호출부 실수를 로그로 드러낸다.
            logger.warning("share=False 업로드에는 initial_comment/thread_ts 가 적용되지 않습니다")

        resp = self._call_with_retry(self.client.files_upload_v2, **kwargs)
        # ★ `dict(resp)` 를 하면 안 된다. slack_sdk 는 dict 가 아니라 `SlackResponse`
        # 를 돌려주고, 그걸 dict() 로 감싸면 `ValueError: dictionary update sequence
        # element #0 has length 1` 로 터진다 — **업로드는 이미 성공한 뒤에** 터지므로
        # 파일은 올라가고 사용자에게는 오류만 보인다. 실제 payload 는 `.data` 에 있다.
        data = getattr(resp, "data", resp)
        if not isinstance(data, dict):
            logger.warning(
                "PNG 업로드 응답이 dict 가 아닙니다 (%s) — 파일 id 없이 진행합니다: %r",
                type(data).__name__,
                data,
            )
            return {}
        # 봇이 대상 채널의 멤버가 아니면 업로드 자체는 성공해도 공유가 안 될 수 있다
        # (`not_in_channel`). 명확한 로그만 남기고 결과는 그대로 돌려준다.
        if data.get("ok") is False:
            logger.warning("PNG 업로드 응답이 실패를 나타냄: %s", data)
        return dict(data)

    # ── 리포트 발송 ──────────────────────────────────────────────────

    def post_report(self, conn, report: Any, *, channel: str | None = None) -> str:
        """PNG 를 먼저 업로드하고, 반환된 file id 를 이미지 블록에 끼워 카드를 한 번에 게시한다.

        순서: files_upload_v2 -> slack_file 이미지 블록 조립 -> chat.postMessage 1회
        -> report 테이블의 posted_at/slack_ts/slack_channel 갱신.
        PNG 가 없거나 업로드에 실패하면 이미지 블록 없이 게시한다.
        """
        if not self.enabled:
            logger.warning("Slack 비활성화(토큰 없음) — 리포트 발송 생략 (report_id=%s)", getattr(report, "report_id", None))
            return ""

        target = self.resolve_channel(channel)
        if not target:
            logger.warning("Slack 채널이 지정되지 않아 리포트 발송 생략")
            return ""

        blocks = list(report.blocks) if getattr(report, "blocks", None) else []
        png_path = getattr(report, "png_path", None)
        if png_path:
            try:
                # share=False — 이 파일은 아래 이미지 블록으로 실린다. 채널에도
                # 공유하면 같은 그림이 두 번 뜬다.
                upload = self.upload_png(
                    png_path, title=f"{report.kind} {report.day}", channel=target, share=False
                )
                file_id = (upload or {}).get("file", {}).get("id") if isinstance(upload, dict) else None
                if file_id:
                    blocks.append(
                        image_block(str(file_id), title=f"{report.kind} {report.day}", alt=f"{report.day} 타임라인")
                    )
            except SlackError as exc:
                logger.warning("PNG 업로드 실패, 이미지 없이 텍스트만 게시: %s", exc)

        # "플래너 열기" 링크 — cfg.web.base_url 이 설정돼 있을 때만 맨 끝에 붙인다.
        # (계약서 §6: base_url 미설정이면 깨진 링크를 만들지 않는다.) `cfg` 에
        # `web` 이 아예 없는 낡은 테스트/호출부도 있을 수 있어 getattr 로 방어한다.
        web_cfg = getattr(self._cfg, "web", None)
        base_url = getattr(web_cfg, "base_url", "") if web_cfg is not None else ""
        if base_url and getattr(report, "day", None):
            planner_url = f"{base_url.rstrip('/')}/d/{report.day}"
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "플래너 열기", "emoji": True},
                            "url": planner_url,
                            "action_id": "open_planner",
                        }
                    ],
                }
            )

        ts = self.post(report.text, blocks=blocks or None, channel=target)
        if ts:
            with transaction(conn):
                conn.execute(
                    "UPDATE report SET posted_at = ?, slack_ts = ?, slack_channel = ? WHERE id = ?",
                    (timeutil.now_ts(), ts, target, report.report_id),
                )
        return ts

