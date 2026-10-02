<#
.SYNOPSIS
    Unregisters the "StreamOvate Insight Collector" scheduled task from Windows Task Scheduler.
#>

$ErrorActionPreference = "Stop"

$TaskName = "StreamOvate Insight Collector"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Scheduled task '$TaskName' has been unregistered and removed."
} else {
    Write-Host "Scheduled task '$TaskName' was not found."
}
