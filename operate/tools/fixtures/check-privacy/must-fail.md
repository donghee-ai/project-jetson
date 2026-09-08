# 반드시 잡아야 하는 것

이 파일은 `check-privacy.sh --self-test` 의 시험지다. **개인정보 모양이 각각 걸린다.**
값은 전부 지어낸 것이지만 **모양은 실제 기록과 같다** — 그게 이 검사가 보는 것이다.

① 원격 창 제목의 호스트가 `host` 가 아니다:

    작업 파일 편집 - some-repo [SSH: workbox] - Visual Studio Code

② 게임 계정명이 `example` 이 아니다:

    app="chrome.exe", url="https://op.gg/summoners/kr/someplayer"

③ 유튜브 영상 id 가 실제 모양이다:

    https://www.youtube.com/watch?v=dQw4w9WgXcQ

④ 폰 열람 기록의 모양(모바일 사이트)인데 예시 도메인이 아니다:

    실제:  m.someforum.com · m.someportal.com

⑤ 실제 곡 메타데이터처럼 보이는 값:

    {"album":"나만의 앨범","artist":"어떤 가수","title":"어떤 곡"}

⑥ 실제 행동 집계라고 밝힌 사용량:

    실측 기준선: 유튜브 37건 · 게임 92분

⑦ 앱 사용과 실제 시각이 붙은 기록:

    20:43 유튜브 재생 시작 · unlock 23:15

⑧ 개인 홈 경로:

    /home/privateuser/project/app

⑨ 실제 환경 주소처럼 보이는 값:

    100.64.0.42 · 192.168.0.77
