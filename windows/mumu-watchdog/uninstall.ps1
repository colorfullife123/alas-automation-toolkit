# SPDX-License-Identifier: MIT
# Remove one MuMu Watchdog scheduled task. Files and logs are kept by default.

[CmdletBinding()]
param(
    [ValidateRange(0, 999)]
    [int]$InstanceIndex = 0,

    [switch]$RemoveFiles
)

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)

if (-not $principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)) {
    throw 'Run Windows PowerShell as Administrator and try again.'
}

$taskName = "MuMu Watchdog - Instance $InstanceIndex"
$installDir = Join-Path $env:LOCALAPPDATA 'MuMuWatchdog'
$installedScript = Join-Path $installDir 'mumu_watchdog.ps1'
$instanceLog = Join-Path $installDir ("instance-{0}.log" -f $InstanceIndex)
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue

if ($task) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Removed scheduled task: $taskName"
}
else {
    Write-Host "Scheduled task not found: $taskName"
}

if ($RemoveFiles) {
    if (Test-Path -LiteralPath $instanceLog) {
        Remove-Item -LiteralPath $instanceLog -Force
        Write-Host "Removed log: $instanceLog"
    }

    $remainingTasks = @(
        Get-ScheduledTask -ErrorAction SilentlyContinue |
            Where-Object {
                $_.TaskName -like 'MuMu Watchdog - Instance *'
            }
    )

    if ($remainingTasks.Count -eq 0 -and (Test-Path $installedScript)) {
        Remove-Item -LiteralPath $installedScript -Force
        Write-Host "Removed script: $installedScript"
    }
    elseif ($remainingTasks.Count -gt 0) {
        Write-Host 'Shared script kept because another watchdog task exists.'
    }
}
else {
    Write-Host 'Installed script and logs were kept.'
    Write-Host 'Run again with -RemoveFiles to remove unused local files.'
}
