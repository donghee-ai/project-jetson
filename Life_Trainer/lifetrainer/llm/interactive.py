"""대화 우선권 — 야간 배치가 도는 중에도 사람 질문이 먼저 간다.

llama-server 는 `--parallel 1` 이라 요청을 한 줄로 세운다. 워커가 요약 1건(약 20초)을
집어 든 순간 Slack 질문은 그 뒤에 선다 — 평소 2초짜리가 22초가 된다.
`data/gpu.lock` 은 **워커끼리만** 막고 대화 경로는 llama-server 를 직접 치므로,
그 층에서는 이 문제를 못 막는다 (`docs/rag-plan.md §4`).

그래서 대화 턴 동안 `data/interactive.lock` 에 **공유 락**을 잡고, 워커는 잡을
claim 하기 전에 같은 파일에 배타 락을 시험해 본다. 잡히면 아무도 대화 중이 아니고,
안 잡히면 이번 바퀴는 GPU 잡을 건너뛴다. 워커는 한 번에 한 건만 처리하므로
이것만으로 최악 대기가 20초 → 0초가 된다.

**공유 락인 이유**: 동시에 두 채널에서 대화해도 서로를 막으면 안 된다.
배타 락으로 만들면 대화끼리 직렬화된다.

새 개념을 도입하지 않았다 — `worker.acquire_gpu_lock` 과 같은 `fcntl.flock` 관용구다.
"""

from __future__ import annotations

import fcntl
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from lifetrainer.config import Config

logger = logging.getLogger(__name__)

LOCK_NAME = "interactive.lock"


def lock_path(cfg: "Config") -> Path:
    return Path(cfg.data_dir) / LOCK_NAME


@contextmanager
def interactive_turn(cfg: "Config") -> Iterator[bool]:
    """대화 한 턴 동안 공유 락을 쥔다. 락을 잡았는지를 yield 한다.

    **락 획득 실패는 절대 대화를 막지 않는다.** 우선권은 편의 기능이고, 여기서
    예외를 올리면 사용자가 답을 못 받는다 — 조용히 락 없이 진행한다.
    """
    path = lock_path(cfg)
    fh = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
    except OSError as exc:  # noqa: BLE001 - 우선권 때문에 대화가 죽으면 안 된다
        logger.debug("대화 우선권 락을 잡지 못했습니다 (무시하고 진행): %s", exc)
        if fh is not None:
            fh.close()
            fh = None

    try:
        yield fh is not None
    finally:
        if fh is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()


def interactive_busy(cfg: "Config") -> bool:
    """지금 대화 턴이 진행 중인가 (= GPU 잡을 미뤄야 하는가).

    배타 락을 비차단으로 시험해 보고 즉시 놓는다. 잡히면 아무도 공유 락을 안 쥐고
    있다는 뜻이다. **파일을 못 열면 `False`** — 락 파일 문제로 배치가 영영 멈추는
    쪽이 대화가 20초 밀리는 것보다 나쁘다.
    """
    path = lock_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+")
    except OSError as exc:  # noqa: BLE001
        logger.debug("대화 우선권 락 파일을 열지 못했습니다 (배치 계속): %s", exc)
        return False

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True  # 누군가 공유 락을 쥐고 있다 = 대화 중
    else:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        fh.close()
