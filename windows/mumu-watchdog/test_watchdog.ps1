# SPDX-License-Identifier: MIT
# Safe health and scheduled-task recovery tests for MuMu Watchdog.

[CmdletBinding()]
param(
    [ValidateSet('Health', 'TaskRecovery', 'Freeze')]
    [string]$Mode = 'Health',

    [string]$ManagerPath,

    [ValidateRange(0, 999)]
    [int]$InstanceIndex = 0,

    [ValidateRange(1, 65535)]
    [int]$AdbPort = 16384,

    [ValidateRange(3, 30)]
    [int]$ShellTimeoutSeconds = 6,

    [ValidateRange(5, 60)]
    [int]$ScreenshotTimeoutSeconds = 10
)

$ErrorActionPreference = 'Stop'
$TaskName = "MuMu Watchdog - Instance $InstanceIndex"
$InstalledScript = Join-Path $env:LOCALAPPDATA (
    'MuMuWatchdog\mumu_watchdog.ps1'
)
$PowerShellPath = Join-Path $env:SystemRoot (
    'System32\WindowsPowerShell\v1.0\powershell.exe'
)

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)

    if (-not $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )) {
        throw 'Run Windows PowerShell as Administrator and try again.'
    }
}

function Assert-HealthInputs {
    if (-not $ManagerPath) {
        throw '-ManagerPath is required for the Health test.'
    }

    if (-not (Test-Path -LiteralPath $ManagerPath -PathType Leaf)) {
        throw "MuMuManager.exe not found: $ManagerPath"
    }

    if (-not (Test-Path -LiteralPath $InstalledScript -PathType Leaf)) {
        throw "Installed watchdog script not found: $InstalledScript"
    }
}

function Invoke-WatchdogHealthCheck {
    $arguments = @(
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', $InstalledScript,
        '-ManagerPath', $ManagerPath,
        '-InstanceIndex', "$InstanceIndex",
        '-AdbPort', "$AdbPort",
        '-ShellTimeoutSeconds', "$ShellTimeoutSeconds",
        '-ScreenshotTimeoutSeconds', "$ScreenshotTimeoutSeconds",
        '-Check'
    )
    $output = @(& $PowerShellPath @arguments 2>&1)

    [pscustomobject]@{
        ExitCode = $LASTEXITCODE
        Output = $output
    }
}

if ($Mode -eq 'Freeze') {
    throw (
        'Freeze mode is disabled in v1.1.2. Suspending every thread in ' +
        'MuMuNxDevice can deadlock the shared MuMu control plane and is not ' +
        'a safe recovery test. Use Health and TaskRecovery instead.'
    )
}

if ($Mode -eq 'Health') {
    Assert-HealthInputs
    $health = Invoke-WatchdogHealthCheck
    $health.Output | ForEach-Object { Write-Host $_ }
    exit $health.ExitCode
}

Assert-Administrator

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop

if ($task.State -ne 'Running') {
    throw "$TaskName is not running. Test cancelled."
}

$before = Get-ScheduledTaskInfo -TaskName $TaskName
$beforeRunTime = $before.LastRunTime

Write-Host "Stopping $TaskName to test its one-minute supervisor trigger."
Stop-ScheduledTask -TaskName $TaskName

$recovered = $false

for ($i = 1; $i -le 20; $i++) {
    Start-Sleep -Seconds 5

    $currentTask = Get-ScheduledTask -TaskName $TaskName
    $currentInfo = Get-ScheduledTaskInfo -TaskName $TaskName

    $statusText = '{0} State={1} LastRunTime={2} NextRunTime={3}' -f @(
        (Get-Date -Format 'HH:mm:ss'),
        $currentTask.State,
        $currentInfo.LastRunTime,
        $currentInfo.NextRunTime
    )
    Write-Host $statusText

    if (
        $currentTask.State -eq 'Running' -and
        $currentInfo.LastRunTime -gt $beforeRunTime
    ) {
        $recovered = $true
        break
    }
}

if (-not $recovered) {
    Start-ScheduledTask -TaskName $TaskName
    throw 'Task did not restart within 100 seconds; it was started manually.'
}

Write-Host 'Scheduled-task recovery test passed.' -ForegroundColor Green
exit 0
