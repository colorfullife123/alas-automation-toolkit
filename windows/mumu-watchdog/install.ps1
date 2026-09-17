# SPDX-License-Identifier: MIT
# Install or upgrade MuMu Watchdog for the current interactive Windows user.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ManagerPath,

    [ValidateRange(0, 999)]
    [int]$InstanceIndex = 0,

    [ValidateRange(1, 65535)]
    [int]$AdbPort = 16384,

    [ValidateRange(10, 300)]
    [int]$PollSeconds = 30,

    [ValidateRange(2, 10)]
    [int]$FailuresToRestart = 3,

    [ValidateRange(3, 30)]
    [int]$ShellTimeoutSeconds = 6,

    [ValidateRange(5, 60)]
    [int]$ScreenshotTimeoutSeconds = 10,

    [ValidateRange(60, 600)]
    [int]$StartupGraceSeconds = 180,

    [ValidateRange(2, 5)]
    [int]$RecoveryConfirmations = 3
)

$ErrorActionPreference = 'Stop'

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)

    if (-not $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )) {
        throw 'Run Windows PowerShell as Administrator and try again.'
    }
}

function Escape-Xml {
    param([Parameter(Mandatory = $true)][string]$Value)
    [Security.SecurityElement]::Escape($Value)
}

Assert-Administrator

if (-not (Test-Path -LiteralPath $ManagerPath -PathType Leaf)) {
    throw "MuMuManager.exe not found: $ManagerPath"
}

$ManagerPath = (Resolve-Path -LiteralPath $ManagerPath).ProviderPath
$SourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SourceScript = Join-Path $SourceDir 'mumu_watchdog.ps1'

if (-not (Test-Path -LiteralPath $SourceScript -PathType Leaf)) {
    throw "Missing file next to installer: $SourceScript"
}

$InstallDir = Join-Path $env:LOCALAPPDATA 'MuMuWatchdog'
$InstalledScript = Join-Path $InstallDir 'mumu_watchdog.ps1'
$TaskName = "MuMu Watchdog - Instance $InstanceIndex"
$TaskUri = '\' + $TaskName
$PowerShellPath = Join-Path $env:SystemRoot (
    'System32\WindowsPowerShell\v1.0\powershell.exe'
)

$adbListening = @(
    Get-NetTCPConnection -LocalPort $AdbPort -State Listen -ErrorAction SilentlyContinue
).Count -gt 0

if ($adbListening) {
    $preflightPassed = $false
    $preflightOutput = @()

    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $preflightArgs = @(
            '-NoProfile',
            '-ExecutionPolicy', 'Bypass',
            '-File', $SourceScript,
            '-ManagerPath', $ManagerPath,
            '-InstanceIndex', "$InstanceIndex",
            '-AdbPort', "$AdbPort",
            '-ShellTimeoutSeconds', "$ShellTimeoutSeconds",
            '-ScreenshotTimeoutSeconds', "$ScreenshotTimeoutSeconds",
            '-Check'
        )
        $preflightOutput = @(& $PowerShellPath @preflightArgs 2>&1)
        $preflightExitCode = $LASTEXITCODE

        if ($preflightExitCode -eq 0) {
            $preflightPassed = $true
            break
        }

        if (($preflightOutput -join ' ') -notmatch 'still running|recovery is already') {
            break
        }

        Start-Sleep -Seconds 3
    }

    if (-not $preflightPassed) {
        $preflightText = ($preflightOutput | Out-String).Trim()
        throw (
            'Strong health-check preflight failed; existing installation ' +
            "was not changed. $preflightText"
        )
    }

    Write-Host 'ADB shell and screencap preflight passed.' -ForegroundColor Green
}
else {
    Write-Warning (
        "ADB port $AdbPort is not listening. Installation will continue, " +
        'but the strong health probe could not be tested.'
    )
}

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null

$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupDir = Join-Path $InstallDir "backup-install-$timestamp"
$backupScript = Join-Path $backupDir 'mumu_watchdog.ps1'
$backupTask = Join-Path $backupDir 'task.xml'
$previousTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$previousTaskXml = $null
$hadInstalledScript = Test-Path -LiteralPath $InstalledScript

if ($previousTask -or $hadInstalledScript) {
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
}

if ($previousTask) {
    $previousTaskXml = Export-ScheduledTask -TaskName $TaskName
    [IO.File]::WriteAllText(
        $backupTask,
        $previousTaskXml,
        [Text.Encoding]::Unicode
    )
}

if ($hadInstalledScript) {
    Copy-Item -LiteralPath $InstalledScript -Destination $backupScript -Force
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$userSid = $identity.User.Value
$accountName = $identity.Name
$firstTimerRun = (Get-Date).AddMinutes(1).ToString(
    "yyyy-MM-dd'T'HH:mm:ss"
)

$actionArguments = (
    '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden ' +
    '-ExecutionPolicy Bypass -File "{0}" ' +
    '-ManagerPath "{1}" -InstanceIndex {2} -AdbPort {3} ' +
    '-PollSeconds {4} -FailuresToRestart {5} ' +
    '-ShellTimeoutSeconds {6} -ScreenshotTimeoutSeconds {7} ' +
    '-StartupGraceSeconds {8} -RecoveryConfirmations {9}'
) -f @(
    $InstalledScript,
    $ManagerPath,
    $InstanceIndex,
    $AdbPort,
    $PollSeconds,
    $FailuresToRestart,
    $ShellTimeoutSeconds,
    $ScreenshotTimeoutSeconds,
    $StartupGraceSeconds,
    $RecoveryConfirmations
)

$sidXml = Escape-Xml $userSid
$accountXml = Escape-Xml $accountName
$uriXml = Escape-Xml $TaskUri
$commandXml = Escape-Xml $PowerShellPath
$argumentsXml = Escape-Xml $actionArguments
$workingDirectoryXml = Escape-Xml $InstallDir

$taskXml = @"
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Monitor MuMu instance $InstanceIndex with bounded ADB probes and verified safe recovery.</Description>
    <URI>$uriXml</URI>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>$sidXml</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <AllowHardTerminate>true</AllowHardTerminate>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <RestartOnFailure>
      <Count>3</Count>
      <Interval>PT1M</Interval>
    </RestartOnFailure>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <IdleSettings>
      <Duration>PT10M</Duration>
      <WaitTimeout>PT1H</WaitTimeout>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <Enabled>true</Enabled>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
  </Settings>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>PT1M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>$firstTimerRun</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
    <LogonTrigger>
      <UserId>$accountXml</UserId>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>$commandXml</Command>
      <Arguments>$argumentsXml</Arguments>
      <WorkingDirectory>$workingDirectoryXml</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

try {
    if ($previousTask) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

        for ($i = 1; $i -le 10; $i++) {
            $currentTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

            if (-not $currentTask -or $currentTask.State -ne 'Running') {
                break
            }

            Start-Sleep -Seconds 1
        }

        $currentTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

        if ($currentTask -and $currentTask.State -eq 'Running') {
            throw 'Existing watchdog task did not stop within 10 seconds.'
        }
    }

    Copy-Item -LiteralPath $SourceScript -Destination $InstalledScript -Force

    Register-ScheduledTask -TaskName $TaskName -Xml $taskXml -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 5

    $task = Get-ScheduledTask -TaskName $TaskName
    $info = Get-ScheduledTaskInfo -TaskName $TaskName

    if ($task.State -ne 'Running') {
        throw "Task state is $($task.State), expected Running."
    }

    Write-Host 'MuMu Watchdog v1.1.2 installed successfully.' -ForegroundColor Green
    Write-Host "Task: $TaskName"
    Write-Host "Script: $InstalledScript"
    Write-Host "Log: $(Join-Path $InstallDir ("instance-{0}.log" -f $InstanceIndex))"
    Write-Host "Next supervisor trigger: $($info.NextRunTime)"
    Write-Host ''
    Write-Host 'Health check command:'

    $healthCommand = (
        '& "{0}" -NoProfile -ExecutionPolicy Bypass -File "{1}" ' +
        '-ManagerPath "{2}" -InstanceIndex {3} -AdbPort {4} ' +
        '-ShellTimeoutSeconds {5} -ScreenshotTimeoutSeconds {6} -Check'
    ) -f @(
        $PowerShellPath,
        $InstalledScript,
        $ManagerPath,
        $InstanceIndex,
        $AdbPort,
        $ShellTimeoutSeconds,
        $ScreenshotTimeoutSeconds
    )
    Write-Host $healthCommand
}
catch {
    $failure = $_.Exception.Message

    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

    if ($previousTaskXml) {
        $restoreXml = [regex]::Replace(
            $previousTaskXml,
            '^\s*(?:\uFEFF)?<\?xml[^?]*\?>\s*',
            ''
        )

        Register-ScheduledTask -TaskName $TaskName -Xml $restoreXml -Force | Out-Null
        Start-ScheduledTask -TaskName $TaskName
    }
    else {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    }

    if ($hadInstalledScript) {
        Copy-Item -LiteralPath $backupScript -Destination $InstalledScript -Force
    }
    elseif (Test-Path -LiteralPath $InstalledScript) {
        Remove-Item -LiteralPath $InstalledScript -Force
    }

    throw "Installation failed; previous configuration restored. $failure"
}
