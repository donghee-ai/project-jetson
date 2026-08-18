<#
.SYNOPSIS
    Windows PC 에 ActivityWatch 를 설치하고 Tailscale 경유 폴링이 가능하도록 설정한다.

.DESCRIPTION
    Life Trainer 의 Phase 1 은 이 PC 의 활동 데이터가 전부다.
    이 스크립트는 다음을 한다:

      1. ActivityWatch v0.14.0b3 (베타) 설치 — 이미 있으면 건너뜀
      2. aw-server 를 0.0.0.0:5600 에 바인딩
      3. Windows 방화벽 인바운드 규칙을 Tailscale 대역(100.64.0.0/10)으로만 허용
      4. 자동시작 확인
      5. 젯슨에서 접근 가능한지 확인용 명령 출력

    ※ 관리자 권한으로 실행해야 한다 (방화벽 규칙 때문).

.PARAMETER EnableApiKey
    aw-server 의 [auth] api_key 를 설정한다. 기본값은 끔.

    ⚠️ 켜기 전에 읽을 것:
    api_key 를 켜면 /api/0/info 를 제외한 모든 API 가 Bearer 토큰을 요구한다.
    이 PC 에서 도는 로컬 워처(aw-watcher-window, aw-watcher-afk)와 브라우저 확장도
    같은 API 를 쓰는데, 번들된 클라이언트가 그 키를 자동으로 실어 보내는지는
    버전에 따라 다르다. 못 보내면 데이터 수집이 통째로 멈춘다.

    그래서 기본 절차는 이렇다:
      (1) 먼저 api_key 없이 돌려서 데이터가 실제로 쌓이는지 확인한다
      (2) 그 다음에 -EnableApiKey 로 다시 실행하고, 워처가 여전히 기록하는지 재확인한다
    Tailscale 자체가 WireGuard 암호화 + 기기 인증이므로, 키 없이도 방화벽 규칙만으로
    충분히 안전하다. 키는 심층방어일 뿐이다.

.PARAMETER SkipInstall
    설치를 건너뛰고 설정·방화벽만 적용한다.

.EXAMPLE
    # 관리자 PowerShell 에서
    powershell -ExecutionPolicy Bypass -File .\setup-activitywatch-windows.ps1

.EXAMPLE
    # 데이터가 쌓이는 것을 확인한 뒤, 인증까지 켜고 싶을 때
    powershell -ExecutionPolicy Bypass -File .\setup-activitywatch-windows.ps1 -SkipInstall -EnableApiKey
#>

[CmdletBinding()]
param(
    [switch]$EnableApiKey,
    [switch]$SkipInstall
)

$ErrorActionPreference = 'Stop'

$AW_VERSION   = 'v0.14.0b3'
$AW_INSTALLER = "activitywatch-$AW_VERSION-windows-x86_64-setup.exe"
$AW_URL       = "https://github.com/ActivityWatch/activitywatch/releases/download/$AW_VERSION/$AW_INSTALLER"
$AW_PORT      = 5600

function Write-Step($msg) { Write-Host "`n=== $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "  [!!] $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "  [XX] $msg" -ForegroundColor Red }

# ── 0. 관리자 권한 확인 ────────────────────────────────────────
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Err "관리자 권한이 필요하다 (방화벽 규칙 생성)."
    Write-Host "  PowerShell 을 '관리자로 실행' 한 뒤 다시 돌려라." -ForegroundColor Yellow
    exit 1
}
Write-Ok "관리자 권한 확인"

# ── 1. Tailscale IP 확인 ──────────────────────────────────────
Write-Step "Tailscale 주소 확인"

$tsExe = Get-Command tailscale.exe -ErrorAction SilentlyContinue
if (-not $tsExe) {
    $candidate = "$env:ProgramFiles\Tailscale\tailscale.exe"
    if (Test-Path $candidate) { $tsExe = $candidate } else { $tsExe = $null }
} else {
    $tsExe = $tsExe.Source
}

$tsIp = $null
if ($tsExe) {
    try { $tsIp = (& $tsExe ip -4 2>$null | Select-Object -First 1).Trim() } catch { }
}

if ($tsIp) {
    Write-Ok "Tailscale IP: $tsIp"
} else {
    Write-Warn2 "Tailscale IP 를 찾지 못했다. 방화벽 규칙은 그대로 넣지만, 젯슨 쪽 설정의"
    Write-Warn2 "activitywatch.base_url 을 직접 확인해서 맞춰야 한다."
}

# ── 2. 설치 ───────────────────────────────────────────────────
$awQt = "$env:LOCALAPPDATA\Programs\ActivityWatch\aw-qt.exe"
if (-not (Test-Path $awQt)) {
    $awQt = "$env:ProgramFiles\ActivityWatch\aw-qt.exe"
}

if ($SkipInstall) {
    Write-Step "설치 건너뜀 (-SkipInstall)"
} elseif (Test-Path $awQt) {
    Write-Step "ActivityWatch 설치 확인"
    Write-Ok "이미 설치돼 있다: $awQt"
} else {
    Write-Step "ActivityWatch $AW_VERSION 다운로드 및 설치"
    $dl = Join-Path $env:TEMP $AW_INSTALLER

    Write-Host "  받는 중: $AW_URL"
    # 진행률 표시가 다운로드를 심하게 느리게 만든다
    $prev = $ProgressPreference; $ProgressPreference = 'SilentlyContinue'
    try {
        Invoke-WebRequest -Uri $AW_URL -OutFile $dl -UseBasicParsing
    } finally {
        $ProgressPreference = $prev
    }
    Write-Ok ("받음: {0:N1} MB" -f ((Get-Item $dl).Length / 1MB))

    Write-Host "  설치 중 (무인)..."
    # Inno Setup. StartMenuEntry 작업이 자동시작 바로가기를 만든다 (기본 선택됨)
    Start-Process -FilePath $dl `
        -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/TASKS=StartMenuEntry' `
        -Wait
    Write-Ok "설치 완료"

    $awQt = "$env:LOCALAPPDATA\Programs\ActivityWatch\aw-qt.exe"
    if (-not (Test-Path $awQt)) { $awQt = "$env:ProgramFiles\ActivityWatch\aw-qt.exe" }
}

if (-not (Test-Path $awQt)) {
    Write-Err "aw-qt.exe 를 찾지 못했다. 설치 경로를 직접 확인해라."
    exit 1
}

# ── 3. 한 번 띄워서 설정 디렉터리를 만들게 한다 ────────────────
Write-Step "설정 디렉터리 준비"

# ActivityWatch 는 서버 구현이 두 가지고 설정 경로·파일명·키가 전부 다르다.
# 실측(2026-08-17, DESKTOP-EXAMPLE)에서 파이썬 서버 + 경로 중복 버그가 함께 나왔다.
#
#   Rust   %LOCALAPPDATA%\activitywatch\aw-server-rust\config.toml     키: address (최상위)
#   Python %LOCALAPPDATA%\activitywatch\activitywatch\aw-server\aw-server.toml
#                                                                       키: host ([server] 안)
#
# 추측하지 말고 **실제로 있는 파일**을 찾아서 판별한다.
if (-not (Test-Path "$env:LOCALAPPDATA\activitywatch")) {
    Write-Host "  aw-qt 를 한 번 띄워 설정 디렉터리를 생성한다..."
    Start-Process -FilePath $awQt
    Start-Sleep -Seconds 20
}

$rustCandidates = @(
    "$env:LOCALAPPDATA\activitywatch\aw-server-rust\config.toml",
    "$env:LOCALAPPDATA\activitywatch\activitywatch\aw-server-rust\config.toml"
)
$pyCandidates = @(
    "$env:LOCALAPPDATA\activitywatch\activitywatch\aw-server\aw-server.toml",
    "$env:LOCALAPPDATA\activitywatch\aw-server\aw-server.toml"
)

$cfgPath = $null; $serverKind = $null
foreach ($c in $rustCandidates) { if (Test-Path $c) { $cfgPath = $c; $serverKind = 'rust'; break } }
if (-not $cfgPath) {
    foreach ($c in $pyCandidates) { if (Test-Path $c) { $cfgPath = $c; $serverKind = 'python'; break } }
}

# 실행 중인 프로세스로 교차 확인 (설정 파일이 아직 없을 때의 판별 근거)
if (-not $cfgPath) {
    if (Get-Process 'aw-server-rust' -ErrorAction SilentlyContinue) { $serverKind = 'rust' }
    elseif (Get-Process 'aw-server' -ErrorAction SilentlyContinue) { $serverKind = 'python' }
    else { $serverKind = 'rust' }
    $cfgPath = if ($serverKind -eq 'rust') { $rustCandidates[0] } else { $pyCandidates[0] }
    New-Item -ItemType Directory -Path (Split-Path $cfgPath) -Force | Out-Null
    Write-Warn2 "설정 파일이 없어 새로 만든다 ($serverKind): $cfgPath"
}

Write-Ok "서버 종류: $serverKind"
Write-Ok "설정 파일: $cfgPath"
if ($serverKind -eq 'python' -and $EnableApiKey) {
    Write-Warn2 "파이썬 aw-server 에는 api_key 기능이 없다. -EnableApiKey 를 무시한다."
    Write-Warn2 "이 경우 방화벽 규칙이 유일한 접근 제어다."
    $EnableApiKey = $false
}

# ── 4. aw-qt 정지 ─────────────────────────────────────────────
Write-Step "ActivityWatch 정지 (설정 변경 전)"
foreach ($p in 'aw-qt','aw-server','aw-server-rust','aw-watcher-window','aw-watcher-afk') {
    Get-Process -Name $p -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 3
Write-Ok "정지됨"

# ── 5. config.toml 작성 ───────────────────────────────────────
Write-Step "aw-server 설정 작성"

if (Test-Path $cfgPath) {
    $bak = "$cfgPath.bak-" + (Get-Date -Format 'yyyyMMdd-HHmmss')
    Copy-Item $cfgPath $bak
    Write-Ok "기존 설정 백업: $bak"
}

# 왜 0.0.0.0 인가 —
#   Tailscale IP 에만 바인딩하면 이 PC 에서 도는 워처들(localhost:5600 으로 붙는다)이
#   서버를 찾지 못해 수집이 통째로 멈춘다. 그래서 0.0.0.0 으로 열고,
#   외부 노출은 방화벽 규칙으로 Tailscale 대역만 남긴다.
$apiKeyLine = ''
$generatedKey = ''
if ($EnableApiKey) {
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $generatedKey = [Convert]::ToBase64String($bytes) -replace '[+/=]',''
    $apiKeyLine = "`n[auth]`napi_key = `"$generatedKey`"`n"
}

if ($serverKind -eq 'python') {
    # 파이썬 서버는 기존 파일의 [server] 섹션에서 host 줄만 바꾼다.
    # 통째로 덮어쓰면 [server-testing] 등 다른 섹션이 날아간다.
    $section = ''
    $touched = $false
    $out = foreach ($line in (Get-Content $cfgPath)) {
        if ($line -match '^\s*\[(.+?)\]\s*$') { $section = $Matches[1] }
        if ($section -eq 'server' -and $line -match '^\s*#?\s*host\s*=') {
            $touched = $true
            'host = "0.0.0.0"'
        } else { $line }
    }
    if (-not $touched) { $out = @('[server]', 'host = "0.0.0.0"') + $out }
    $cfgBody = $out
} else {
    $cfgBody = @(
        '# Life Trainer 용 설정 — setup-activitywatch-windows.ps1 이 생성',
        '#',
        '# Rust 서버는 키 이름이 host 가 아니라 address 다 (CLI 플래그만 --host).',
        '# 0.0.0.0 바인딩은 의도된 것이다: 로컬 워처(localhost)와 젯슨(Tailscale)이 둘 다 붙어야 한다.',
        '# 외부 차단은 Windows 방화벽 규칙(ActivityWatch-Tailscale-Only)이 담당한다.',
        'address = "0.0.0.0"',
        "port = $AW_PORT"
    )
    if ($apiKeyLine) { $cfgBody += @('', '[auth]', "api_key = `"$generatedKey`"") }
}

# ★ -Encoding UTF8 을 쓰지 마라. Windows PowerShell 5.1 은 BOM 을 붙이는데,
#   파이썬 aw-server 는 설정을 시스템 기본 코덱(한글 윈도우면 cp949)으로 읽어
#   BOM(EF BB BF)에서 즉사한다. 실제로 겪은 사고다:
#     UnicodeDecodeError: 'cp949' codec can't decode byte 0xbf in position 2
#   서버가 조용히 안 뜨고 워처만 SYN_SENT 로 재시도한다 — 원인 찾기가 고약하다.
#   내용이 전부 ASCII 이므로 ASCII 로 쓰면 BOM 이 붙지 않는다.
Set-Content -Path $cfgPath -Value $cfgBody -Encoding ASCII
Write-Ok "작성: $cfgPath"

$head = [byte[]](Get-Content $cfgPath -Encoding Byte -TotalCount 3)
if ($head.Length -ge 3 -and $head[0] -eq 239 -and $head[1] -eq 187 -and $head[2] -eq 191) {
    Write-Err "BOM 이 붙었다. 파이썬 서버라면 기동에 실패한다. 스크립트를 확인해라."
} else {
    Write-Ok "BOM 없음 확인"
}
if ($EnableApiKey) {
    Write-Warn2 "api_key 를 켰다. 아래 §요약의 키를 젯슨 설정에 넣어야 한다."
    Write-Warn2 "그리고 로컬 워처가 계속 기록하는지 반드시 재확인해라 (§6 검증)."
}

# ── 6. 방화벽 ─────────────────────────────────────────────────
Write-Step "Windows 방화벽 규칙"

$ruleName = 'ActivityWatch-Tailscale-Only'
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule -ErrorAction SilentlyContinue

# Tailscale CGNAT 대역만 허용
New-NetFirewallRule -DisplayName $ruleName `
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort $AW_PORT `
    -RemoteAddress '100.64.0.0/10' `
    -Profile Any `
    -Description 'Life Trainer: allow aw-server polling from tailnet only' | Out-Null
Write-Ok "허용 규칙 생성 (TCP $AW_PORT ← 100.64.0.0/10)"

# 그 외 전부 차단하는 명시적 deny. 규칙 순서에 기대지 않기 위함이다.
$denyName = 'ActivityWatch-Block-Others'
Get-NetFirewallRule -DisplayName $denyName -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule -ErrorAction SilentlyContinue

New-NetFirewallRule -DisplayName $denyName `
    -Direction Inbound -Action Block -Protocol TCP -LocalPort $AW_PORT `
    -RemoteAddress 'Any' `
    -Profile Any `
    -Description 'Life Trainer: deny aw-server from everywhere except tailnet' | Out-Null
Write-Ok "차단 규칙 생성 (그 외 전부)"
Write-Host "  ※ Windows 방화벽은 Block 이 Allow 보다 우선한다. 두 규칙이 겹치는" -ForegroundColor DarkGray
Write-Host "     Tailscale 대역에서도 Block 이 이길 수 있으므로 §7 에서 실제로 확인한다." -ForegroundColor DarkGray

# ── 7. 재기동 + 검증 ──────────────────────────────────────────
Write-Step "ActivityWatch 재기동"
Start-Process -FilePath $awQt
Write-Host "  서버가 뜰 때까지 대기..."

$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$AW_PORT/api/0/info" -TimeoutSec 3
        $ok = $true
        Write-Ok "aw-server 응답. hostname=$($r.hostname) version=$($r.version)"
        break
    } catch { }
}

if (-not $ok) {
    Write-Err "aw-server 가 뜨지 않았다. 트레이 아이콘에서 로그를 확인해라."
    exit 1
}

Write-Step "버킷 확인 (워처가 실제로 기록하는지)"
Start-Sleep -Seconds 5
try {
    $headers = @{}
    if ($EnableApiKey) { $headers['Authorization'] = "Bearer $generatedKey" }
    $buckets = Invoke-RestMethod -Uri "http://127.0.0.1:$AW_PORT/api/0/buckets/" -Headers $headers -TimeoutSec 5
    $names = $buckets.PSObject.Properties.Name
    if ($names.Count -eq 0) {
        Write-Warn2 "버킷이 아직 없다. 워처가 첫 이벤트를 보내기까지 1~2분 걸릴 수 있다."
    } else {
        Write-Ok "버킷 $($names.Count) 개:"
        foreach ($n in $names) { Write-Host "       - $n" }
        $hasWindow = $names -match 'aw-watcher-window'
        $hasAfk    = $names -match 'aw-watcher-afk'
        if (-not $hasWindow) { Write-Warn2 "window 워처 버킷이 없다. 트레이에서 활성 상태를 확인해라." }
        if (-not $hasAfk)    { Write-Warn2 "afk 워처 버킷이 없다." }
    }
} catch {
    Write-Err "버킷 조회 실패: $_"
    if ($EnableApiKey) {
        Write-Warn2 "api_key 를 켠 직후라면, 로컬 워처가 키를 못 보내 기록이 멈췄을 수 있다."
        Write-Warn2 "그렇다면 config.toml 의 [auth] 절을 지우고 aw-qt 를 재시작해라."
    }
}

# ── 8. 요약 ───────────────────────────────────────────────────
Write-Step "요약 — 젯슨 쪽에 넣을 값"

$shownIp = if ($tsIp) { $tsIp } else { '<이 PC 의 Tailscale IP>' }

Write-Host ""
Write-Host "  Life_Trainer/config/lifetrainer.toml 의 [activitywatch] 절:" -ForegroundColor White
Write-Host ""
Write-Host "    base_url = `"http://${shownIp}:$AW_PORT`"" -ForegroundColor Green
if ($EnableApiKey) {
    Write-Host "    api_key  = `"$generatedKey`"" -ForegroundColor Green
} else {
    Write-Host "    api_key  = `"`"" -ForegroundColor Green
}
Write-Host ""
Write-Host "  젯슨에서 연결 확인:" -ForegroundColor White
Write-Host "    curl -s http://${shownIp}:$AW_PORT/api/0/info" -ForegroundColor Green
if ($EnableApiKey) {
    Write-Host "    curl -s -H 'Authorization: Bearer $generatedKey' http://${shownIp}:$AW_PORT/api/0/buckets/" -ForegroundColor Green
} else {
    Write-Host "    curl -s http://${shownIp}:$AW_PORT/api/0/buckets/" -ForegroundColor Green
}
Write-Host ""
Write-Host "  브라우저 URL 까지 수집하려면 확장을 설치해라 (선택):" -ForegroundColor White
Write-Host "    Chrome/Edge: https://chromewebstore.google.com/detail/nglaklhklhcoonedhgnpgddginnjdadi" -ForegroundColor DarkGray
Write-Host "    Firefox    : https://addons.mozilla.org/firefox/addon/aw-watcher-web/" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  창 제목만으로도 분류는 되지만, URL 이 있으면 정확도가 눈에 띄게 올라간다." -ForegroundColor DarkGray
Write-Host ""
