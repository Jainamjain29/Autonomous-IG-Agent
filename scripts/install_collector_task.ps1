<#
.SYNOPSIS
    Registers the "StreamOvate Insight Collector" scheduled task in Windows Task Scheduler.
.DESCRIPTION
    Runs .venv\Scripts\python.exe -m insight.collect every 30 minutes from the repository root,
    only when the user is logged on, and starts a missed run when the PC wakes.
#>

$ErrorActionPreference = "Stop"

$TaskName = "StreamOvate Insight Collector"
$RepoDir = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $RepoDir ".venv\Scripts\python.exe"

if (-not (Test-Path $PythonExe)) {
    Write-Error "Virtual environment Python executable not found at: $PythonExe"
}

# Action: run python.exe -m insight.collect in the repository root directory
$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "-m insight.collect" `
    -WorkingDirectory $RepoDir

# Trigger: repeat every 30 minutes indefinitely
$Trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 30)

# Settings: only when user logged on, start missed run on wake (StartWhenAvailable)
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Automated 30-minute metric collector for StreamOvate Insight Agent" `
    -Force

Write-Host "Scheduled task '$TaskName' has been successfully registered."
Write-Host "Executable:       $PythonExe"
Write-Host "Arguments:        -m insight.collect"
Write-Host "Working Directory:$RepoDir"
Write-Host "Schedule:         Every 30 minutes (interactive logon, start when available on wake)"
