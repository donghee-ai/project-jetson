"""lifetrainer.collect.dedupe 테스트. 네트워크 없이, tmp_path 의 임시 SQLite 로 돈다."""

from __future__ import annotations

import time

import pytest

from lifetrainer import db
from lifetrainer.collect import dedupe


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "lt.db")
    db.init_db(c)
    yield c
    c.close()


def _insert_doc(conn, *, url: str, simhash: int, fetched_at: float) -> int:
    """`doc` 테이블에 최소 필드만 채워 테스트용 문서를 넣는다."""
    cur = conn.execute(
        "INSERT INTO doc(kind, url, title, fetched_at, content_hash, simhash, state) "
        "VALUES ('article', ?, 'title', ?, 'hash', ?, 'new')",
        (url, fetched_at, simhash),
    )
    return cur.lastrowid


# ── sha256_hex ────────────────────────────────────────────────────────────


def test_sha256_hex_matches_known_vector():
    # sha256("") 의 잘 알려진 값
    assert dedupe.sha256_hex("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_sha256_hex_str_and_bytes_agree():
    assert dedupe.sha256_hex("hello world") == dedupe.sha256_hex(b"hello world")


def test_sha256_hex_different_input_different_hash():
    assert dedupe.sha256_hex("a") != dedupe.sha256_hex("b")


# ── simhash64 / hamming ────────────────────────────────────────────────────


def test_simhash64_identical_text_matches_exactly():
    text = "On-device inference with llama.cpp on Jetson Orin NX is surprisingly fast"
    a = dedupe.simhash64(text)
    b = dedupe.simhash64(text)
    assert a == b
    assert dedupe.hamming(a, b) == 0


def test_simhash64_one_word_change_has_small_hamming_distance():
    base = (
        "NVIDIA Jetson Orin NX is a compact edge AI board for running quantized "
        "large language models locally without cloud dependency"
    )
    changed = (
        "NVIDIA Jetson Orin NX is a compact edge AI board for running quantized "
        "small language models locally without cloud dependency"
    )
    a = dedupe.simhash64(base)
    b = dedupe.simhash64(changed)
    dist = dedupe.hamming(a, b)
    # 20 단어 중 1단어만 바뀐 정도 -> 완전히 다른 텍스트보다 훨씬 가까워야 한다
    assert dist <= 12


def test_simhash64_unrelated_text_has_large_hamming_distance():
    a_text = (
        "NVIDIA Jetson Orin NX is a compact edge AI board for running quantized "
        "large language models locally without cloud dependency"
    )
    b_text = (
        "The recipe calls for two cups of flour, a pinch of salt, and enough "
        "warm water to form a soft dough before letting it rest overnight"
    )
    a = dedupe.simhash64(a_text)
    b = dedupe.simhash64(b_text)
    assert dedupe.hamming(a, b) > 20


def test_hamming_self_is_zero():
    assert dedupe.hamming(12345, 12345) == 0


def test_hamming_all_bits_differ():
    assert dedupe.hamming(0, (1 << 64) - 1) == 64


def test_simhash64_empty_text_is_zero():
    assert dedupe.simhash64("") == 0


# ── is_near_duplicate ───────────────────────────────────────────────────


def test_is_near_duplicate_finds_matching_recent_doc(conn):
    now = time.time()
    simhash = dedupe.simhash64("근사 중복 판정을 위한 예시 텍스트입니다")
    doc_id = _insert_doc(conn, url="https://example.com/a", simhash=simhash, fetched_at=now)

    found = dedupe.is_near_duplicate(conn, simhash, threshold=3, within_days=14)
    assert found == doc_id


def test_is_near_duplicate_no_match_returns_none(conn):
    now = time.time()
    _insert_doc(
        conn, url="https://example.com/a", simhash=dedupe.simhash64("완전히 다른 내용의 문서"), fetched_at=now
    )

    unrelated = dedupe.simhash64(
        "The recipe calls for two cups of flour, a pinch of salt, and warm water"
    )
    found = dedupe.is_near_duplicate(conn, unrelated, threshold=3, within_days=14)
    assert found is None


def test_is_near_duplicate_ignores_docs_outside_window(conn):
    old_ts = time.time() - 30 * 86400  # 30일 전 (within_days=14 밖)
    simhash = dedupe.simhash64("이 텍스트는 오래된 문서와 동일합니다")
    _insert_doc(conn, url="https://example.com/old", simhash=simhash, fetched_at=old_ts)

    found = dedupe.is_near_duplicate(conn, simhash, threshold=3, within_days=14)
    assert found is None


def test_is_near_duplicate_within_window_boundary_included(conn):
    recent_ts = time.time() - 1 * 86400  # 1일 전, within_days=14 안
    simhash = dedupe.simhash64("최근 문서와 동일한 내용의 텍스트입니다")
    doc_id = _insert_doc(conn, url="https://example.com/recent", simhash=simhash, fetched_at=recent_ts)

    found = dedupe.is_near_duplicate(conn, simhash, threshold=3, within_days=14)
    assert found == doc_id
