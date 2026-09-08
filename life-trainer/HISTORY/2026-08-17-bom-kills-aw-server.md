# UTF-8 BOM 하나가 aw-server 를 조용히 죽였다

- **발견**: 설정을 바꾼 뒤 서버가 안 뜨는데 **로그도 안 남았다.**
  콘솔에 직접 물려 실행(`&` 로)해서 트레이스백을 보고 확정.
- **증상**: `netstat` 에 `LISTENING` 줄이 아예 없고, 워처만 `SYN_SENT` 로 재시도 중.
  젯슨에서는 계속 `Connection refused`.

## 원인

```
UnicodeDecodeError: 'cp949' codec can't decode byte 0xbf in position 2:
illegal multibyte sequence
  File "aw_core\config.py", line 56, in load_config_toml
```

`position 2` 의 `0xbf` — **UTF-8 BOM**(`EF BB BF`)이다.

내가 준 명령이 원인이었다:

```powershell
Set-Content -Path $p -Value $out -Encoding UTF8     # ← Windows PowerShell 5.1 은 BOM 을 붙인다
```

파이썬 `aw-server` 는 설정 파일을 **시스템 기본 코덱**으로 읽는다. 한글 윈도우면 cp949 다.
cp949 로 `EF BB BF` 를 읽으려다 죽는다.

### 왜 찾기 고약했나

- **로그가 안 남는다.** 설정 파싱은 로거 초기화 **전에** 일어난다
- `Start-Process` 로 띄우면 창이 즉시 사라져 트레이스백을 못 본다
- aw-qt(트레이)는 멀쩡히 떠 있고 워처도 살아 있어서 "일부는 되는데" 처럼 보인다
- 증상이 나타나는 곳(젯슨의 `Connection refused`)과 원인(설정 파일 인코딩)이 멀다

## 수정

내용이 전부 ASCII 이므로 **`-Encoding ASCII`** 로 쓰면 BOM 이 붙지 않는다.

`scripts/setup-activitywatch-windows.ps1` 도 같은 버그를 갖고 있어서 고쳤다.
쓰고 나서 **앞 3바이트를 실제로 확인**하는 검증까지 넣었다:

```powershell
Set-Content -Path $cfgPath -Value $cfgBody -Encoding ASCII
$head = [byte[]](Get-Content $cfgPath -Encoding Byte -TotalCount 3)
if ($head[0] -eq 239 -and $head[1] -eq 187 -and $head[2] -eq 191) { Write-Err "BOM 이 붙었다" }
```

같은 스크립트에서 함께 고친 것 (전부 이 기기의 실측에서 나왔다):

- **서버 구현 자동 판별** — Rust(`config.toml`, 키 `address`, 최상위) vs
  파이썬(`aw-server.toml`, 키 `host`, `[server]` 안). 추측하지 말고 실재하는 파일로 판별
- **경로 중복 버그 #1068 실물 확인** — `…\activitywatch\activitywatch\…`.
  조사 때 "옛 버전에서 보고된 적 있다"고만 적어뒀는데 이 기기에 그대로 있었다
- **기존 파일을 덮어쓰지 않는다** — `[server]` 의 `host` 줄만 교체.
  통째로 쓰면 `[server-testing]` 등이 날아간다
- 파이썬 서버에는 `api_key` 기능이 없으므로 `-EnableApiKey` 를 무시하고 경고

## 방어

- 스크립트가 쓴 직후 BOM 을 검사한다 (위 코드)
- `docs/research/activitywatch.md` 에 파이썬 서버 경로·키를 추가해야 한다 — **미완**

## 교훈

**"UTF8" 이라는 이름을 믿지 마라.** Windows PowerShell 5.1 의 `-Encoding UTF8` 은
BOM 포함이다. PowerShell 7 은 BOM 없음이 기본이라 같은 스크립트가 환경에 따라 다르게 동작한다.

그리고 **안 뜨는 프로세스는 콘솔에 직접 물려라.** `Start-Process` 로 계속 재시도하면
"왜 안 뜨지"만 반복한다. `&` 로 실행해 트레이스백을 본 순간 5초 만에 끝났다.

일반화하면: **초기화 실패는 로그에 안 남는다.** 로거보다 먼저 죽기 때문이다.
로그가 비어 있다는 것 자체가 "설정/부팅 단계에서 죽었다"는 신호다.
