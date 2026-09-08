# 8B 서버가 LLM 요청 한 건마다 약 70~105MB 씩 자라고 반납하지 않는다

- **번호**: `0019`
- **발견일**: 2026-08-31 (전역 OOM 을 파다가)
- **상태**: 🟡 **h — 증상은 막아 뒀다.** 매일 02:00·06:10 재기동으로 최대 크기를
  약 9.4GB 에 묶는다. **원인은 그대로**이고 `[embed]` 쪽 [`0018`](h-0018-the-buffers-grow-with-input-length-and-never-return.md) 과 같은 계열이다

## 무엇이 사실인가 (측정됨)

**요청이 있을 때만 자란다. 놀 때는 안 자란다.**

```
                              8B 크기            LLM 호출   요청당
08-29 15:00 → 08-30 18:12    9,048 → 12,296MB    31건     104.8MB
08-31 09:32 → 09:55          6,818 →  8,939MB    30건      70.7MB

08-30 06:10 ~ 18:12 (전역 OOM 까지)              0건       ← 그 구간 호출 없음
```

두 구간 모두 **요약(`summarize_doc`) 1건에 70~105MB**. `0018`(임베딩, 실문서 1건당
약 29MB)과 같은 얼굴이다 — **llama.cpp 가 요청마다 버퍼를 잡고 OS 에 돌려주지 않는다.**

★ 처음에 **"놀면서 자란다"고 잘못 읽었다.** 추정 증가율(130MB/h)로 계산한 값이었고,
실제로 세어 보니 호출 0건인 구간이 있었다. **시간이 아니라 요청이 변수다.**

## 이 병의 대가 — 08-30 18:12 전역 OOM

```
llama-server 8B   RSS 6,577 + 스왑 5,719 = 12,296MB   ← 혼자 12.3GB
llama-embed       RSS 2,020 + 스왑     0
나머지 107개      RSS 거의 0 — 전부 스왑으로 밀려나 있었다
스왑              7,833 / 8,009MB (97.8%)
```

경위는 [HISTORY](../../HISTORY/2026-08-31-the-swap-i-cleared-as-harmless-killed-the-machine.md).

## 재현

```bash
B=$(pgrep -x llama-server | while read p; do grep -qa Qwen3-8B /proc/$p/cmdline && echo $p; done | head -1)
awk '/VmRSS|VmSwap/{printf "%s %.0fMB\n",$1,$2/1024}' /proc/$B/status   # 전

# 요약 30건을 돌린다 (야간 배치와 같은 부하)
life-trainer/.venv/bin/lt nightly && sleep 700

awk '/VmRSS|VmSwap/{printf "%s %.0fMB\n",$1,$2/1024}' /proc/$B/status   # 후
life-trainer/.venv/bin/python -c "
import sqlite3; c=sqlite3.connect('file:life-trainer/data/lifetrainer.db?mode=ro',uri=True)
print(c.execute('SELECT COUNT(*) FROM llm_call WHERE purpose=\"summarize_doc\" AND created_at>?',
      (__import__('time').time()-1200,)).fetchone())"
```

## 메모리 구조 (2026-08-31, 가동 1시간 · 요약 30건 처리 후)

```
2,058MB  [익명mmap]   ← 이게 자란다. 929 → 2,008 → 2,058MB 로 단조 증가를 직접 봤다
  334MB  /dmabuf:     ← GPU(nvmap)
  232MB  [heap]       ← glibc 메인 아레나
   64MB × 8개         ← glibc 보조 아레나 (HEAP_MAX_SIZE)
스레드 12개 · `-t` 미지정 · MALLOC_* 미설정 · THP = always
```

**매핑 개수는 안 늘고 하나가 커진다** — 새로 `mmap` 하는 게 아니라 기존 영역을 넓힌다.

## 아직 모르는 것

- **2,058MB 가 glibc 아레나인지 ggml 자체 풀인지 모른다.** 보조 아레나는 64MB 단위인데
  이건 한 덩어리다. `/proc/maps` 가 인접한 익명 VMA 를 한 줄로 합쳐 보여주는 것일 수도,
  llama.cpp 의 버퍼 풀일 수도 있다. **여기가 갈리면 아래 후보 ①의 효과가 갈린다**
- 증가가 **입력 길이에 비례하는지** 안 쟀다. `0018`(임베딩)은 그랬다.
  요약 프롬프트는 중앙값 332토큰인데 편차가 크다(최대 2,385)
- **평탄해지는 지점이 있는지** 모른다. `0018` 은 평탄해졌다. 8B 는 42시간·수백 건까지
  자라는 것만 봤다

## 고칠 것 — 후보

1. **`MALLOC_ARENA_MAX=2` · `MALLOC_TRIM_THRESHOLD_=131072`** — `0018` 에서 임베딩에
   넣은 것과 같다. 8B 에는 **안 넣었다.**
   - 위험 ⓐ **효과가 없을 수 있다** — 위 "아직 모르는 것" 1번
   - 위험 ⓑ 스레드 12개가 아레나 2개를 나눠 쓴다 → 경합
   - 위험 ⓒ **`TRIM_THRESHOLD` 를 명시하면 glibc 의 `MMAP_THRESHOLD` 동적 조정까지
     같이 꺼진다.** 중간 크기 할당이 계속 mmap 으로 가 syscall 이 늘 수 있다
   - 위험 ⓓ 임베딩은 `-ngl 0`(CPU 추론), 8B 는 `-ngl 99`(GPU) — **할당 주체가 달라
     임베딩의 결과가 그대로 옮겨간다는 보장이 없다**
2. **THP 를 `always` → `madvise`** — 전역 sysctl 이라 sudo 가 필요하고 8B·임베딩 둘 다
   영향을 받는다. `0018` 에도 같은 항목이 있다
3. **`MemoryMax` 를 8B 에도 건다** — 기기 전체를 끌어내리는 대신 자기만 죽는다
   (임베딩이 그 구조). 다만 8B 는 로드가 10~30초라 자가재시작의 대가가 크다
4. **상류 확인** — llama.cpp `b1-a94d563`(2026-08-13). 이후 릴리스에 관련 수정이
   있는지 안 봤다

## ★ 왜 지금 ①을 안 넣었나 — 측정을 가르기 위해

02:00·06:10 재기동은 **2026-08-31 에 붙였고 아직 한 번도 안 돌았다.**
지금 `MALLOC` 을 같이 넣으면 다음 아침 결과가 **재기동 덕인지 MALLOC 덕인지 못 가른다.**

**이 저장소는 이미 한 번 그랬다** — `0018` 에서 세 줄을 한꺼번에 넣어
`dmabuf 646 → 0` 만 단정할 수 있고 `MALLOC` 두 줄의 기여는 지금도 모른다.
그게 `0018` 의 "아직 모르는 것" 1번이다.

```
1단계  재기동만 돈 결과를 잰다        ← 이미 붙어 있다. 추가 비용 0
       CSV(llama-server-size.csv) + llm_call 로 요청당 증가량 재확인
2단계  MALLOC 두 줄을 넣고 한 밤 더    → 70.7MB/건 이 얼마로 바뀌는지가 그대로 답
```

**기준선이 좋다** — 야간 배치가 매일 정확히 요약 30건이고, 지금 값이 **건당 70.7MB** 다.
한 밤이면 효과가 숫자로 나온다.

지금 당장의 위험은 낮다. 재기동이 최대 크기를 약 9.4GB 로 묶고, 전역 OOM 이 난
12,296MB 까지 약 2.9GB 여유가 있다.
