"""Life Trainer 웹 플래너 (계약서 §4).

폰에서 계획을 넣고 격자를 보정하는 화면. `create_app(cfg) -> Flask` 팩토리 하나만
공개 인터페이스로 삼는다 (테스트가 이걸로 `test_client()` 를 만든다).
"""

from __future__ import annotations

from lifetrainer.web.app import create_app

__all__ = ["create_app"]
