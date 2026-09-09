# 반드시 통과시켜야 하는 것

**오탐이 나오면 그 예제를 여기 추가한다.** 빡빡해지면 사람이 검사를 꺼 버리고,
그러면 검사가 없는 것과 같다 (저장소 규칙 §1).

이 저장소가 실제로 쓰는 예시값들 — 전부 통과해야 한다:

    문서 편집 - project-jetson [SSH: host] - Visual Studio Code
    app="chrome.exe", title="OP.GG", url="https://op.gg/summoners/kr/example"
    url='m.example-forum.com/best/1234567'
    url='m.example-portal.com'
    ("youtube.com", "웃긴 고양이 모음 - YouTube", "https://www.youtube.com/watch?v=%08d")
    app="chrome.exe", title="유튜브", url="https://youtube.com/watch?v=x"
    실제:  m.example-forum.com · m.example-board.com · example-blog.net
    {"album":"예시 앨범","artist":"예시 아티스트","title":"예시 곡"}
    합성 회귀 예시: 유튜브 20건 · 게임 30분
    /home/user/project/app · /home/runner/work/app · /home/user
    100.64.0.2 · 100.64.0.3 · 192.168.1.100
    wlan0 · wlxEXAMPLEMAC · 192.0.2.1
    8.8.8.8 · 93.184.216.34
    Chrome/131.0.0.0

정상적인 문서 내용도 걸리면 안 된다:

    schema v11 · 이벤트 60,616건 · arXiv 8개 소스
    https://github.com/ActivityWatch/aw-server-rust/issues/1234
    https://arxiv.org/abs/2508.01234
    버전 2026.9.8 · 포트 8770 · 커밋 8c7626a

2026-09-08 에 실제로 오탐이 났던 것들 — "URL 의 긴 숫자" 규칙을 걷어내게 만든 예제다:

    [공공데이터포털](https://www.data.go.kr/data/15125364/openapi.do)
    GPUDEV=/sys/devices/platform/bus@0/17000000.gpu/devfreq/17000000.gpu
    echo "scale=2;$want/1073741824" | bc
