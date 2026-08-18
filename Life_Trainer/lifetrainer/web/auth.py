"""원타임 서명 링크 + 세션 쿠키 (계약서 §4, contracts-v2.md).

Slack 처럼 "링크를 클릭하면 로그인된다" 흐름을 만든다. HMAC-SHA256 으로 서명한
토큰을 두 가지 종류로 다룬다 (페이로드의 ``k`` 필드로 엄격히 구분한다 — 그러지
않으면 가로챈 링크 토큰을 그대로 쿠키 값으로 붙여넣어 1회용 방어를 우회할 수 있다):

  - **링크 토큰** (``k='link'``): `signed_planner_url` 이 만드는 URL 의 ``t=`` 값.
    TTL 이 짧고(`cfg.web.link_ttl_sec`, 기본 600초) **1회용**이다. 서명 링크는 URL
    에 그대로 남아 브라우저 히스토리·리퍼러로 새어나갈 수 있어, TTL + 1회용 소비가
    이중 방어선이다.
  - **세션 쿠키** (``k='session'``): `/auth/enter` 가 링크 토큰과 교환해 발급한다.
    TTL 이 길고(`cfg.web.session_ttl_sec`) 재사용 가능하다.

서명 비교는 전부 `hmac.compare_digest` 를 쓴다 — 문자열 `==` 비교는 타이밍
공격에 열려 있다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from lifetrainer import db

logger = logging.getLogger(__name__)

# 쿠키 이름. web/app.py 가 request.cookies 조회/Response.set_cookie 에 그대로 쓴다.
SESSION_COOKIE_NAME = "lt_session"

_WEBSECRET_FILENAME = "websecret"


class AuthError(Exception):
    """서명 불일치·형식 오류·만료·재사용 등 인증 실패를 통틀어 나타낸다."""


# ── 비밀키 ────────────────────────────────────────────────────────────────


def ensure_session_secret(cfg: Any) -> str:
    """서명에 쓸 비밀키를 돌려준다.

    `cfg.web.session_secret` 이 비어 있지 않으면 그대로 쓴다. 비어 있으면
    `<data_dir>/websecret` 을 읽고, 파일도 없으면 새로 만든다(무작위 32바이트,
    0600 권한). 설정 로더(`config.py`)는 필드를 빈 문자열로 둔 채 로드를 끝내므로
    — 파일시스템에 손대는 이 로직은 V3(web/auth.py) 담당이다.
    """
    if cfg.web.session_secret:
        return cfg.web.session_secret

    path = Path(cfg.data_dir) / _WEBSECRET_FILENAME
    if path.exists():
        secret = path.read_text(encoding="utf-8").strip()
        if secret:
            return secret

    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(32)
    # os.open 의 mode 인자는 프로세스 umask 의 영향을 받을 수 있어, 생성 직후
    # chmod 로 0600 을 다시 확정한다.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(secret)
    os.chmod(path, 0o600)
    logger.info("새 세션 비밀키를 생성했습니다: %s", path)
    return secret


# ── 토큰 인코딩/디코딩 (HMAC-SHA256, base64url) ───────────────────────────


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def _sign(secret: str, payload_b64: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64e(mac)


def _encode(cfg: Any, payload: dict[str, Any]) -> str:
    secret = ensure_session_secret(cfg)
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    payload_b64 = _b64e(body.encode("utf-8"))
    return f"{payload_b64}.{_sign(secret, payload_b64)}"


def _decode(cfg: Any, token: str, *, expected_kind: str, now: float) -> dict[str, Any]:
    """서명 → 형식 → 종류 → 만료 순서로 검증하고 페이로드를 돌려준다. 실패 시 AuthError."""
    secret = ensure_session_secret(cfg)
    try:
        payload_b64, sig_b64 = (token or "").split(".", 1)
    except ValueError:
        raise AuthError("토큰 형식이 올바르지 않습니다") from None

    # 서명을 먼저 검증한다 — 위조된 토큰의 페이로드는 애초에 신뢰하지 않는다.
    if not hmac.compare_digest(_sign(secret, payload_b64), sig_b64):
        raise AuthError("서명이 유효하지 않습니다 (위조되었거나 다른 비밀키로 만들어짐)")

    try:
        payload = json.loads(_b64d(payload_b64))
    except Exception as exc:  # noqa: BLE001 - 어떤 형태로 깨지든 AuthError 로 통일한다
        raise AuthError("토큰 페이로드를 해석할 수 없습니다") from exc

    if not isinstance(payload, dict) or payload.get("k") != expected_kind:
        raise AuthError(f"기대한 토큰 종류가 아닙니다: {expected_kind!r}")

    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or now >= exp:
        raise AuthError("토큰이 만료되었습니다")

    return payload


# ── 링크 토큰 (1회용, TTL 짧음) ──────────────────────────────────────────────


def make_link_token(cfg: Any, user_id: str, *, now: float | None = None) -> str:
    """서명된 원타임 링크 토큰을 만든다. TTL = `cfg.web.link_ttl_sec`."""
    ts = now if now is not None else time.time()
    payload = {
        "k": "link",
        "u": user_id,
        "jti": secrets.token_urlsafe(9),  # 1회용 판정 키 (sync_state 에 기록됨)
        "exp": ts + cfg.web.link_ttl_sec,
    }
    return _encode(cfg, payload)


def _verify_link_payload(cfg: Any, token: str, *, now: float | None = None) -> dict[str, Any]:
    ts = now if now is not None else time.time()
    payload = _decode(cfg, token, expected_kind="link", now=ts)
    user_id = payload.get("u")
    jti = payload.get("jti")
    if not isinstance(user_id, str) or not user_id or not isinstance(jti, str) or not jti:
        raise AuthError("토큰 페이로드가 올바르지 않습니다")
    return payload


def verify_link_token(cfg: Any, token: str, *, now: float | None = None) -> str:
    """링크 토큰의 서명·만료·종류를 검증하고 user_id 를 돌려준다. 실패 시 AuthError.

    **1회용(재사용 금지) 검사는 여기서 하지 않는다** — DB 연결이 필요해 이 함수의
    계약 시그니처(``conn`` 인자 없음)로는 표현할 수 없다. 실제 로그인 경로
    (`/auth/enter`)는 이 함수 대신 `consume_link_token(conn, cfg, token)` 을 써서
    재사용까지 막는다.
    """
    payload = _verify_link_payload(cfg, token, now=now)
    return str(payload["u"])


def consume_link_token(conn: Any, cfg: Any, token: str, *, now: float | None = None) -> str:
    """링크 토큰을 검증하고 1회용으로 소비한다. 이미 쓴 토큰이면 AuthError.

    사용한 토큰의 jti 를 `sync_state`(``auth_jti:<jti>``)에 남겨 재사용을 막는다.
    서명·만료 검증에 먼저 실패한 토큰은 상태를 남기지 않는다 — 그러지 않으면
    무작위 문자열을 던져보는 것만으로 sync_state 가 부풀어 오를 수 있다.
    """
    payload = _verify_link_payload(cfg, token, now=now)
    jti = str(payload["jti"])
    state_key = f"auth_jti:{jti}"
    if db.get_state(conn, state_key) is not None:
        raise AuthError("이미 사용된 링크입니다 (서명 링크는 1회용입니다)")
    # 값 자체는 쓰지 않지만, 나중에 만료된 jti 를 청소하는 배치를 만들 때 쓸 수
    # 있도록 만료 시각을 남겨 둔다.
    db.set_state(conn, state_key, repr(payload["exp"]))
    return str(payload["u"])


# ── 세션 쿠키 (재사용 가능, TTL 김) ───────────────────────────────────────────


def make_session_cookie(cfg: Any, user_id: str) -> str:
    """세션 쿠키 값을 만든다. TTL = `cfg.web.session_ttl_sec`."""
    payload = {"k": "session", "u": user_id, "exp": time.time() + cfg.web.session_ttl_sec}
    return _encode(cfg, payload)


def verify_session_cookie(cfg: Any, raw: str) -> str | None:
    """세션 쿠키를 검증한다. 실패하면 예외 대신 None (매 요청 `before_request` 에서 쓰기 위함)."""
    if not raw:
        return None
    try:
        payload = _decode(cfg, raw, expected_kind="session", now=time.time())
    except AuthError:
        return None
    user_id = payload.get("u")
    return user_id if isinstance(user_id, str) and user_id else None


def session_cookie_kwargs(cfg: Any) -> dict[str, Any]:
    """Flask `Response.set_cookie(**kwargs)` 에 그대로 넘길 옵션.

    `HttpOnly`/`SameSite=Lax` 는 항상 걸고, `Secure` 는 `cfg.web.external` 일 때만
    건다 (로컬 http 개발 환경에서는 Secure 쿠키가 브라우저에 아예 저장되지 않는다).
    """
    return {
        "max_age": cfg.web.session_ttl_sec,
        "httponly": True,
        "samesite": "Lax",
        "secure": bool(cfg.web.external),
        "path": "/",
    }


# ── Slack 등에서 쓸 링크 빌더 ────────────────────────────────────────────────


def signed_planner_url(cfg: Any, user_id: str, day: str) -> str | None:
    """`/auth/enter` 로 바로 로그인되는 서명 링크. `cfg.web.base_url` 이 없으면 None."""
    base = (cfg.web.base_url or "").rstrip("/")
    if not base:
        return None
    token = make_link_token(cfg, user_id)
    return (
        f"{base}/auth/enter?u={quote(user_id, safe='')}"
        f"&t={quote(token, safe='')}&d={quote(day, safe='')}"
    )
