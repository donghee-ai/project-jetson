"""스위트가 이 기기의 운영 설정을 읽지 않는다 (`docs/issues/0003`).

`config/lifetrainer.toml` 의 값 하나를 바꿨을 때 테스트 결과가 바뀌면, "N개 통과"는
**이 기계의 이 설정에서만** 참인 문장이 된다. 이 파일이 그 성질을 못박는다.

격리는 `tests/conftest.py` 가 건다. 여기서는 **격리가 실제로 걸렸는지**만 본다 —
운영 파일을 옮겨서 확인하면 중간에 죽었을 때 서비스가 설정을 잃는다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lifetrainer.config import load_config


def test_설정은_운영파일이_아니라_격리사본에서_온다():
    env = os.environ.get("LT_CONFIG")
    assert env, "conftest 가 LT_CONFIG 를 설정해야 한다"
    assert Path(env).is_file()
    운영파일 = Path(__file__).resolve().parent.parent / "config" / "lifetrainer.toml"
    assert Path(env).resolve() != 운영파일.resolve()


def test_바깥으로_나갈_수_있는_자격증명이_비어_있다():
    """★ 이 값들의 **유무가 분기를 가른다.** 필요한 테스트는 스스로 넣어야 한다
    (`test_websearch.py::_with_keys`). 기기 상태에 기대면 안 된다."""
    cfg = load_config()
    assert cfg.search.serper_api_key == ""
    assert cfg.slack.bot_token == "", "openclaw_config 폴백으로 진짜 토큰이 새어들어왔다"
    assert cfg.slack.app_token == ""


def test_LT_환경변수가_설정을_덮지_않는다():
    """`LT_<SECTION>_<KEY>` 가 남아 있으면 셸 환경에 따라 결과가 달라진다."""
    남은것 = [k for k in os.environ if k.startswith("LT_") and k != "LT_CONFIG"]
    assert 남은것 == [], f"conftest 가 지웠어야 한다: {남은것}"


def test_슬랙_토큰_폴백_경로가_실기기를_안_가리킨다():
    cfg = load_config()
    assert not cfg.slack.openclaw_config.exists()


def test_바깥으로_연결하면_실패한다():
    """★ 주석이 아니라 구조로 막는다.

    `test_websearch.py` 는 첫 줄에 "네트워크 호출 없음" 이라고 적어 두고도, 운영 키가
    설정에 있을 때 실제로 요청을 내보냈다 (2026-08-27 23:10 실측).
    """
    import socket

    with pytest.raises(AssertionError, match="바깥으로 연결"):
        socket.socket().connect(("8.8.8.8", 53))


def test_루프백은_막지_않는다():
    """로컬 서버를 띄우는 테스트가 있으므로 127.0.0.1 은 살려 둔다."""
    import socket

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        socket.socket().connect(srv.getsockname())
    finally:
        srv.close()
