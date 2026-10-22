$ErrorActionPreference = 'Stop'
$botRuntime = Join-Path $PSScriptRoot 'runtime'
$botRunner = Join-Path $PSScriptRoot 'run_bot.py'
$botPidFile = Join-Path $botRuntime 'process.json'

if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.env'))) {
    throw 'Create .env from .env.example and fill BOT_TOKEN first. See README.md.'
}
New-Item -ItemType Directory -Force -Path $botRuntime | Out-Null
if (Test-Path -LiteralPath $botPidFile) {
    $botSaved = Get-Content -LiteralPath $botPidFile -Raw | ConvertFrom-Json
    $botExisting = Get-CimInstance Win32_Process -Filter "ProcessId=$($botSaved.id)" -ErrorAction SilentlyContinue
    if ($botExisting -and $botExisting.CommandLine -and $botExisting.CommandLine.Contains($botRunner)) {
        Write-Output "Bot is already running (PID $($botSaved.id))."
        exit 0
    }
}

$botCandidates = @(
    (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
    (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
)
$botSystemPython = Get-Command python.exe -ErrorAction SilentlyContinue
if ($botSystemPython -and $botSystemPython.Source -notlike '*\WindowsApps\*') {
    $botCandidates += $botSystemPython.Source
}
$botPython = $botCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $botPython) { throw 'Python 3.11+ was not found. Install Python and follow README.md.' }

$env:PYTHONUTF8 = '1'
$botProcess = Start-Process -FilePath $botPython -ArgumentList @('-u', ('"{0}"' -f $botRunner)) `
    -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $botRuntime 'stdout.log') `
    -RedirectStandardError (Join-Path $botRuntime 'stderr.log')
@{ id = $botProcess.Id; started = [DateTime]::UtcNow.ToString('o') } |
    ConvertTo-Json | Set-Content -LiteralPath $botPidFile -Encoding UTF8
Start-Sleep -Seconds 2
$botProcess.Refresh()
if ($botProcess.HasExited) {
    Get-Content -LiteralPath (Join-Path $botRuntime 'stderr.log') -Tail 12
    throw 'Bot exited. Check .env and runtime/stderr.log.'
}
Write-Output "Bot started in background (PID $($botProcess.Id)). Open https://t.me/caled_bot_ai"
Write-Output 'You can close this window. Use stop-bot.cmd to stop the bot.'
