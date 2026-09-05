#!/usr/bin/env python3
"""PC 워처를 트레이에서 껐다 켠 구간이 **PC 의 aw-server 에 남는가**.

## 왜 재나

프라이빗 모드를 PC 에서 쓰는 길로 **aw-qt 트레이 → Modules 체크박스**를 고랐다
(`for_window/01-private-mode-pc-helper.md`). 그 선택의 전제가 *"끄면 그 시간은
기록 자체가 안 생긴다"* 인데, **그건 추론이지 측정이 아니었다.**

의심할 기전이 구체적으로 하나 있다. ActivityWatch 워처는 `heartbeat` 로 보내고
aw-server 는 `pulsetime` 안의 **같은 데이터를 하나로 합친다.** 다시 켰을 때 같은 창이
여전히 떠 있으면, 새 이벤트가 생기는 게 아니라 **마지막 이벤트의 duration 이 멈춘
구간을 넘어 늘어날** 수 있다.

★ 그래서 *"그 구간에 새 이벤트가 있나"* 만 보면 못 잡는다. **구간을 덮는 이벤트**를
  같이 본다 — 시작이 구간보다 앞이고 끝이 구간보다 뒤인 것.

## 젯슨에서 재는 이유

PC 의 aw-server 에 젯슨이 이미 닿는다(`cfg.aw.base_url`). **젯슨 DB 를 보면 안 된다** —
저장 관문(`privacy.clip_events`)이 프라이빗 구간을 잘라내므로, *PC 가 안 만든 것*과
*젯슨이 잘라낸 것*이 구분되지 않는다. **원천에 직접 묻는다.**

## 쓰는 법

    # 1) PC 트레이에서 aw-watcher-window · aw-watcher-afk 만 체크 해제
    #    (aw-server 는 켜 둔다 — 끄면 로컬 큐가 재개 시 한꺼번에 밀려 온다)
    # 2) 3분쯤 평소처럼 쓴다. 창을 몇 번 바꾼다
    # 3) 다시 체크한다
    # 4) 2분 기다린 뒤:
    life-trainer/.venv/bin/python measure/tools/pc-watcher-gap.py --minutes-ago 8 --window 3

★ **시스템 python3 이 아니라 앱 venv 로 돌린다** — `lifetrainer.config` 가 TOML 파서를
  쓰는데 이 기기의 시스템 파이썬에는 없다(3.10). 그냥 `python3` 으로 부르면 import 에서 죽는다.

`--minutes-ago` 는 **껐던 시각**이 몇 분 전인지, `--window` 는 꺼 뒀던 길이(분)다.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "life-trainer"))

from lifetrainer.collect.aw_client import AWClient  # noqa: E402
from lifetrainer.config import load_config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--minutes-ago", type=float, required=True, help="껐던 시각이 몇 분 전인가")
    ap.add_argument("--window", type=float, required=True, help="꺼 뒀던 길이(분)")
    args = ap.parse_args()

    cfg = load_config()
    now = time.time()
    start = now - args.minutes_ago * 60.0
    end = start + args.window * 60.0

    client = AWClient(cfg.aw.base_url, api_key=cfg.aw.api_key, timeout=cfg.aw.timeout_sec)
    print(f"원천: {cfg.aw.base_url}")
    print(
        f"검사 구간: {time.strftime('%H:%M:%S', time.localtime(start))}"
        f"–{time.strftime('%H:%M:%S', time.localtime(end))} ({args.window:.0f}분)\n"
    )

    for bucket_id in sorted(client.buckets()):
        if "watcher" not in bucket_id:
            continue
        # 넉넉히 앞뒤로 벌려 읽는다 — **구간을 덮는** 이벤트를 잡아야 한다.
        evs = client.events(bucket_id, start=start - 3600, end=end + 600)
        inside, covering = [], []
        for e in evs:
            e_end = e.ts + e.duration
            if e.ts >= start and e_end <= end:
                inside.append(e)
            elif e.ts < start and e_end > start + 1.0:
                covering.append(e)

        print(f"  {bucket_id}")
        print(f"    구간 안에 새로 생긴 이벤트   {len(inside)}건")
        for e in inside[:3]:
            print(f"      {time.strftime('%H:%M:%S', time.localtime(e.ts))} +{e.duration:.0f}s {e.data}")
        if covering:
            over = max((c.ts + c.duration) - start for c in covering)
            print(f"    ★ 구간을 **덮는** 이벤트     {len(covering)}건 — 최대 {over:.0f}초까지 넘어온다")
            for c in covering[:2]:
                print(
                    f"      {time.strftime('%H:%M:%S', time.localtime(c.ts))} +{c.duration:.0f}s {c.data}"
                )
        else:
            print("    구간을 덮는 이벤트           없음")
        print()

    # ★ **판정을 스스로 내리지 않는다.** 이 스크립트만으로는
    #   *"워처를 껐는데 병합이 구간을 삼켰다"* 와 *"안 껐고 그 창을 계속 보고 있었다"* 가
    #   구분되지 않는다 — 둘 다 "구간을 덮는 이벤트" 로 똑같이 보인다.
    #   (실제로 아무것도 안 끈 구간에 돌려 봤더니 롤 창 하나가 516초짜리로 덮고 있었다.)
    #
    #   그래서 **사람이 실제로 껐던 구간에 대해서만** 아래 숫자를 읽는다.
    print("─" * 60)
    print("★ 이 숫자는 **실제로 껐다 켠 구간**에 돌렸을 때만 뜻이 있다.")
    print("  안 끈 구간이면 긴 이벤트 하나가 그냥 덮고 있는 것이라 아무 뜻이 없다.\n")
    print("  껐던 구간이라면:")
    print("    '구간 안에 새로 생긴 이벤트' 0건 · '넘어온다' 가 거의 0초")
    print("        → ✅ 진짜 멈춤. 트레이로 끄면 기록 자체가 안 생긴다")
    print("    넘어오는 초가 꺼 둔 길이만큼 크다")
    print("        → ❌ heartbeat 병합이 구간을 삼켰다. 재개 뒤 정리가 필요하다")
    print("           (`lt private purge --minutes N --aw`)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
