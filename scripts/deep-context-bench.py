#!/usr/bin/env python3
"""
깊은 컨텍스트 테스트 — 속도 저하 + 장거리 검색 정확도 + OOM 한계

각 깊이마다 컨텍스트 '맨 앞'에 심어둔 고유 사실을 묻는다(needle-in-haystack).
속도만이 아니라 "깊어져도 앞쪽 정보를 찾아내는가"를 함께 본다.

실행: python3 scripts/deep-context-bench.py
"""
import json, time, urllib.request, subprocess

URL = "http://127.0.0.1:8080/v1/chat/completions"

REGIONS = ["서울","부산","대구","인천","광주","대전","울산","세종","경기","강원","충북","충남","전북","전남","경북","경남"] * 8
FIELDS = ["스마트제조","바이오헬스","친환경에너지","미래모빌리티","반도체소재","우주항공","차세대통신","로봇융합","디지털콘텐츠","농식품기술","해양플랜트","정밀화학","이차전지","양자컴퓨팅","사이버보안","물류혁신"] * 8


def doc(i):
    """공고 1건 — 각각 고유한 사업번호·지원금·마감일을 가진다."""
    n = 1000 + i * 7
    return f"""
[공고 제{n}호] {REGIONS[i % 16]}지역 {FIELDS[i % 16]} 기술개발 지원사업

1. 사업개요
{REGIONS[i % 16]} 소재 중소기업의 {FIELDS[i % 16]} 분야 기술경쟁력 강화를 위하여
연구개발 자금을 지원한다. 지역 산업 생태계 활성화와 일자리 창출을 목표로 하며,
산학연 협력 과제를 우대한다.

2. 지원내용
가. 총 사업규모: {50 + i * 3}억원
나. 과제당 지원한도: 연간 {i % 5 + 1}억원
다. 지원기간: 최대 {i % 3 + 2}년
라. 정부지원비율: 중소기업 {70 + i % 3 * 5}퍼센트 이내

3. 신청자격
가. {REGIONS[i % 16]} 소재 사업장 보유 중소기업
나. 업력 {i % 4 + 1}년 이상
다. 전년도 매출액 {i % 10 + 5}억원 이상
라. 연구전담부서 또는 기업부설연구소 보유

4. 접수일정
가. 공고: 2026년 {i % 12 + 1}월 1일
나. 접수마감: 2026년 {i % 12 + 1}월 {i % 20 + 8}일 18시
다. 평가: 접수마감 후 4주 이내
라. 협약체결: 선정통보 후 3주 이내

5. 평가항목
기술개발 목표의 명확성 {20 + i % 3 * 5}점, 기술적 파급효과 {25}점,
사업화 가능성 {30 - i % 3 * 5}점, 수행기관 역량 {25}점

6. 문의
{REGIONS[i % 16]}테크노파크 기업지원단 담당자 내선 {2000 + i}
"""


def ask(msgs, max_tokens=200):
    body = json.dumps({"model": "local", "messages": msgs,
                       "max_tokens": max_tokens, "temperature": 0.3}).encode()
    req = urllib.request.Request(URL, data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read()), time.time() - t0


def mem():
    o = subprocess.run(["free", "-m"], capture_output=True, text=True).stdout
    for l in o.splitlines():
        if l.startswith("Mem:"):
            p = l.split()
            return int(p[2]), int(p[6])   # used, available
    return 0, 0


def main():
    print("=" * 84)
    print(" 깊은 컨텍스트 테스트 — 속도 저하 / 장거리 검색 / OOM 한계")
    print("=" * 84)
    print(f"  {'문서수':>5}{'깊이':>9}{'프롬프트':>12}{'생성':>11}{'소요':>8}"
          f"{'RAM사용':>9}{'여유':>8}  검색정답")
    print("  " + "-" * 78)

    import sys
    counts = ([int(x) for x in sys.argv[1].split(",")]
              if len(sys.argv) > 1 else [1, 2, 4, 8, 12, 16])
    rows = []
    for ndocs in counts:
        # 맨 앞 문서(#0)의 고유 사실을 마지막에 묻는다 → 장거리 검색 테스트
        body = "\n".join(doc(i) for i in range(ndocs))
        q = ("다음은 여러 지원사업 공고 모음이다.\n\n" + body +
             "\n\n위 공고 중 '공고 제1000호'의 접수마감일과 과제당 지원한도를 "
             "정확히 답하라. 다른 설명은 붙이지 마라.")
        try:
            d, wall = ask([{"role": "user", "content": q}])
        except Exception as e:
            u, a = mem()
            print(f"  {ndocs:>5}{'—':>9}   실패: {str(e)[:44]}  (RAM {u}/{a}MB)")
            break

        t = d.get("timings", {}); us = d.get("usage", {})
        ans = d["choices"][0]["message"]["content"].strip().replace("\n", " ")
        u, a = mem()
        # 정답: 공고 제1000호(i=0) → 마감 2026년 1월 8일, 한도 연간 1억원
        ok = ("8일" in ans or "01-08" in ans or "1월 8" in ans) and "1억" in ans
        rows.append((ndocs, us.get("prompt_tokens", 0),
                     t.get("prompt_per_second", 0), t.get("predicted_per_second", 0),
                     wall, u, a, ok))
        print(f"  {ndocs:>5}{us.get('prompt_tokens',0):>9,}"
              f"{t.get('prompt_per_second',0):>11.1f}/s"
              f"{t.get('predicted_per_second',0):>10.2f}/s"
              f"{wall:>7.1f}s{u:>8,}M{a:>7,}M   {'✅' if ok else '❌'}")
        print(f"        └ 응답: {ans[:100]}")

    if len(rows) >= 2:
        f, l = rows[0], rows[-1]
        print("\n" + "=" * 84)
        print(f"  깊이 {f[1]:,} → {l[1]:,} 토큰")
        print(f"    생성 속도 {f[3]:.2f} → {l[3]:.2f} tok/s  ({(l[3]/f[3]-1)*100:+.1f}%)")
        print(f"    프롬프트  {f[2]:.1f} → {l[2]:.1f} tok/s  ({(l[2]/f[2]-1)*100:+.1f}%)")
        hit = sum(1 for r in rows if r[7])
        print(f"    장거리 검색 정확도: {hit}/{len(rows)}")
        print(f"    최대 도달 깊이: {max(r[1] for r in rows):,} 토큰 "
              f"(설정 32,768 대비 {max(r[1] for r in rows)/32768*100:.0f}%)")


if __name__ == "__main__":
    main()
