$ErrorActionPreference = 'Stop'
$botRunner = Join-Path $PSScriptRoot 'run_bot.py'
$botPidFile = Join-Path $PSScriptRoot 'runtime\process.json'
if (-not (Test-Path -LiteralPath $botPidFile)) { Write-Output 'No background bot process is recorded.'; exit 0 }
$botSaved = Get-Content -LiteralPath $botPidFile -Raw | ConvertFrom-Json
$botExisting = Get-CimInstance Win32_Process -Filter "ProcessId=$($botSaved.id)" -ErrorAction SilentlyContinue
if ($botExisting) {
    if (-not $botExisting.CommandLine -or -not $botExisting.CommandLine.Contains($botRunner)) {
        throw 'The recorded PID belongs to a different process. Nothing was stopped.'
    }
    Stop-Process -Id $botSaved.id
}
Remove-Item -LiteralPath $botPidFile
Write-Output 'Bot stopped. Plans and settings are saved.'
$botTunnelPidFile = Join-Path $PSScriptRoot 'runtime\tunnel.pid'
if (Test-Path -LiteralPath $botTunnelPidFile) {
    $botTunnelId = [int](Get-Content -LiteralPath $botTunnelPidFile -Raw).Trim()
    $botTunnel = Get-CimInstance Win32_Process -Filter "ProcessId=$botTunnelId" -ErrorAction SilentlyContinue
    if ($botTunnel -and $botTunnel.ExecutablePath -eq (Join-Path $PSScriptRoot 'runtime\tools\cloudflared.exe')) {
        Stop-Process -Id $botTunnelId
    }
    Remove-Item -LiteralPath $botTunnelPidFile
}
