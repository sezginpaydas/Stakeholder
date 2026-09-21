$Root = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $Root 'server.pid'
$Port = 8080
$stopped = $false

if (Test-Path $PidFile) {
    $id = (Get-Content $PidFile -Raw).Trim()
    $proc = Get-Process -Id $id -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -like 'python*' }
    if ($proc) {
        Stop-Process -Id $proc.Id -Force
        $stopped = $true
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

# Yedek: PID dosyasi yoksa portu dinleyen python surecini bul
$conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($c in $conns) {
    $proc = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -like 'python*' }
    if ($proc) {
        Stop-Process -Id $proc.Id -Force
        $stopped = $true
    }
}

if ($stopped) {
    Write-Host 'Gorev Yoneticisi durduruldu. Veriler data.json icinde saklandi.' -ForegroundColor Green
} else {
    Write-Host 'Calisan bir Gorev Yoneticisi bulunamadi.' -ForegroundColor Yellow
}
