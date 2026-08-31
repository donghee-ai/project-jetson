#!/usr/bin/env python3
"""
실제 대화로 컨텍스트 깊이별 성능 + 품질을 동시에 측정.

llama-bench 의 합성 벤치마크와 달리 운영 설정(q8_0 KV, thinking off)
그대로 측정하며, 응답 내용을 남겨 품질 판단 근거로 쓴다.

실행: python3 bench/chat-bench.py
"""
import json, time, urllib.request, sys

URL = "http://127.0.0.1:8080/v1/chat/completions"

# 컨텍스트를 채우기 위한 현실적 한국어 문서 (지원사업 공고 형식)
DOC = """
[2026년 인공지능 기반 스마트 제조혁신 기술개발사업 시행공고]

1. 사업목적
제조업 현장의 디지털 전환을 가속화하고 인공지능 기술을 활용한 생산성 향상을
도모하기 위하여 중소·중견기업의 기술개발을 지원한다. 특히 엣지 컴퓨팅 기반의
실시간 품질검사, 설비 예지보전, 공정 최적화 분야를 중점 지원한다.

2. 지원규모 및 기간
가. 총 사업비: 240억원 (2026년 80억원)
나. 과제당 지원금: 연간 최대 5억원, 총 3년 이내
다. 정부지원 비율: 중소기업 75% 이내, 중견기업 60% 이내
라. 민간부담금 중 현금 비율: 총 민간부담금의 40% 이상

3. 지원대상
가. 신청자격
  - 「중소기업기본법」 제2조에 따른 중소기업
  - 「중견기업 성장촉진 및 경쟁력 강화에 관한 특별법」 제2조에 따른 중견기업
  - 접수 마감일 기준 업력 3년 이상인 기업
  - 최근 결산 기준 매출액 10억원 이상 300억원 이하
나. 제외대상
  - 국세 및 지방세 체납 기업
  - 부채비율 500% 이상 또는 자본잠식 상태인 기업
  - 최근 3년 이내 국가연구개발사업 참여제한 처분을 받은 기업
  - 동일 과제로 타 부처 지원을 받고 있는 기업

4. 신청기간 및 방법
가. 접수기간: 2026년 3월 2일(월) 09:00 ~ 4월 15일(수) 18:00
나. 접수방법: 범부처통합연구지원시스템(IRIS) 온라인 접수
다. 문의처: 한국산업기술평가관리원 스마트제조혁신팀

5. 제출서류
가. 사업계획서(별지 제1호 서식) 1부
나. 사업자등록증 사본 1부
다. 최근 3개년 재무제표 1부
라. 중소기업 확인서 또는 중견기업 확인서 1부
마. 연구시설·장비 보유 현황 1부
바. 참여연구원 이력사항 1부

6. 평가절차 및 기준
가. 평가절차: 요건검토 → 서면평가 → 대면평가 → 최종선정
나. 평가기준
  - 기술성(40점): 기술의 혁신성, 개발목표의 구체성, 기술적 실현가능성
  - 사업성(30점): 시장규모 및 성장성, 사업화 전략의 타당성
  - 수행역량(20점): 연구인력 및 인프라, 과제수행 실적
  - 정책부합성(10점): 국가 정책방향 부합도, 고용창출 효과
다. 가점사항
  - 여성기업, 장애인기업: 2점
  - 뿌리기업 확인서 보유: 2점
  - 최근 3년 이내 특허 등록 5건 이상: 3점
"""

TASKS = [
    ("W1", "인사",
     "안녕하세요. 간단히 인사만 해주세요."),
    ("W2", "문서요약",
     f"다음 공고를 3줄로 요약해줘.\n\n{DOC}"),
    ("W3", "정보추출",
     "위 공고에서 신청 마감일과 과제당 연간 최대 지원금을 알려줘."),
    ("W4", "★JSON출력",
     '위 공고를 아래 JSON 스키마로만 출력해. 설명 문장 없이 JSON만.\n'
     '{"사업명":"","접수시작":"YYYY-MM-DD","접수마감":"YYYY-MM-DD",'
     '"연간최대지원금_억원":0,"최소업력_년":0,"매출하한_억원":0,"매출상한_억원":0}'),
    ("W5", "★자격판정",
     "우리 회사는 업력 2년, 매출 15억, 중소기업, 부채비율 300%야. "
     "이 사업에 지원 가능한지 판단하고 이유를 한 줄로 말해줘."),
    ("W6", "긴생성",
     "위 공고에 지원하려는 기업이 사업계획서 작성 시 유의할 점을 "
     "평가기준에 맞춰 자세히 설명해줘."),
    ("W7", "지시이행",
     "지금까지 대화를 정확히 다섯 단어로 요약해. 다른 말 붙이지 마."),
]


def ask(messages, max_tokens=700):
    body = json.dumps({
        "model": "local", "messages": messages,
        "max_tokens": max_tokens, "temperature": 0.7,
    }).encode()
    req = urllib.request.Request(URL, data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.loads(r.read())
    return d, time.time() - t0


def main():
    msgs, rows = [], []
    print("=" * 78)
    print(" 실사용 대화 벤치마크 — 컨텍스트 깊이별 성능 + 품질")
    print("=" * 78)

    for tid, name, prompt in TASKS:
        msgs.append({"role": "user", "content": prompt})
        try:
            d, wall = ask(msgs)
        except Exception as e:
            print(f"\n[{tid}] 실패: {e}")
            break
        m = d["choices"][0]["message"]["content"]
        msgs.append({"role": "assistant", "content": m})
        t = d.get("timings", {})
        u = d.get("usage", {})
        depth = u.get("prompt_tokens", 0)
        rows.append({
            "id": tid, "name": name, "depth": depth,
            "out": u.get("completion_tokens", 0),
            "pp": t.get("prompt_per_second", 0),
            "tg": t.get("predicted_per_second", 0),
            "wall": wall,
        })
        print(f"\n{'─'*78}")
        print(f"[{tid}] {name}   컨텍스트 깊이 {depth:,} 토큰")
        print(f"      프롬프트 {t.get('prompt_per_second',0):>7.1f} tok/s │ "
              f"생성 {t.get('predicted_per_second',0):>6.2f} tok/s │ "
              f"소요 {wall:.1f}초")
        print(f"{'·'*78}")
        out = m.strip()
        print(out[:600] + ("\n      …(생략)" if len(out) > 600 else ""))

    print("\n" + "=" * 78)
    print(" 요약")
    print("=" * 78)
    print(f"  {'단계':<6}{'항목':<12}{'깊이':>8}{'출력':>7}{'프롬프트':>11}{'생성':>10}{'소요':>8}")
    print("  " + "-" * 70)
    for r in rows:
        print(f"  {r['id']:<6}{r['name']:<12}{r['depth']:>8,}{r['out']:>7}"
              f"{r['pp']:>10.1f}/s{r['tg']:>9.2f}/s{r['wall']:>7.1f}s")

    if len(rows) >= 2:
        f, l = rows[0], rows[-1]
        print(f"\n  깊이 {f['depth']:,} → {l['depth']:,} 토큰 변화")
        if f["tg"] and l["tg"]:
            print(f"    생성 속도 {f['tg']:.2f} → {l['tg']:.2f} tok/s "
                  f"({(l['tg']/f['tg']-1)*100:+.1f}%)")


if __name__ == "__main__":
    main()
