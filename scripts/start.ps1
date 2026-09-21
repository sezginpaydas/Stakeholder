$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $Root 'server.pid'
$LogFile = Join-Path $Root 'server.log'
$Port = 8080
$Url = "http://localhost:$Port"

function Get-ServerProcess {
    if (-not (Test-Path $PidFile)) { return $null }
    $id = (Get-Content $PidFile -Raw).Trim()
    if (-not $id) { return $null }
    return Get-Process -Id $id -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -like 'python*' }
}

$running = Get-ServerProcess
if ($running) {
    Write-Host "Gorev Yoneticisi zaten calisiyor (PID $($running.Id)) -> $Url" -ForegroundColor Yellow
    Start-Process $Url
    exit 0
}
Remove-Item $PidFile -Force -ErrorAction SilentlyContinue

$python = $null
foreach ($name in 'pythonw.exe', 'python.exe', 'py.exe') {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { $python = $cmd.Source; break }
}
if (-not $python) {
    Write-Host 'Python bulunamadi. https://www.python.org adresinden kurun.' -ForegroundColor Red
    exit 1
}

Start-Process -FilePath $python -ArgumentList "`"$(Join-Path $Root 'server.py')`"" -WorkingDirectory $Root -WindowStyle Hidden

$ok = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 250
    try {
        Invoke-WebRequest "http://127.0.0.1:$Port/login" -UseBasicParsing -TimeoutSec 2 | Out-Null
        $ok = $true
        break
    } catch {}
}

if (-not $ok) {
    Write-Host 'Sunucu baslatilamadi.' -ForegroundColor Red
    if (Test-Path $LogFile) { Get-Content $LogFile -Tail 5 }
    exit 1
}

Write-Host ''
Write-Host "Gorev Yoneticisi calisiyor: $Url" -ForegroundColor Green
$ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
    Select-Object -ExpandProperty IPAddress
foreach ($ip in $ips) { Write-Host "Ag uzerinden erisim:     http://${ip}:$Port" }
Write-Host 'Durdurmak icin: durdur.bat'
Start-Process $Url
