"""OpenClaw 에이전트 결합층 — 이 패키지가 Life Trainer 를 하나의 에이전트로 만든다.

## 왜 별도 패키지인가

`llm/` 은 **우리가 만든 대화 루프**다 (`converse.py` + 규칙 게이트 `trigger.py`).
여기 `agent/` 는 **OpenClaw 가 루프를 돌 때** 우리 능력을 빌려주는 쪽이다.
루프의 주인이 다르므로 섞지 않는다:

    llm/converse.py    우리가 프롬프트를 조립하고, 규칙이 툴을 고른다   2~9초
    agent/             OpenClaw 가 프롬프트를 조립하고, 모델이 툴을 고른다

둘은 **같은 llama-server(:8080) 를 공유**하고 같은 툴 몸통(`llm/tools.REGISTRY`)을
부른다. 달라지는 것은 "누가 무엇을 부를지 정하는가" 하나뿐이다.

## 구성

    sandbox.py   경로 감옥 — 지정된 폴더 밖은 열지도 쓰지도 않는다
    slash.py     "/" 명령을 Slack 없이 실행 (에이전트의 주 경로)
    catalog.py   MCP 로 내보낼 툴 목록 — 토큰 예산까지 여기서 센다
    mcp_server.py  MCP stdio 서버 (의존성 0)
    prompt.py    워크스페이스 프롬프트 생성 (AGENTS.md)
"""
