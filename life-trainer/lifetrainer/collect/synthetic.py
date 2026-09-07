"""ActivityWatch 없이 파이프라인을 검증하기 위한 합성 활동 데이터 생성기.

`aw_bucket` / `aw_event` 에 직접 쓴다 (HTTP 경유 안 함).

## 설계 — 기상~취침을 먼저 정하고, 몇 시간짜리 "덩어리" 몇 개로 채운다

세 번의 반복을 거쳐 지금 모양이 됐다. 순서대로 겪은 문제와 해법을 적어둔다 — 셋 다
동시에 만족해야 하고, 하나를 고치다 다른 하나를 깨기 쉬운 자리라서 그렇다.

1. **하루가 중간에 끊김.** 세그먼트 길이를 무작위로 뽑아 이어 붙이기만 했더니, 뽑힌 길이의
   합이 "기상~취침"에 못 미쳐 오후에 하루가 그냥 끝나버렸다 (커버리지 20~30%). →
   기상·취침 시각을 먼저 정하고, 그 사이 전체(`total_span`)를 세그먼트 **비중(weight)**으로
   정확히 나눠 채운다. 세그먼트 길이의 합이 항상 `total_span` 과 같아 하루가 끊기지 않는다.
2. **미래 이벤트.** `end_day` 가 오늘이면 아직 오지 않은 시각의 이벤트가 생겼다. →
   `timeutil.now_ts()` 로 모든 날짜의 상한을 `min(day_end, now)` 로 클리핑한다.
3. **하루가 너무 잘게 쪼개짐.** 세그먼트 안을 5~10분 간격의 짧은 not-afk 조각으로
   채워 커버리지를 벌었는데, 조각마다 `_pick_window` 가 **다른 카테고리로 분류되는
   앱을 섞어 골랐다** (예: leisure 세그먼트에서 유튜브는 entertainment, reddit.com은
   browsing — 카테고리가 다르다. meeting/browsing/leisure 에 섞여 있던 explorer.exe
   는 ops). 슬롯의 "승자 카테고리"가 조각마다 다른 진짜 카테고리 사이를 오갔고, 그게
   10분마다 색이 바뀌는 "스트로브" 로 보였다 (하루 30~40개 블록, 최장 집중 20~30분).
   → 조각 개수를 줄인 게 아니라, **한 세그먼트 안의 모든 조각이 같은 카테고리로만
   떨어지도록 앱 풀을 정리했다** (`_pick_window`, `config/rules.yaml` 기준). `longest_focus`
   는 슬롯의 승자 카테고리가 몇 슬롯 연속인지만 보므로, 조각이 여러 개라도 전부 같은
   카테고리면 여전히 하나의 긴 블록으로 보인다 — 커버리지(조각을 흩뿌려서 번다)와
   긴 집중 블록(카테고리를 안 섞는다)을 동시에 만족한다.
   추가로 활동형 세그먼트 개수 자체도 8~10개에서 4개(평일: 오전 코딩·오후 코딩·회의·
   저녁 독서 / 주말: 아침 브라우징·독서·여가·독서2)로 줄였다 — 세그먼트 경계마다
   앞뒤로 붙는 자리비움 조각이 다음 세그먼트의 것과 이어져 하나의 "전환 구간"으로
   보이므로, 활동 세그먼트 수 × 2 가 하루 카테고리 전환 횟수의 기본 상한이다 (4개 →
   전환 ≤8, 블록 ≤9). 폭이 아주 넓은 세그먼트(>170분, 보통 오전/오후 코딩 중 더 넓은
   쪽) 하나는 중간에 확실한 자리비움을 끼워 두 "에피소드"로 나눈다 — 그래야
   `longest_focus` 가 3~5시간짜리 단일 블록으로 튀지 않는다. 분할은 전환을 2번 더
   쓰므로 하루 딱 하나만 허용한다 (기본 상한 9 + 분할 2 = 11, 여전히 12 이하).
   점심/저녁 식사는 아예 활동을 안 만든다 — 이 구간에 억지로 활동을 흩뿌리면
   그 자체가 여분의 블록이 된다.

이 조합으로 하루 활동 시간 5~9시간, 커버리지(비-off 슬롯 비율) 55~75%, 활동 세그먼트
6개 안팎(카테고리 전환 12회 이하), 최장 집중 45분 이상이 나오도록 맞췄다
(경험적으로 `lt synth && lt rollup && lt stats` 로 반복 검증).

재현성: 날짜별 RNG 시드를 `(seed, host, day)` 에서 결정론적으로 뽑는다.
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

from lifetrainer.collect.aw_client import AWEvent
from lifetrainer.collect.aw_sync import upsert_bucket, upsert_events
from lifetrainer.config import Config
from lifetrainer.db import transaction
from lifetrainer.timeutil import day_bounds, day_str, now_ts

# ── 앱/사이트 풀 ──────────────────────────────────────────────────────
# 실제로 흔한 프로세스명·창 제목을 써서 rollup 의 규칙 분류기(config/rules.yaml)가 실제로
# 매칭되게 한다. 풀 하나 안의 항목들은 전부 같은 카테고리로 분류돼야 한다 — 한 활동
# 세그먼트 안에서 앱이 바뀌어도 카테고리가 튀지 않게 하기 위해서다.

_CODE_TITLES = [
    "aw_sync.py — life-trainer — Visual Studio Code",
    "synthetic.py — life-trainer — Visual Studio Code",
    "schema.sql — life-trainer — Visual Studio Code",
    "README.md — project-jetson — Visual Studio Code",
    "contracts.md — docs — Visual Studio Code",
]
_TERMINAL_TITLES = [
    "python -m pytest tests/ — WindowsTerminal",
    "git status — WindowsTerminal",
    "ssh jetson@100.64.0.5 — WindowsTerminal",
    "npm run dev — WindowsTerminal",
    "sqlite3 data/lifetrainer.db — WindowsTerminal",
]
_SLACK_TITLES = [
    "# eng-jetson | Slack",
    "# team-standup | Slack",
    "DM with 팀장님 | Slack",
    "# life-trainer | Slack",
]
_ZOOM_TITLES = [
    "Zoom Meeting",
    "주간 스프린트 회의 - Zoom",
    "1:1 with 매니저 - Zoom",
]
_PDF_TITLES = [
    "2508.01234v1.pdf - SumatraPDF",
    "attention_is_all_you_need.pdf - SumatraPDF",
    "on_device_llm_survey.pdf - SumatraPDF",
]
_OBSIDIAN_TITLES = [
    "Daily Notes - Obsidian",
    "Life Trainer 설계 - Obsidian",
    "논문 요약 - Obsidian",
]
_MEDIA_TITLES = [
    "Lo-fi beats to code to - Spotify",
    "Podcast: 오늘의 기술 뉴스 - Spotify",
    "OST 재생목록 - VLC media player",
]
_EXPLORER_TITLES = ["Downloads", "project-jetson", "문서"]

# (도메인, 창 제목, URL 패턴) — URL 은 %d 로 매 호출마다 다른 숫자를 채운다.
_NEWS_SITES = [
    ("news.ycombinator.com", "Hacker News", "https://news.ycombinator.com/item?id=%d"),
    ("reddit.com", "reddit: the front page of the internet", "https://www.reddit.com/r/programming/"),
]
_CODE_SITES = [
    (
        "github.com",
        "GitHub - ActivityWatch/aw-server-rust",
        "https://github.com/ActivityWatch/aw-server-rust/issues/%d",
    ),
    (
        "stackoverflow.com",
        "python - sqlite3 upsert ON CONFLICT DO UPDATE - Stack Overflow",
        "https://stackoverflow.com/questions/%d",
    ),
]
_PAPER_SITES = [
    (
        "arxiv.org",
        "[2508.%05d] Efficient On-Device LLM Inference - arXiv.org",
        "https://arxiv.org/abs/2508.%05d",
    ),
    (
        "paperswithcode.com",
        "Papers with Code - On-Device Inference",
        "https://paperswithcode.com/task/on-device-inference",
    ),
]
# youtube/netflix 둘 다 rules.yaml 에서 entertainment 로 떨어진다 (reddit.com 은 browsing 이라
# 예전엔 leisure 세그먼트 안에서 카테고리가 튀는 원인이었다 — 그래서 뺐다).
_LEISURE_SITES = [
    ("youtube.com", "웃긴 고양이 모음 - YouTube", "https://www.youtube.com/watch?v=%08d"),
    ("netflix.com", "봤던 시리즈 다시보기 - Netflix", "https://www.netflix.com/watch/%08d"),
]


def _pick_site(rng: random.Random, pool: list[tuple[str, str, str]]) -> tuple[str, str]:
    """사이트 풀에서 하나 골라 (title, url) 을 만든다."""
    _domain, title, url_pattern = rng.choice(pool)
    if "%d" in url_pattern or "%05d" in url_pattern:
        url = url_pattern % rng.randint(1, 99999)
    else:
        url = url_pattern
    return title, url


def _pick_window(rng: random.Random, effective_type: str) -> tuple[str, str, str | None]:
    """세그먼트 종류에 맞는 (app, title, url) 하나를 확률적으로 뽑는다.

    같은 `effective_type` 안에서 고르는 항목들은 전부 `config/rules.yaml` 기준으로
    같은 카테고리에 떨어진다 — 세그먼트 하나가 롤업 결과에서도 "하나의 블록"으로 보이게
    하기 위한 핵심 규칙이다. `reading` 은 세그먼트 단위로 한 번만 "논문/노트" 갈래를
    정해 그 갈래(`reading_research`/`reading_notes`) 안에서만 고른다.
    """
    r = rng.random()
    if effective_type == "browsing":  # -> browsing/news
        title, url = _pick_site(rng, _NEWS_SITES)
        return "chrome.exe", title, url
    if effective_type == "coding":  # -> coding (editor/terminal/reference 전부)
        if r < 0.55:
            return "Code.exe", rng.choice(_CODE_TITLES), None
        if r < 0.85:
            return "WindowsTerminal.exe", rng.choice(_TERMINAL_TITLES), None
        title, url = _pick_site(rng, _CODE_SITES)
        return "chrome.exe", title, url
    if effective_type == "meeting":  # -> sns/chat
        if r < 0.55:
            return "Slack.exe", rng.choice(_SLACK_TITLES), None
        return "Zoom.exe", rng.choice(_ZOOM_TITLES), None
    if effective_type == "reading_research":  # -> measure/findings/paper
        title, url = _pick_site(rng, _PAPER_SITES)
        return "chrome.exe", title, url
    if effective_type == "reading_notes":  # -> writing/pdf, writing/notes
        if r < 0.5:
            return "SumatraPDF.exe", rng.choice(_PDF_TITLES), None
        return "Obsidian.exe", rng.choice(_OBSIDIAN_TITLES), None
    if effective_type == "leisure":  # -> entertainment (video/app 전부)
        if r < 0.6:
            title, url = _pick_site(rng, _LEISURE_SITES)
            return "chrome.exe", title, url
        return ("spotify.exe" if r < 0.8 else "vlc.exe"), rng.choice(_MEDIA_TITLES), None
    # 방어적 fallback (이론상 도달하지 않는다 — 점심/저녁은 이 함수를 아예 호출하지 않는다).
    return "explorer.exe", rng.choice(_EXPLORER_TITLES), None


def _split_duration(rng: random.Random, total: float, k: int) -> list[float]:
    """total 초를 k 조각으로 무작위 비례 분할한다. 합은 정확히 total 과 같다."""
    if k <= 1 or total <= 0:
        return [total]
    weights = [rng.random() + 0.15 for _ in range(k)]
    s = sum(weights)
    parts = [total * w / s for w in weights]
    parts[-1] = total - sum(parts[:-1])  # 부동소수 오차를 마지막 조각에 흡수
    return parts


def _split_duration_capped(rng: random.Random, total: float, k: int, max_piece: float) -> list[float]:
    """`_split_duration` 과 같지만, 개별 조각이 `max_piece` 를 웬만하면 넘지 않게 눌러준다.

    자리비움 조각 하나가 우연히 롤업 슬롯(기본 10분)보다 커지면, 원래는 하나로 이어져야
    할 활동 블록이 그 자리에서 진짜로 두 조각으로 쪼개진다 — 무작위 비례 분할은 평균은
    맞아도 개별 조각의 분산이 커서 이런 이상치가 드물지 않게 나온다. 초과분을 여유가
    있는 다른 조각들로 몇 차례 나눠 옮겨 최대치를 억누른다 (완벽한 상한 보장은 아니다 —
    `k` 가 `total/max_piece` 보다 너무 작으면 평균 자체가 상한을 넘어버린다. 호출부가
    `k` 를 충분히 크게 잡아야 한다).
    """
    parts = _split_duration(rng, total, k)
    if k <= 1:
        return parts
    for _ in range(4):
        overflow = 0.0
        headroom_idx: list[int] = []
        for i, p in enumerate(parts):
            if p > max_piece:
                overflow += p - max_piece
                parts[i] = max_piece
            elif p < max_piece:
                headroom_idx.append(i)
        if overflow <= 1e-6 or not headroom_idx:
            break
        share = overflow / len(headroom_idx)
        for i in headroom_idx:
            parts[i] += share
    return parts


def _segment_piece_count(rng: random.Random, seg_type: str, total: float) -> int:
    """활동 조각 하나를 몇 개의 window 이벤트(앱 전환)로 쪼갤지 — 카테고리는 유지한 채 앱만 바뀐다."""
    ranges = {
        "browsing": (1, 2),
        "coding": (2, 4),
        "meeting": (1, 2),
        "reading": (1, 2),
        "leisure": (1, 2),
    }
    base_type = seg_type.split("_", 1)[0]  # reading_measure/findings/reading_notes -> reading
    lo, hi = ranges.get(base_type, (1, 1))
    k = rng.randint(lo, hi)
    # 앱 전환 하나가 최소 3분은 되게 — 너무 잘게 쪼개면 다시 "스트로브"가 된다.
    return max(1, min(k, int(total // 180) or 1))


# 활동형 세그먼트(연속된 not-afk 덩어리를 만드는 것들)의 not-afk 밀도 "원형"(raw) 범위.
# 절대값은 하루 단위로 목표 활동시간(_DAILY_ACTIVE_TARGET_HOURS)에 맞춰 일괄 보정되므로,
# 여기서는 세그먼트 종류 사이의 "상대적" 밀도 차이만 정확하면 된다.
_ACTIVE_SEGMENT_TYPES = ("browsing", "coding", "meeting", "reading", "leisure")
_DENSITY_RANGES: dict[str, tuple[float, float]] = {
    "coding": (0.70, 0.90),
    "meeting": (0.45, 0.65),
    "reading": (0.60, 0.82),
    "browsing": (0.55, 0.80),
    "leisure": (0.50, 0.75),
}

# 하루 전체 not-afk(활동) 시간의 목표 범위. 5~9시간 경계값 자체를 목표로 쓰면 부동소수/
# 반올림으로 살짝 넘칠 수 있어 안쪽에 여유를 둔다.
_DAILY_ACTIVE_TARGET_HOURS = (5.5, 8.3)

# 활동형 세그먼트 안에서 not-afk "조각" 사이의 평균 간격(초). 롤업 슬롯(기본 10분) 안에
# not-afk 시간이 5% 미만이면 그 슬롯이 통째로 `off` 로 떨어지므로, 세그먼트 폭 전체에
# 걸쳐 5~10분 간격으로 조각을 흩뿌려야 세그먼트 폭만큼 커버리지가 확보된다.
# (`longest_focus` 는 슬롯의 "승자 카테고리"가 몇 슬롯 연속인지만 보므로, 조각이 여러
# 개여도 전부 같은 카테고리로 떨어지는 한 — `_pick_window` 가 보장한다 — 하나의 긴
# 집중 블록으로 보인다. 예전 버그는 조각 개수가 아니라 조각마다 "다른 카테고리"를
# 뽑았던 것이었다.)
_BURST_GAP_RANGE = (300.0, 600.0)

# 자리비움 조각 하나의 상한(초). 롤업 슬롯(기본 600초)보다 확실히 작게 잡아야, 무작위
# 분할의 이상치 하나가 슬롯 하나를 통째로 `off` 로 만들어 "하나의 블록"을 둘로 쪼개는
# 사고를 막을 수 있다 (실측 사례 — 60~90분짜리 자리비움 조각이 우연히 나와 회의/코딩
# 블록이 3조각으로 갈라진 적이 있다).
_MAX_AFK_PIECE_SEC = 420.0


def _split_active_afk(rng: random.Random, total: float, density: float, n_active: int) -> list[tuple[bool, float]]:
    """total 초를 [afk, active, afk, active, ..., afk] 순서로 쪼갠다.

    `n_active` 개의 활동 조각이 자리비움 조각과 번갈아 나온다. 조각이 여러 개여도
    `_pick_window` 가 세그먼트 전체에 걸쳐 같은 카테고리로 떨어지는 앱만 고르므로,
    롤업에서는 여전히 하나의 긴 집중 블록으로 보인다 (조각 개수는 커버리지에만 영향).
    active 조각 합 == total*density, afk 조각 합 == total*(1-density) 이 되도록
    (부동소수 오차까지) 정확히 맞춘다. 0에 가까운 조각은 버린다. 자리비움 조각은
    `_MAX_AFK_PIECE_SEC` 를 웬만하면 넘지 않도록 눌러준다(`_split_duration_capped`).
    """
    active_total = total * density
    afk_total = total - active_total
    active_parts = _split_duration(rng, active_total, n_active) if active_total > 0 else []
    afk_parts = (
        _split_duration_capped(rng, afk_total, n_active + 1, _MAX_AFK_PIECE_SEC)
        if afk_total > 0
        else [0.0] * (n_active + 1)
    )

    seq: list[tuple[bool, float]] = []
    for i in range(n_active):
        seq.append((False, afk_parts[i]))
        seq.append((True, active_parts[i]))
    seq.append((False, afk_parts[n_active]))
    return [(is_active, d) for is_active, d in seq if d > 0.5]


# (세그먼트 종류, 비중) — 합이 1.0 이 되도록 맞춰져 있고, 매 호출마다 지터를 준다.
# 활동형 세그먼트를 5개(평일/주말 공통)로 눌러뒀다 — 세그먼트 하나가 [자리비움-활동-
# 자리비움] 3조각이라 세그먼트 수 × 2 가 하루 카테고리 전환 횟수의 상한이기 때문이다
# (5개 × 2 = 10 전환 = 11 블록, 회의 없는 날은 4개 × 2 = 8 전환 = 9 블록 — 항상 12 이하).
# lunch/dinner 는 활동형이 아니다 — 통짜 자리비움 블록 하나로 처리된다 (아래 _generate_day).
# 평일에는 아침 브라우징을 별도 세그먼트로 두지 않는다 — 두면 6개가 되어 상한(12)을 넘긴다.
_WEEKDAY_SEGMENTS: list[tuple[str, float]] = [
    ("coding", 0.24),
    ("lunch", 0.08),
    ("coding", 0.22),
    ("meeting", 0.20),
    ("dinner", 0.07),
    ("reading", 0.19),
]
_WEEKEND_SEGMENTS: list[tuple[str, float]] = [
    ("browsing", 0.10),
    ("reading", 0.24),
    ("lunch", 0.08),
    ("leisure", 0.26),
    ("dinner", 0.07),
    ("reading", 0.25),
]


def _build_weighted_schedule(rng: random.Random, is_weekend: bool) -> list[tuple[str, float]]:
    """세그먼트 종류별 비중(합 1.0)을 만든다.

    평일은 가끔 회의 없는 날이 섞인다 — 그 비중은 기존 코딩 세그먼트들에 나눠 흡수시킨다
    (새 세그먼트를 추가하지 않는다. 추가하면 활동 블록 개수가 다시 늘어난다).
    """
    base = list(_WEEKEND_SEGMENTS if is_weekend else _WEEKDAY_SEGMENTS)
    if not is_weekend and rng.random() < 0.3:
        meeting_w = next((w for t, w in base if t == "meeting"), 0.0)
        base = [(t, w) for t, w in base if t != "meeting"]
        coding_idx = [i for i, (t, _) in enumerate(base) if t == "coding"]
        if meeting_w and coding_idx:
            share = meeting_w / len(coding_idx)
            for i in coding_idx:
                t, w = base[i]
                base[i] = (t, w + share)
        elif meeting_w:
            t0, w0 = base[0]
            base[0] = (t0, w0 + meeting_w)

    jittered = [(t, w * rng.uniform(0.8, 1.25)) for t, w in base]
    total_w = sum(w for _, w in jittered)
    return [(t, w / total_w) for t, w in jittered]


# 집중 블록(연속 같은 카테고리 구간) 하나가 너무 길어지지 않도록 하는 상한. 세그먼트
# 폭이 이보다 넓으면 중간에 확실한 자리비움 한 번(_EPISODE_GAP_RANGE)을 끼워 두 개의
# "에피소드"로 나눈다 — 그래도 같은 카테고리라 여전히 하나의 활동으로 보이지만,
# `longest_focus` 는 짧아진다. 세그먼트당 최대 한 번만 나눈다(전환 횟수 예산 보호).
_EPISODE_SPLIT_THRESHOLD_SEC = 170 * 60.0
_EPISODE_GAP_RANGE = (20.0 * 60.0, 32.0 * 60.0)


def _fill_bursts(
    rng: random.Random,
    seg_type: str,
    effective_type: str,
    t0: float,
    width: float,
    density: float,
    window_bucket: str,
    afk_bucket: str,
    web_bucket: str,
    window_events: list[AWEvent],
    afk_events: list[AWEvent],
    web_events: list[AWEvent],
) -> None:
    """폭 `width` 구간을 [자리비움, 활동] 조각 여러 개로 채운다 (커버리지를 벌기 위한 잔가지).

    조각이 여러 개여도 `effective_type` 하나로 고정된 카테고리 앱만 고르므로(`_pick_window`),
    롤업에서는 하나의 연속 블록으로 보인다.
    """
    avg_gap = rng.uniform(*_BURST_GAP_RANGE)
    n_active = max(1, min(60, round(width / avg_gap)))
    # 자리비움 조각 개수(n_active+1)가 총 자리비움 시간을 _MAX_AFK_PIECE_SEC 이하로 충분히
    # 잘게 나눌 수 있을 만큼은 돼야 한다 — 안 그러면 밀도가 낮은(=자리비움이 많은)
    # 세그먼트에서 개별 조각이 상한을 넘는 이상치가 나오기 쉽다.
    afk_total_estimate = width * (1.0 - density)
    n_active_for_afk = max(0, math.ceil(afk_total_estimate / _MAX_AFK_PIECE_SEC) - 1)
    n_active = max(n_active, min(80, n_active_for_afk))

    t = t0
    for is_active, dur in _split_active_afk(rng, width, density, n_active):
        if is_active:
            k = _segment_piece_count(rng, seg_type, dur)
            piece_start = t
            for piece_dur in _split_duration(rng, dur, k):
                app, title, url = _pick_window(rng, effective_type)
                window_events.append(
                    AWEvent(bucket_id=window_bucket, ts=piece_start, duration=piece_dur, data={"app": app, "title": title})
                )
                if url is not None:
                    web_events.append(
                        AWEvent(
                            bucket_id=web_bucket,
                            ts=piece_start,
                            duration=piece_dur,
                            data={"url": url, "title": title, "audible": False, "incognito": False},
                        )
                    )
                piece_start += piece_dur
            afk_events.append(AWEvent(bucket_id=afk_bucket, ts=t, duration=dur, data={"status": "not-afk"}))
        else:
            afk_events.append(AWEvent(bucket_id=afk_bucket, ts=t, duration=dur, data={"status": "afk"}))
        t += dur


def _process_active_segment(
    rng: random.Random,
    seg_type: str,
    seg_start: float,
    seg_dur: float,
    density: float,
    allow_split: bool,
    window_bucket: str,
    afk_bucket: str,
    web_bucket: str,
    window_events: list[AWEvent],
    afk_events: list[AWEvent],
    web_events: list[AWEvent],
) -> None:
    """활동형 세그먼트 하나를 채운다.

    `allow_split` 이 참이고 폭이 넓으면(>_EPISODE_SPLIT_THRESHOLD_SEC) 중간에 확실한
    자리비움 한 번을 끼워 두 "에피소드"로 나눈다 — 그래야 `longest_focus` 가 3~5시간짜리
    단일 블록으로 튀지 않고 1~2.5시간대에 머문다. 나누기는 전환 횟수를 2번 더 쓰므로,
    호출부(`_generate_day`)가 하루에 **가장 넓은 활동형 세그먼트 하나에만** 허용한다 —
    아무 세그먼트나 나누면 하루 카테고리 전환이 12개를 넘어가 버린다.
    """
    effective_type = seg_type
    if seg_type == "reading":
        effective_type = "reading_research" if rng.random() < 0.6 else "reading_notes"

    if not allow_split or seg_dur <= _EPISODE_SPLIT_THRESHOLD_SEC:
        _fill_bursts(
            rng, seg_type, effective_type, seg_start, seg_dur, density,
            window_bucket, afk_bucket, web_bucket, window_events, afk_events, web_events,
        )
        return

    gap = rng.uniform(*_EPISODE_GAP_RANGE)
    split_point = seg_dur * rng.uniform(0.4, 0.6)
    ep1_dur = split_point
    ep2_dur = seg_dur - split_point - gap
    if ep2_dur <= 300.0:  # 나눠봐야 두 번째 에피소드가 너무 짧으면 그냥 하나로 둔다
        _fill_bursts(
            rng, seg_type, effective_type, seg_start, seg_dur, density,
            window_bucket, afk_bucket, web_bucket, window_events, afk_events, web_events,
        )
        return

    _fill_bursts(
        rng, seg_type, effective_type, seg_start, ep1_dur, density,
        window_bucket, afk_bucket, web_bucket, window_events, afk_events, web_events,
    )
    gap_start = seg_start + ep1_dur
    afk_events.append(AWEvent(bucket_id=afk_bucket, ts=gap_start, duration=gap, data={"status": "afk"}))
    ep2_start = gap_start + gap
    _fill_bursts(
        rng, seg_type, effective_type, ep2_start, ep2_dur, density,
        window_bucket, afk_bucket, web_bucket, window_events, afk_events, web_events,
    )


def _generate_day(
    rng: random.Random,
    day: str,
    tz,
    now: float,
    window_bucket: str,
    afk_bucket: str,
    web_bucket: str,
) -> tuple[list[AWEvent], list[AWEvent], list[AWEvent]]:
    """하루치 (window 이벤트, afk 이벤트, web 이벤트) 를 만든다.

    기상 시각과 취침 시각을 먼저 정하고, 그 사이 전체를 세그먼트 비중으로 나눠 채운다.
    `now` 이후 시각은 만들지 않는다 — 오늘 날짜라면 `day_end` 대신 `now` 가 상한이 된다.
    """
    day_start, day_end = day_bounds(day, tz)
    cap_end = min(day_end, now)  # 미래 이벤트 금지: 오늘이면 now 가, 과거면 자정이 상한.

    weekday = date.fromisoformat(day).weekday()
    is_weekend = weekday >= 5

    wake_hour = rng.uniform(7.5, 9.5) if is_weekend else rng.uniform(6.0, 8.0)
    sleep_hour = rng.uniform(25.0, 27.0) if is_weekend else rng.uniform(24.0, 26.0)
    wake_ts = day_start + wake_hour * 3600.0
    sleep_ts = min(day_start + sleep_hour * 3600.0, day_end)

    window_events: list[AWEvent] = []
    afk_events: list[AWEvent] = []
    web_events: list[AWEvent] = []

    total_span = min(sleep_ts, cap_end) - wake_ts
    if total_span <= 0:
        # 아직 기상 전(예: 오늘을 새벽에 생성)이거나 완전히 미래인 날 — 이벤트 없음이 맞다.
        return window_events, afk_events, web_events

    # 진짜 외출(버킷 어디에도 데이터가 없는 구간). 하루 전체 흐름에서 한 번만 끼워 넣는다.
    has_excursion = rng.random() < (0.3 if is_weekend else 0.18)
    excursion_dur = rng.uniform(10, 30) * 60.0 if has_excursion else 0.0
    if excursion_dur >= total_span:
        excursion_dur = 0.0
        has_excursion = False
    remaining_span = total_span - excursion_dur

    weighted = _build_weighted_schedule(rng, is_weekend)
    plan: list[tuple[str, float]] = [(seg_type, remaining_span * w) for seg_type, w in weighted]
    plan = [(t, d) for t, d in plan if d >= 120.0]  # 2분 미만 조각은 만들지 않는다

    if has_excursion:
        insert_idx = next((i for i, (t, _) in enumerate(plan) if t == "dinner"), None)
        if insert_idx is None:
            insert_idx = next((i for i, (t, _) in enumerate(plan) if t == "reading"), len(plan))
        plan.insert(insert_idx, ("excursion", excursion_dur))

    # 활동형 세그먼트의 "원형" 밀도를 먼저 뽑고, 하루 총 활동시간이 목표 범위
    # (_DAILY_ACTIVE_TARGET_HOURS) 에 들어오도록 한 번에 스케일링한다. 점심/저녁/외출은
    # 애초에 활동을 만들지 않으므로 이 계산에서 아예 빠진다.
    raw_densities = [
        rng.uniform(*_DENSITY_RANGES[seg_type]) if seg_type in _ACTIVE_SEGMENT_TYPES else 0.0
        for seg_type, _ in plan
    ]
    naive_active = sum(
        dur * rd
        for (seg_type, dur), rd in zip(plan, raw_densities, strict=True)
        if seg_type in _ACTIVE_SEGMENT_TYPES
    )
    target_active = rng.uniform(*_DAILY_ACTIVE_TARGET_HOURS) * 3600.0
    scale = (target_active / naive_active) if naive_active > 0 else 1.0
    densities = [min(0.95, max(0.05, rd * scale)) for rd in raw_densities]

    # 하루에 딱 하나, 가장 넓은 활동형 세그먼트에만 에피소드 분할을 허용한다 (전환 횟수
    # 예산 보호 — 둘 이상 허용해봤더니 예외적인 조합에서 블록이 17개까지 튄 사례가 있었다.
    # 하나만 허용하면 실측상 항상 12개 이하였다).
    active_indices = [i for i, (seg_type, _) in enumerate(plan) if seg_type in _ACTIVE_SEGMENT_TYPES]
    widest_idx = max(active_indices, key=lambda i: plan[i][1]) if active_indices else -1
    split_allowed = {widest_idx}

    cur = wake_ts
    cap = min(sleep_ts, cap_end)
    for i, ((seg_type, dur), density) in enumerate(zip(plan, densities, strict=True)):
        if cur >= cap:
            break
        end = min(cur + dur, cap)
        seg_dur = end - cur
        if seg_dur <= 0:
            break

        if seg_type in _ACTIVE_SEGMENT_TYPES:
            _process_active_segment(
                rng, seg_type, cur, seg_dur, density, i in split_allowed, window_bucket, afk_bucket, web_bucket,
                window_events, afk_events, web_events,
            )
        elif seg_type == "excursion":
            pass  # 어떤 버킷에도 이벤트를 만들지 않는다 — 진짜 관측 공백.
        else:
            # 점심/저녁 식사 — 통짜 자리비움 블록 하나. 억지로 활동을 흩뿌리지 않는다
            # (그러면 다시 블록이 잘게 쪼개진다). 커버리지는 기상~취침 구간을 넉넉히
            # 잡는 쪽으로 맞춘다.
            afk_events.append(AWEvent(bucket_id=afk_bucket, ts=cur, duration=seg_dur, data={"status": "afk"}))

        cur = end

    window_events.sort(key=lambda e: e.ts)
    afk_events.sort(key=lambda e: e.ts)
    web_events.sort(key=lambda e: e.ts)
    return window_events, afk_events, web_events


def generate(
    conn,
    cfg: Config,
    *,
    days: int = 7,
    end_day: str | None = None,
    seed: int = 42,
    host: str = "synth-pc",
) -> int:
    """`days` 일치 현실적인 합성 활동을 만들어 `aw_bucket`/`aw_event` 에 upsert 한다.

    같은 (seed, host, end_day, days) 조합이면 항상 같은 결과가 나온다 (단, `now` 이후의
    이벤트는 절대 만들지 않으므로 `end_day` 가 오늘이면 현재 시각까지만 채워진다).
    반환값은 새로 만든(=upsert 시도한) 이벤트 총 개수다.
    """
    tz = cfg.tz
    now = now_ts()
    if end_day is None:
        end_day = day_str(now, tz)

    end_date = date.fromisoformat(end_day)
    start_date = end_date - timedelta(days=days - 1)
    day_list = [(start_date + timedelta(days=i)).isoformat() for i in range(days)]

    window_bucket = f"aw-watcher-window_{host}"
    afk_bucket = f"aw-watcher-afk_{host}"
    web_bucket = f"aw-watcher-web-chrome_{host}"

    all_window: list[AWEvent] = []
    all_afk: list[AWEvent] = []
    all_web: list[AWEvent] = []

    for day in day_list:
        rng = random.Random(f"{seed}:{host}:{day}")
        window_events, afk_events, web_events = _generate_day(
            rng, day, tz, now, window_bucket, afk_bucket, web_bucket
        )
        all_window.extend(window_events)
        all_afk.extend(afk_events)
        all_web.extend(web_events)

    with transaction(conn):
        upsert_bucket(conn, window_bucket, {"hostname": host, "client": "aw-watcher-window"})
        upsert_bucket(conn, afk_bucket, {"hostname": host, "client": "aw-watcher-afk"})
        upsert_bucket(conn, web_bucket, {"hostname": host, "client": "aw-watcher-web-chrome"})

        n = upsert_events(conn, all_window)
        n += upsert_events(conn, all_afk)
        n += upsert_events(conn, all_web)

    return n
