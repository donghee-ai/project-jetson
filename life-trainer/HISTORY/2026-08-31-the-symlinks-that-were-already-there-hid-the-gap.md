# 이미 걸려 있던 심링크가 **설치 스크립트의 구멍을 가려 주고 있었다**

- **발견**: 폴더 재구조화 때문에 유닛 심링크를 전부 지웠다가 다시 걸었다.
  `operate/tools/install.sh` 를 돌린 뒤 `jetson-daily-check.timer` 를 켜려 했더니
  **"Unit file does not exist"** 였다. `jetson-heartbeat.timer` 도 같았다.
  둘 다 `desired-state.txt` 에 `always` 로 적혀 있는데 설치 스크립트가 안 걸었다.

- **증상**: 없었다 — **지우기 전까지는.** 두 타이머는 몇 달째 정상적으로 돌고 있었다.
  누가 예전에 **손으로** 심링크를 걸어 뒀고, 스크립트가 그걸 안 만든다는 사실은
  아무도 다시 확인할 이유가 없었다. 심링크가 있는 한 스크립트를 다시 돌려도 티가 안 난다.

- **원인**: 깨진 가정은 **"목록은 `desired-state.txt` 한 곳뿐이다"** 였다.

  2026-08-28 에 정확히 이 문제를 고쳤다. 그날 `life-trainer/scripts/install-units.sh` ·
  `uninstall-units.sh` · `verify-boot.sh` 가 **전부 같은 파일을 읽게** 바꿨고,
  그 스크립트 머리말에 이렇게 적었다:

  > 무엇을 켜는지는 **여기 안 적는다** — `systemd/desired-state.txt` 하나가 정본이고
  > … 전에는 세 곳이 각자 목록을 들고 있어 서로 달랐다 (2026-08-28).

  **그런데 고친 것은 Life Trainer 쪽뿐이었다.** 공유 자산 쪽(`operate/`)은
  자기 `desired-state.txt` 를 갖고 있었지만 — `verify-boot.sh` 는 그걸 읽고 있었지만 —
  **설치 스크립트는 유닛 이름 둘을 본문에 박은 채였다.** 목록이 두 곳이 됐고,
  둘 중 **읽는 쪽만** 맞았다.

  ```
  desired-state.txt   llama-server · openclaw-gateway · jetson-daily-check
                      jetson-heartbeat · llama-server-restart      ← 5개
  install.sh 본문     llama-server · llama-server-restart          ← 2개
  verify-boot.sh      desired-state.txt 를 읽는다                  ← 5개를 본다
  ```

  `verify-boot` 이 5개를 보고 5개가 다 있었으니 **초록불이었다.**
  검사는 *"desired-state 와 실제가 맞나"* 를 물었고, 그 답은 계속 예였다.
  아무도 *"실제가 어떻게 그렇게 됐나"* 를 안 물었다.

- **영향**: 재플래시나 다른 기기에서 이 저장소를 세우면 **감시 타이머 둘이 안 뜬다.**
  그 둘은 일일 점검과 dead-man heartbeat 다 — **기기가 죽은 것을 알려 주는 경로**가
  조용히 빠진 채 세워진다. 복구 상황에서 가장 나쁜 종류의 누락이다.

- **수정**: `operate/tools/install.sh` 가 `operate/systemd/` 의 유닛을 전부 심링크하고,
  `desired-state.txt` 를 읽어 `always` 와 `optional=<변수>` 조건대로 켜게 했다.
  Life Trainer 쪽과 같은 모양이 됐다.

- **방어**: 새 검사는 안 만들었다. **`verify-boot.sh` 가 이미 옳은 질문을 하고 있었고,
  실패는 그 검사가 못 본 자리가 아니라 "손으로 한 번 맞춰 두면 검사가 통과한다" 였다.**
  대신 설치 경로에서 목록이 하나가 되게 했다 — 검사를 늘리는 대신 **어긋날 자리를 없앴다.**

  ★ 진짜 방어는 **재부팅**이다. 이 저장소가 이미 그렇게 정해 뒀다
  (`docs/plans/restructure-2026-08-31.md` §5: *"재기동만으로는 부족하다"*).

- **교훈**: **"한 곳에만 적는다"를 고칠 때는, 같은 일을 하는 다른 쪽도 같이 센다.**
  같은 날 [백업 스크립트에서 똑같은 모양](2026-08-31-the-backup-kept-what-its-own-check-created.md)
  이 나왔다 — `make-recovery-bundle.sh` 는 고쳤는데 `backup.sh` 는 안 고쳤다.
  **하루에 두 번이면 그건 우연이 아니라 습관이다.** 결함을 고칠 때 `git grep` 으로
  같은 패턴을 세는 것을 절차에 넣는다.

  그리고 **"지금 잘 돌고 있다"는 설치 경로가 옳다는 증거가 아니다.**
  돌고 있는 상태는 스크립트가 만든 것일 수도, 사람이 한 번 손으로 맞춘 것일 수도 있다.
  둘을 가르는 유일한 방법은 **지우고 다시 세워 보는 것**이다.
