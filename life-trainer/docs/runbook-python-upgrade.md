# 파이썬 판을 바꾸는 절차 — 그리고 **되돌리는 절차**

2026-09-07 에 **3.10.12 → 3.14.6** 으로 올렸다. 그때 실제로 밟은 순서 그대로이고,
다음 날 CI 가 알려 준 것(고정본 재생성 · 3.10 걷어내기)까지 반영했다.

## 무엇을 골랐고 왜

| | 선택 | 왜 |
|---|---|---|
| 판 | **3.14.6** | 그날의 최신 **안정판**. 3.15 는 베타라 24시간 도는 기기에 안 올린다 |
| 설치 방법 | **`uv`** (사용자 영역) | 시스템 파이썬을 안 건드린다 → §5 의 *"되돌릴 수 없는 것"* 이 아니게 된다 |
| 경로 | **`.venv` 그대로** | 45개 파일이 `.venv/bin` 을 가리킨다. 경로를 바꾸면 유닛·Makefile·문서가 전부 흔들린다 |
| 의존성 | **`constraints.txt` 그대로** | 규칙 §5 — *"옮길 때 버전을 고정한다. 옮기는 김에 올리면 무엇이 원인인지 못 가른다"* |

**41개가 전부 같은 판으로 깔렸다.** 인터프리터만 바뀌었다.

## 순서

```bash
cd life-trainer

# ① 새 판이 되는지 **옆에서** 먼저 본다 (여기서 틀리면 아무것도 안 바꾼 상태다)
uv venv --python 3.14 .venv314
uv pip install --python .venv314/bin/python -e '.[dev]' -c constraints.txt
.venv314/bin/python -m pytest tests/ -q          # 여기가 관문이다

# ② 서비스를 멈춘다 — 도는 중에 갈아 끼우지 않는다
systemctl --user stop lifetrainer-web lifetrainer-worker lifetrainer-slack

# ③ 옛것을 옆으로, 같은 자리에 새것을
rm -rf .venv314
mv .venv .venv310
uv venv --python 3.14 --relocatable .venv
uv pip install --python .venv/bin/python -e '.[dev]' -c constraints.txt

# ④ 올린다
systemctl --user start lifetrainer-web lifetrainer-worker lifetrainer-slack
```

## ★ 확인은 **경로가 아니라 실물**로 한다

`.venv/bin/python -V` 는 그 파일이 무엇인지 말할 뿐, **서비스가 무엇으로 도는지**는
말하지 않는다. 이 저장소는 같은 착각으로 이미 한 번 헤맸다(nvm/시스템 Node):

```bash
for u in lifetrainer-web lifetrainer-worker lifetrainer-slack; do
  pid=$(systemctl --user show "$u" -p MainPID --value)
  echo "$u  $(readlink -f /proc/$pid/exe)"
done
# → .../python3.14 여야 한다
```

그리고 `lt doctor`. 올린 직후 **항목 수가 줄고 노란불이 하나 떴다** —
그게 [백포트 사고](../HISTORY/2026-09-07-a-backport-that-only-existed-because-python-was-old.md)를
잡아 줬다. 그러니 올리기 전에 **`lt doctor` 를 한 번 찍어 두고** 올린 뒤와 비교한다.

## 되돌리는 절차

**옛 `.venv310` 의 명령 스크립트는 셔뱅이 `.venv/bin/python` 이라 못 쓴다** —
이름을 바꾸는 순간 깨진다. 그래서 되돌리기는 *복사*가 아니라 **다시 만들기**다.
`constraints.txt` 가 판을 전부 고정하고 있어 그대로 재현된다.

```bash
systemctl --user stop lifetrainer-web lifetrainer-worker lifetrainer-slack
cd life-trainer
rm -rf .venv
uv venv --python 3.13 --relocatable .venv      # 3.11 이상이면 아무 판이나 된다
uv pip install --python .venv/bin/python -e '.[dev]' -c constraints.txt
systemctl --user start lifetrainer-web lifetrainer-worker lifetrainer-slack
```

★ **되돌릴 판은 3.10 이 아니어도 된다.** 하루 동안은 `requires-python` 을 `>=3.10` 으로
두고 CI 로 3.10 을 같이 돌렸는데, 그 판단이 두 군데서 틀렸다 (2026-09-08 에 바로잡았다):

- **앱은 3.10 을 전혀 안 쓴다.** 서비스 셋도 타이머도 전부 `.venv`(3.14)다.
  즉 지키고 있던 것은 *"쓰는 것"* 이 아니라 *"쓸 수도 있는 것"* 이었다
- **첫 CI 실행에서 3.10 잡이 바로 실패했다.** 테스트가 `import tomllib` 을 하는데
  3.10 에는 없다. 아무도 안 쓰는 판을 지키느라 **빨간불이 상수가 될 뻔했다**
  (저장소 규칙 §1 — 안 꺼지는 신호는 신호가 아니다)

그래서 `requires-python` 은 **`>=3.11`**, CI 는 **3.14 하나**다.
되돌리기는 3.12·3.13 이면 성립한다 — 셋 다 `tomllib` 이 표준이고 uv 로 바로 깔린다.

## ★ 고정본은 **새 판에서 다시 뽑는다** (2026-09-08 에 배웠다)

옮길 때는 `constraints.txt` 를 그대로 썼다 — 규칙 §5(*"옮기는 김에 올리지 않는다"*)를
지키려고. 이 기기에서는 잘 깔렸다. **그런데 CI 의 설치가 7분 49초 걸렸다.**

원인은 그 고정본이 **3.10 에서 뽑은 것**이라, 몇 개가 3.14용 휠이 없어 CI 가
소스에서 컴파일한 것이다. PyPI 에 직접 물어 확인했다:

```
numpy      2.2.6   cp314 휠 없음  → 소스 컴파일
contourpy  1.3.2   cp314 휠 없음  → 소스 컴파일
나머지               휠 있음
```

**옮긴 다음에는 새 인터프리터에서 `make lock` 을 다시 돌린다.**
휠이 있는 최소 판으로 올리면 된다 (numpy 2.3.2+ · contourpy 1.3.3+).
순서가 중요하다 — *옮길 때는 고정, 옮긴 뒤에 재고정*이다.

## 잰 것 (2026-09-07, 이 기기)

```
3일치 롤업     3.10.12  3.16s   →   3.14.6  2.39s     (−24%)
기동 + import  3.10.12  0.15s   →   3.14.6  0.18s     (+0.03s)
```

★ 처음 잰 값은 **틀렸다.** `.venv310/bin/lt` 로 쟀는데 그 셔뱅이 `.venv/bin/python`
을 가리켜서 **둘 다 3.14 를 돌리고 있었다.** 인터프리터를 직접 지정해 다시 쟀다.
*경로가 아니라 실물을 본다* 는 규칙이 측정에도 그대로 적용된다.

## 다음에 올릴 때

`.venv310` 은 지워도 된다 — 되돌리기가 *다시 만들기*라 그 폴더에 의존하지 않는다.
남겨 두면 인터프리터 하나(약 100MB)를 계속 들고 있는 것이다.
