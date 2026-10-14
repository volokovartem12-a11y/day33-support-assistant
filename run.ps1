# run.ps1 — ассистент поддержки «Пульт» (день 33).
#
#   .\run.ps1 -Mode test                       # проверки на имитации (ключ и интернет не нужны)
#   .\run.ps1 -Mode chat                       # чат с ассистентом; внутри: /ticket T-1001, /tickets, /exit
#   .\run.ps1 -Mode service -Port 8080         # мини-сервис: POST /api/ask, нужен ключ X-Api-Key
#
# Ключ AITunnel и ключ сервиса спрашиваются скрытым вводом и в файлы не сохраняются.
# Если Windows блокирует запуск:  powershell -ExecutionPolicy Bypass -File .\run.ps1

param(
    [ValidateSet("chat", "service", "test")]
    [string]$Mode = "chat",
    [int]$Port = 8080,
    [string]$Model = ""
)

Set-Location $PSScriptRoot
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

function Find-Python {
    $installed = @(Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | ForEach-Object { $_.FullName })
    foreach ($candidate in @("py", "python") + $installed) {
        try {
            $output = & $candidate --version 2>&1
            if ($output -match "Python \d") { return $candidate }
        }
        catch { }
    }
    return $null
}

function Read-SecretToEnv($name, $prompt) {
    if (Test-Path "env:$name") { return }
    $secure = Read-Host $prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { Set-Item -Path "env:$name" -Value ([Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

$py = Find-Python
if (-not $py) { Write-Host "Python не найден. Установите: winget install Python.Python.3.12 --scope user" -ForegroundColor Red; exit 1 }
Write-Host "Python: $py" -ForegroundColor Green

& $py -c "import numpy" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Ставлю numpy (один раз)..." -ForegroundColor Cyan
    & $py -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Write-Host "Не удалось установить numpy. Проверьте интернет и повторите." -ForegroundColor Red; exit 1 }
}

if ($Mode -eq "test") {
    & $py .\test_day33.py
    Write-Host "`nОтчёт: $PSScriptRoot\output\test_report.md" -ForegroundColor Green
    exit $LASTEXITCODE
}

if ($Mode -eq "chat") {
    $args2 = @(".\support\assistant.py")
    if ($Model) { $args2 += @("--model", $Model) }
    Read-SecretToEnv "AITUNNEL_API_KEY" "Ключ AITunnel (ввод скрыт)"
    & $py @args2
    exit $LASTEXITCODE
}

# service
Read-SecretToEnv "AITUNNEL_API_KEY" "Ключ AITunnel (ввод скрыт)"
if (-not (Test-Path "env:SUPPORT_API_KEY")) {
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $env:SUPPORT_API_KEY = [Convert]::ToBase64String($bytes)
    Write-Host "Ключ сервиса для клиентов (сохраните сейчас, в файлы не пишется):" -ForegroundColor Yellow
    Write-Host $env:SUPPORT_API_KEY -ForegroundColor Yellow
}
& $py .\support\service.py --port $Port
exit $LASTEXITCODE
