"""Slack 발송 계층.

핵심 원칙(계약서 §6, 조사 문서 §5·§7·§9):
  - 토큰이 없으면 조용히 no-op 한다. 설치 직후 토큰 없이 CLI 를 돌려도 죽으면 안 된다.
  - 파일 업로드는 `files_upload_v2` 만 쓴다 (v1 `files.upload` 는 2025-11-12 완전 폐기).
  - 이미지 블록은 업로드한 파일의 `slack_file: {"id": ...}` 로 참조한다 (공개 URL 불필요).
  - 429 는 `Retry-After` 를 보고 최대 3회 재시도.
  - 운영 알림(`alert`)은 `alert_state` 로 쿨다운 중복 억제 — "알림 피로"가 사망 원인 1위.
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
from lifetrainer.slackio.blocks import error_blocks, image_block

logger = logging.getLogger(__name__)


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
                self.client.chat_postMessage, channel=target, text=text, blocks=blocks, thread_ts=thread_ts
            )
        except SlackError as exc:
            retry_blocks = _blocks_without_slack_file_images(blocks) if _is_invalid_blocks(exc) else None
            if retry_blocks is None:
                raise
            # 방금 올린 파일을 Slack 이 아직 처리 중이면 그 파일을 참조하는 이미지 블록이
            # `invalid_blocks` 로 거부된다. 업로드 자체는 성공해 파일은 이미 채널에
            # 보이는 상태이므로, **카드를 통째로 잃는 것보다 이미지만 빼고 올리는 편이 낫다.**
            # (실측: 같은 코드로 일일 리포트는 통과하고 /view 는 거부됐다 — 경합이다.)
            logger.warning("이미지 블록이 거부돼(%s) 이미지 없이 다시 게시합니다", exc)
            resp = self._call_with_retry(
                self.client.chat_postMessage,
                channel=target,
                text=text,
                blocks=retry_blocks,
                thread_ts=thread_ts,
            )
        return str(resp.get("ts", ""))

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

    # ── 운영 알림 (쿨다운 억제) ──────────────────────────────────────

    def alert(self, conn, key: str, title: str, detail: str, *, cooldown_sec: float = 3600) -> bool:
        """`alert_state` 로 같은 key 의 중복 알림을 억제한다.

        쿨다운 안이면 count 만 올리고 발송하지 않는다 (알림 피로 방지가 기본값).
        실제로 Slack 에 보냈으면 True, 억제됐거나 비활성 상태면 False.
        """
        now = timeutil.now_ts()
        row = conn.execute("SELECT last_sent FROM alert_state WHERE key = ?", (key,)).fetchone()
        if row is not None and (now - row["last_sent"]) < cooldown_sec:
            with transaction(conn):
                conn.execute("UPDATE alert_state SET count = count + 1 WHERE key = ?", (key,))
            logger.info("알림 쿨다운으로 억제: key=%s", key)
            return False

        sent = False
        if self.enabled:
            try:
                ts = self.post(f"{title}: {detail}", blocks=error_blocks(title, detail))
                sent = bool(ts)
            except SlackError as exc:
                logger.warning("알림 발송 실패(key=%s): %s", key, exc)
        else:
            logger.warning("Slack 비활성화(토큰 없음) — 알림 기록만 하고 발송 생략: %s", title)

        with transaction(conn):
            conn.execute(
                "INSERT INTO alert_state(key, last_sent, count) VALUES (?, ?, 1) "
                "ON CONFLICT(key) DO UPDATE SET last_sent = excluded.last_sent, count = 1",
                (key, now),
            )
        return sent
