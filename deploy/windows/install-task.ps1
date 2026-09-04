[CmdletBinding()]
param(
    [string]$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")),
    [string]$ConfigPath = (Join-Path $ProjectDir "config.toml"),
    [string]$TaskName = "CamVault"
)

$ErrorActionPreference = "Stop"
$uv = (Get-Command uv -ErrorAction Stop).Source
& $uv sync --project $ProjectDir --no-dev
$arguments = "run --project `"$ProjectDir`" --no-sync --no-dev camvault serve -c `"$ConfigPath`""
$action = New-ScheduledTaskAction -Execute $uv -Argument $arguments -WorkingDirectory $ProjectDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Write-Warning "The task runs as SYSTEM. Set camera passwords, CAMVAULT_PLAYBACK_TOKEN, and WebDAV credentials (when used) as machine-level environment variables before installing. Optionally set PYTHONDONTWRITEBYTECODE=1 at machine scope to avoid Python bytecode writes."
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -User "SYSTEM" `
    -RunLevel Highest `
    -Force | Out-Null

Write-Host "Installed scheduled task '$TaskName'. Start it with: Start-ScheduledTask -TaskName '$TaskName'"
