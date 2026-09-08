"""테스트 전역 격리 — 스위트가 이 기기의 운영 설정을 읽지 않게 한다.

## 왜 필요한가 (`docs/issues/0003`)

`config/lifetrainer.toml` 의 `[search] serper_api_key` 를 **비우자 테스트가 깨졌다.**
코드는 한 줄도 안 고친 상태였다. 픽스처 23개가 `load_config()` 로 운영 설정을 읽고
`db_path`·`data_dir` 만 tmp 로 갈아끼우고 있었기 때문이다.

같은 일을 2026-08-21 에도 겪었다 — 그때는 키를 *넣자* `test_websearch.py` 의 테스트
3개가 깨졌고, 그 파일에만 `_with_keys` 처방을 넣었다. `test_cli.py` 도 자기 toml 을
따로 쓰며 같은 문제를 피하고 있었다. **처방이 두 곳에 흩어져 있었고 나머지 21개는
무방비였다.** 여기서 한 번에 막는다.

## 무엇을 막나

1. **설정 파일** — `$LT_CONFIG` 를 `lifetrainer.example.toml` 사본으로 고정한다
2. **Slack 토큰 폴백** — `bot_token` 이 비면 `openclaw_config` 의 JSON 에서 읽는다.
   그 경로가 이 기기의 진짜 `~/.openclaw/openclaw.json` 이므로 **없는 경로로 돌린다**
3. **환경변수** — `LT_<SECTION>_<KEY>` 가 설정을 덮으므로 전부 지운다

바깥 접근이 걸린 값(`serper_api_key`·`bot_token`)은 이제 전부 비어 있다.
**그 값이 필요한 테스트는 스스로 넣는다** (`test_websearch.py::_with_keys` 참고).

## 그리고 소켓을 막는다

설정을 격리해도 **테스트가 키를 직접 넣으면 진짜 요청이 나간다.** 실제로 나갔다 —
2026-08-27 23:10, 이 결함을 진단하려고 운영 키를 잠시 되돌린 채 누수 재현 테스트를
돌렸고, `어제 했지 오늘은?` 이 진짜 Serper 로 갔다. 그 프로세스는 `setup_logging` 을
안 타서 **로컬 로그에도 안 남았다** (`HISTORY/2026-08-27-the-suite-said-it-made-no-network-calls.md`).

`test_websearch.py` 의 첫 줄은 그때도 *"네트워크 호출 없음"* 이라고 적혀 있었다.
주석은 약속이지 강제가 아니다. **루프백 밖 연결을 아예 실패시킨다.**
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import socket
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE = _ROOT / "config" / "lifetrainer.example.toml"


def _build_hermetic_config() -> Path:
    text = _EXAMPLE.read_text(encoding="utf-8")
    # ★ 이 한 줄이 없으면 이 기기의 **진짜 Slack 봇 토큰**이 테스트 설정에 들어온다.
    text = re.sub(
        r"^openclaw_config\s*=.*$",
        'openclaw_config = "/nonexistent/openclaw-for-tests.json"',
        text,
        flags=re.MULTILINE,
    )
    tmpdir = Path(tempfile.mkdtemp(prefix="lt-test-config-"))
    atexit.register(shutil.rmtree, tmpdir, True)
    path = tmpdir / "lifetrainer.toml"
    path.write_text(text, encoding="utf-8")
    return path


# ★ 모듈 수준에서 실행한다. `conftest.py` 는 테스트 모듈 import 보다 먼저 읽히므로,
#   모듈 최상단에서 `load_config()` 를 부르는 파일까지 덮인다.
for _name in [k for k in os.environ if k.startswith("LT_")]:
    del os.environ[_name]
os.environ["LT_CONFIG"] = str(_build_hermetic_config())


# ── 루프백 밖으로 나가는 연결을 막는다 ──────────────────────────────────
#
# 설정 격리만으로는 부족하다. 키를 직접 넣는 테스트(`_with_keys`)가 `_serper` 를
# 안 막으면 진짜 요청이 나간다. 주석으로 "네트워크 호출 없음" 이라고 적어 둔 파일에서
# 실제로 개인 발화가 나간 적이 있다. 약속을 강제로 바꾼다.
_orig_connect = socket.socket.connect


def _no_outbound(self, address):  # noqa: ANN001
    host = address[0] if isinstance(address, tuple) else str(address)
    if not (str(host).startswith("127.") or str(host) in ("::1", "localhost", "")):
        raise AssertionError(
            f"테스트가 바깥으로 연결하려 했다: {address}. "
            "네트워크가 필요하면 그 지점을 monkeypatch 로 막아라."
        )
    return _orig_connect(self, address)


socket.socket.connect = _no_outbound
