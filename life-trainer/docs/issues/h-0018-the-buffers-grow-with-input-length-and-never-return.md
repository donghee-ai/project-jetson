# 임베딩 계산 버퍼가 입력 길이에 비례해 자라고 반납되지 않는다

- **번호**: `0018`
- **발견일**: 2026-08-29 (대조 실험) · 전조는 2026-08-23 (유닛 주석의 "누수")
- **상태**: 🟡 **h — 동작은 한다.** `MemoryMax` + `MemorySwapMax=0` + `Restart=on-failure`
  가 자가복구하고, 배치는 실패 없이 완주한다. 근본 원인은 llama.cpp 쪽에 남아 있다

## 무엇이 사실인가 (측정됨)

같은 요청을 반복하면 자라다가 **평탄해진다.** 무한 누수가 아니다.
증가분을 정하는 것은 **요청 수가 아니라 입력 길이**다.

```
짧은 문장 (4건 × 약 40자) × 60건
  기동직후 1,793MB → 20건 1,868 → 40건 1,874 → 60건 1,874     +81MB 뒤 평탄

실문서 (4건 × 1,600자, doc 테이블 실데이터) × 80건
  기동직후 1,684MB → 40건 2,821 → 80건 2,862                 +1,178MB 뒤 평탄
```

**14배 차이가 난다.** llama.cpp 가 본 것 중 가장 큰 배치에 맞춰 계산 버퍼를 잡고,
요청이 끝나도 OS 에 돌려주지 않는다. 그래서 짧은 요청만 재면 문제가 안 보인다 —
**처음에 그렇게 재서 "평탄해지니 괜찮다" 는 잘못된 결론을 낼 뻔했다.**

## 재현

```bash
# 실문서 40건을 4건씩 10요청. 쓰기 없음(읽기 전용 연결)
life-trainer/.venv/bin/python - > /tmp/payloads.json <<'PY'
import sqlite3, json
c = sqlite3.connect('file:life-trainer/data/lifetrainer.db?mode=ro', uri=True)
rows = [r[0][:1600] for r in c.execute(
    "SELECT abstract FROM doc WHERE abstract IS NOT NULL AND LENGTH(abstract)>1200 LIMIT 40")]
print(json.dumps([{"model": "qwen3-embedding-0.6b", "input": rows[i:i+4]}
                  for i in range(0, 40, 4)], ensure_ascii=False))
PY

systemctl --user restart llama-embed && sleep 4
systemctl --user show llama-embed -p MemoryCurrent --value   # 기동직후
python3 -c "
import json,urllib.request
for p in json.load(open('/tmp/payloads.json')):
    urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8081/v1/embeddings',
        json.dumps(p).encode(), {'Content-Type':'application/json'}), timeout=180).read()"
systemctl --user show llama-embed -p MemoryCurrent --value   # 40건 후
```

## 원인 — 세 층이 겹친다

| | 무엇 | 확인 |
|---|---|---|
| **1** | llama.cpp 가 최대 배치 크기에 맞춘 계산 버퍼를 잡고 반납하지 않는다 | 위 표. 입력 길이에 비례하고 평탄해진다 |
| **2** | glibc 아레나가 반납하지 않는다 | `MALLOC_ARENA_MAX` 미설정 → 기본 8×코어수. 1MB 이상 익명 매핑 35개 |
| **3** | THP 가 그 파편을 2MB 단위로 증폭한다 | `transparent_hugepage/enabled = [always]` · 익명 963MB 중 **948MB(98.5%)가 THP** |

3 이 증폭기다. huge page 안에서 조각난 빈 공간은 반납이 안 되므로,
살아 있는 양보다 훨씬 빠르게 붇는다. 대조로 8B 는 익명이 91MB 뿐이라
(가중치·KV 가 nvmap 에 있다) 같은 증폭을 안 겪는다.

## 영향

야간 배치가 `embed_limit = 1000` 이다. **지금 확인된 것은 80건까지**이므로
큰 백필에서는 여전히 상한을 칠 수 있다. 실제로 2026-08-29 05:50~05:54 에
185건을 돌리며 **OOM 재시작 5회**가 났다 (`0018` 조치 이전 상태).

죽어도 데이터는 안 잃는다 — `embed_pending()` 이 `EmbedUnavailable` 을 잡아
재기동을 기다렸다 그 배치를 재시도하고, `flush_every` 단위로 커밋한다.
그날도 `185건 생성 · 0건 실패` 로 완주했다. **비용은 배치 창의 약 2분과 프로세스 churn 이다.**

## 이미 한 것 (2026-08-29)

유닛에 세 줄을 넣었다. 근거와 경위는
[HISTORY](../../HISTORY/2026-08-29-a-cpu-only-server-was-holding-gpu-memory.md).

```ini
Environment=CUDA_VISIBLE_DEVICES=          # -ngl 0 인데 켜져 있던 CUDA 백엔드를 끈다
Environment=MALLOC_ARENA_MAX=2
Environment=MALLOC_TRIM_THRESHOLD_=131072
```

```
              dmabuf   VmRSS(기동)   cgroup(기동)   여유(상한 2,500)
전            646MB      1,793MB       1,927MB          573MB
후              0MB      1,702MB       1,323MB        1,177MB

같은 실문서 부하:  대조군 32건에서 OOM 사망 · 실험군 80건 생존
```

**cgroup 헤드룸이 2배가 됐고 실패 지점이 32건 → 80건 이상으로 밀렸다.**
다만 상한 자체를 없앤 것은 아니다.

> **★ 2026-08-31: 같은 병이 8B 에서도 나왔다** — 요청 1건당 70~105MB.
> 이쪽은 **전역 OOM 으로 기기를 죽였다** ([`0019`](h-0019-the-8b-server-grows-about-eighty-megabytes-per-request.md)).
> 두 건이 같은 상류(llama.cpp)를 가리키므로 **후보 4(온디맨드)를 빼면 대책이 겹친다.**

## 아직 모르는 것

- **세 줄 중 무엇이 얼마나 기여했는지 분리하지 않았다.** `dmabuf 646MB → 0` 은
  `CUDA_VISIBLE_DEVICES` 로 단정할 수 있다(단일 관측). **`MALLOC_*` 두 줄의 기여는
  측정하지 않았다** — 한 번에 셋을 넣었다
- THP 를 `madvise` 로 내렸을 때의 효과는 **측정 안 했다.** 전역 sysctl 이고 sudo 가 필요하다.
  8B 의 익명이 91MB 뿐이라 8B 가 잃는 것은 적어 보이지만, **보인다는 것과 쟀다는 것은 다르다**
- 평탄해지는 상한이 입력 길이의 어떤 함수인지 모른다. 1,600자에서 +1,178MB 라는
  점 하나만 있다. `max_chars` 를 낮추면 선형으로 줄어드는지 확인 안 됨
- `-b 2048` 과 실제 요청(4건 × 최대 1,600자 ≈ 최대 3,200토큰)이 어긋난다.
  llama.cpp 가 ubatch 로 쪼개 처리하지만, **그 쪼개기가 버퍼 크기에 어떻게 반영되는지 모른다**

## 고칠 것 — 후보

1. **`MALLOC_*` 두 줄을 따로 재서 남기거나 뺀다.** 효과 없는 설정이 유닛에 남으면
   다음 사람이 그것 때문이라고 믿는다. 재는 법은 위 재현 절차에서 한 줄씩 빼는 것
2. **`cfg.embed.max_chars` 를 낮춘다** (지금 1,600). 실데이터 평균이 1,212자이므로
   1,000 이면 대부분을 온전히 담으면서 버퍼 상한을 내린다. **검색 품질 영향을 먼저 재야 한다**
3. **큰 배치 뒤 자동 재시작.** `lt nightly` 가 임베딩을 끝낸 뒤
   `systemctl --user restart llama-embed` 를 부른다. 유닛 주석이 이미 사람에게
   그렇게 하라고 적고 있다 — **사람에게 시키는 것은 안 불린다**
4. **온디맨드 기동.** 상주를 없애면 이 문제 전체가 사라진다. 평상시 사용량은
   하루 수십 건이고 대량 백필은 밤에 몰린다. 대가는 첫 RAG 질의의 모델 로드 대기 —
   **그 시간을 재기 전에는 판단할 수 없다**

**4 가 근본이고 2 가 가장 값싸다.** 1 은 지금 넣은 것에 대한 정직성 문제라
다른 것보다 먼저 하는 게 맞다.
