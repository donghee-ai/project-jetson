# 버전 도장을 두 프로세스가 서로 되돌렸다 — 고쳐도 안 꺼지는 WARN 이 됐다

- **날짜**: 2026-09-04
- **깨진 가정**: **"마이그레이션을 적용했으면 DB 가 그렇게 적혀 있다."**

## 무슨 일이 있었나

008·009 를 적용한 뒤 `make status` 가 **`schema v7`** 이라고 했다. 코드 상수는 9고,
컬럼도 둘 다 실제로 생겨 있었다. 마이그레이션 마커도 `applied` 였다.

```
migration:008_doc_digested_at            applied
migration:009_doc_body_fetch_failed      applied
schema_version                           7        ← 이것만 뒤처져 있다
```

`lt doctor` 는 *"스키마 버전 7 (코드 기준 v9) — 마이그레이션 확인 필요"* 를 띄웠다.
**시키는 대로 `migrate()` 를 다시 돌려도 안 꺼졌다.**

## 원인 — 도장을 두 곳에서 찍고, 한 곳은 뒤로 찍었다

**`init_db()` 가 `open_db()` 마다 불리면서 자기 프로세스의 상수로 갈아엎고 있었다.**

```python
"INSERT INTO meta(...) VALUES ('schema_version', ?, ?) "
"ON CONFLICT(key) DO UPDATE SET value=excluded.value …"   # ← 무조건 덮는다
```

이 기기의 `lifetrainer-web`·`worker`·`slack` 은 **나흘째 떠 있었다.** 그 프로세스들이
메모리에 든 `SCHEMA_VERSION` 은 **7** 이다. 내가 9 로 올려 놔도, 그중 하나가 다음
요청에서 DB 를 여는 순간 다시 7 이 된다. **고친 쪽과 되돌리는 쪽이 동시에 돌았다.**

### 그리고 되돌아간 것을 고칠 방법이 없었다

`migrate()` 의 도장이 `if applied:` 안에 있었다. 008·009 는 이미 `applied` 라
**몇 번을 불러도 `applied == 0`** 이고, 그러면 도장 블록을 아예 안 탄다.

즉 *"마이그레이션은 다 적용됐는데 기록만 뒤처진 상태"* 에서 빠져나올 길이 없었다.
doctor 가 시키는 일을 그대로 해도 안 꺼진다 — [CLAUDE.md §1](../../CLAUDE.md) 의 1번
*"원인을 고쳐도 안 꺼지는 판정"* 그대로다.

## 수정

| 어디 | 무엇 |
|---|---|
| `init_db()` | `DO NOTHING` — **이미 적힌 값을 안 덮는다.** 값이 아예 없을 때만 처음 심는다 |
| `migrate()` | 도장을 `if applied:` 밖으로. **기록이 뒤처져 있으면 새로 적용한 게 없어도 찍는다** |
| 〃 | 단 **올리기만 한다** — 옛 코드가 새 DB 를 열었을 때 낮추면 방금 그 사고가 재발한다 |

**버전을 올리는 것은 `migrate()`·`baseline_migrations()` 의 일이다.**
`init_db` 는 스키마를 붓는 함수지 버전을 정하는 함수가 아니었다.

## ★ 아직 안 꺼졌다 — 서비스를 재시작해야 한다

수정은 소스에 있고 테스트도 붙었지만, **되돌리는 쪽이 아직 옛 코드로 돌고 있다.**
`make status` 는 이 커밋 시점에도 `v7` 이라고 답한다.

```bash
systemctl --user restart lifetrainer-web lifetrainer-worker lifetrainer-slack
```

*만들었다 ≠ 그게 불린다* 의 변종이다 — **고쳤다 ≠ 돌고 있는 것이 고쳐졌다.**

## 방어

`tests/test_migrate.py` 2건.

- `test_init_db_does_not_move_the_recorded_version_backwards` — 옛 상수를 든 프로세스가
  다시 열어도 기록이 안 내려간다
- `test_migrate_stamps_the_version_even_when_nothing_new_applies` — **기록만 뒤처진 DB 를
  고칠 수 있다.** 이게 없으면 빠져나올 길 없는 WARN 이 다시 생긴다

## 교훈

- [ ] **같은 값을 두 곳에서 쓰나?** 이 저장소의 반복 실패 2번(*파생값은 한 곳에서만*)이
      숫자가 아니라 **DB 행**에서 났다. 쓰는 쪽이 둘이면 마지막에 쓴 쪽이 이긴다
- [ ] **오래 떠 있는 프로세스가 옛 코드를 들고 있다.** 배포 = 파일 교체가 아니다.
      `lt doctor` 는 CLI 라 항상 새 코드인데, 되돌리는 쪽은 나흘 된 프로세스였다
- [ ] **"확인 필요" 라고 말하는 판정은 확인하면 꺼져야 한다.** 안 꺼지면 그건 판정이 아니라 소음이다
