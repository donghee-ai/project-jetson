"""중복 제거 — SHA-256 완전 중복 + 64비트 SimHash 근사 중복.

설계서 §6: "SHA-256 으로 완전 중복, SimHash 로 근사 중복(뉴스 신디케이션 대응)."
SimHash 는 표준 구현을 따른다: 토큰화 -> 토큰마다 해시 -> 비트별 가중 투표 -> 부호로 확정.
"""

from __future__ import annotations

import hashlib
import re
import time

_TOKEN_RE = re.compile(r"[0-9a-zA-Z가-힣]+")
_SIMHASH_BITS = 64
_UINT64_MASK = (1 << 64) - 1


def sha256_hex(data: bytes | str) -> str:
    """SHA-256 다이제스트를 16진 문자열로 반환한다."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _tokenize(text: str) -> list[str]:
    """소문자화한 뒤 영숫자/한글 토큰만 뽑는다."""
    return _TOKEN_RE.findall(text.lower())


def _token_hash(token: str) -> int:
    """토큰 하나를 64비트 정수로 해시한다 (SHA-256 상위 8바이트)."""
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def simhash64(text: str) -> int:
    """텍스트의 64비트 SimHash. 빈 텍스트/토큰 없음이면 0.

    SQLite `INTEGER` 는 부호 있는 64비트라 상위 비트가 선 값(2**63 이상)은
    그대로 저장할 수 없다. 그래서 결과를 부호 있는 64비트 정수(two's complement)로
    정규화해 반환한다 — DB 왕복은 물론, `hamming()` 비교에서도 문제없이 쓰인다.
    """
    tokens = _tokenize(text or "")
    if not tokens:
        return 0

    weights = [0] * _SIMHASH_BITS
    for token in tokens:
        h = _token_hash(token)
        for bit in range(_SIMHASH_BITS):
            if (h >> bit) & 1:
                weights[bit] += 1
            else:
                weights[bit] -= 1

    result = 0
    for bit in range(_SIMHASH_BITS):
        if weights[bit] > 0:
            result |= 1 << bit

    if result >= (1 << 63):
        result -= 1 << 64  # 부호 있는 64비트 범위로 접어넣는다
    return result


def hamming(a: int, b: int) -> int:
    """두 64비트 정수(부호 있는 표현이어도 무관)의 해밍 거리 (다른 비트 수)."""
    return bin((a ^ b) & _UINT64_MASK).count("1")


def is_near_duplicate(conn, simhash: int, *, threshold: int = 3, within_days: int = 14) -> int | None:
    """최근 `within_days` 안의 문서 중 해밍 거리가 threshold 이하인 것이 있으면 그 doc.id 를 반환한다.

    전수 비교는 느리므로 최근 문서만 훑는다 (설계서 §6 의도 그대로).
    """
    if simhash == 0:
        return None
    cutoff = time.time() - within_days * 86400.0
    rows = conn.execute(
        "SELECT id, simhash FROM doc WHERE simhash IS NOT NULL AND simhash != 0 AND fetched_at >= ?",
        (cutoff,),
    ).fetchall()
    for row in rows:
        if hamming(simhash, row["simhash"]) <= threshold:
            return row["id"]
    return None
