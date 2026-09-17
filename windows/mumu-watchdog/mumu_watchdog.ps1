# SPDX-License-Identifier: MIT
# MuMu Watchdog - https://github.com/colorfullife123/mumu-watchdog
# Monitor one MuMu 12/15 instance and recover it after repeated ADB failures.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ManagerPath,

    [Parameter(Mandatory = $true)]
    [ValidateRange(0, 999)]
    [int]$InstanceIndex,

    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 65535)]
    [int]$AdbPort,

    [switch]$Check,

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
$ScriptVersion = '1.1.2'

if (-not (Test-Path -LiteralPath $ManagerPath -PathType Leaf)) {
    throw "MuMuManager.exe not found: $ManagerPath"
}

$ManagerPath = (Resolve-Path -LiteralPath $ManagerPath).ProviderPath
$ManagerDir = Split-Path -Parent $ManagerPath
$ControllerPath = Join-Path $ManagerDir 'MuMuNxMain.exe'
$LogDir = Join-Path $env:LOCALAPPDATA 'MuMuWatchdog'
$LogPath = Join-Path $LogDir ("instance-{0}.log" -f $InstanceIndex)
$RecoveryMarkerPath = Join-Path $LogDir ("instance-{0}.recovering" -f $InstanceIndex)
$CommandMutexName = "Local\MuMuWatchdog-Manager"
$MonitorMutexName = "Local\MuMuWatchdog-Monitor-$InstanceIndex"

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

function Write-Log {
    param([Parameter(Mandatory = $true)][string]$Text)

    $message = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Text
    Add-Content -LiteralPath $LogPath -Value $message -Encoding UTF8
    Write-Host $message
}

function Stop-ProcessTree {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    $taskkillPath = Join-Path $env:SystemRoot 'System32\taskkill.exe'
    $killer = $null

    try {
        if (Test-Path -LiteralPath $taskkillPath -PathType Leaf) {
            $killer = Start-Process -FilePath $taskkillPath -ArgumentList @(
                '/PID', "$ProcessId", '/T', '/F'
            ) -WindowStyle Hidden -PassThru

            if (-not $killer.WaitForExit(5000)) {
                try {
                    $killer.Kill()
                }
                catch {
                }
            }
        }
    }
    catch {
    }
    finally {
        if ($killer) {
            $killer.Dispose()
        }
    }

    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue

    try {
        Wait-Process -Id $ProcessId -Timeout 3 -ErrorAction SilentlyContinue
    }
    catch {
    }
}

function Invoke-MuMu {
    param(
        [Parameter(Mandatory = $true)][string[]]$CliArgs,
        [Parameter(Mandatory = $true)][ValidateRange(1, 120)][int]$TimeoutSeconds
    )

    $commandMutex = New-Object System.Threading.Mutex(
        $false,
        $CommandMutexName
    )
    $ownsMutex = $false
    $process = $null
    $token = [Guid]::NewGuid().ToString('N')
    $stdoutPath = Join-Path $LogDir ("manager-$token.out")
    $stderrPath = Join-Path $LogDir ("manager-$token.err")

    try {
        try {
            $ownsMutex = $commandMutex.WaitOne(2000)
        }
        catch [System.Threading.AbandonedMutexException] {
            $ownsMutex = $true
        }

        if (-not $ownsMutex) {
            return [pscustomobject]@{
                Ok = $false
                TimedOut = $false
                ExitCode = $null
                Output = 'another MuMuManager command is still running'
            }
        }

        $process = Start-Process -FilePath $ManagerPath -ArgumentList $CliArgs -WorkingDirectory $ManagerDir -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
        $timedOut = -not $process.WaitForExit($TimeoutSeconds * 1000)

        if ($timedOut) {
            $processId = $process.Id
            Stop-ProcessTree -ProcessId $processId
            [void]$process.WaitForExit(3000)
        }
        else {
            [void]$process.WaitForExit()
            $process.Refresh()
        }

        $stdout = if (Test-Path -LiteralPath $stdoutPath) {
            [IO.File]::ReadAllText($stdoutPath)
        }
        else {
            ''
        }
        $stderr = if (Test-Path -LiteralPath $stderrPath) {
            [IO.File]::ReadAllText($stderrPath)
        }
        else {
            ''
        }
        $output = ((@($stdout, $stderr) | Where-Object {
            -not [string]::IsNullOrWhiteSpace($_)
        }) -join ' ').Trim()

        if ($timedOut) {
            if ($output) {
                $output = "timeout after $TimeoutSeconds seconds; process tree terminated; partial output: $output"
            }
            else {
                $output = "timeout after $TimeoutSeconds seconds; process tree terminated"
            }

            return [pscustomobject]@{
                Ok = $false
                TimedOut = $true
                ExitCode = $null
                Output = $output
            }
        }

        return [pscustomobject]@{
            Ok = ($process.ExitCode -eq 0)
            TimedOut = $false
            ExitCode = $process.ExitCode
            Output = $output
        }
    }
    catch {
        return [pscustomobject]@{
            Ok = $false
            TimedOut = $false
            ExitCode = $null
            Output = $_.Exception.Message
        }
    }
    finally {
        if ($process) {
            $process.Dispose()
        }

        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue

        if ($ownsMutex) {
            try {
                [void]$commandMutex.ReleaseMutex()
            }
            catch {
            }
        }

        $commandMutex.Dispose()
    }
}

function Get-TargetProcess {
    $escapedIndex = [regex]::Escape([string]$InstanceIndex)
    $nxPattern = '(?i)(?:^|\s)--vm(?:\s+|=)"?MuMuPlayer-[^\s"]+-{0}"?(?:\s|$)' -f $escapedIndex
    $legacyPattern = '(?i)(?:^|\s)(?:-v|--vmindex)(?:\s+|=)?{0}(?:\s|$)' -f $escapedIndex

    @(
        Get-CimInstance Win32_Process -Filter (
            "Name = 'MuMuNxDevice.exe' OR " +
            "Name = 'MuMuPlayer.exe' OR " +
            "Name = 'NemuPlayer.exe'"
        ) | Where-Object {
            if (-not $_.CommandLine) {
                return $false
            }

            if ($_.Name -eq 'MuMuNxDevice.exe') {
                return $_.CommandLine -match $nxPattern
            }

            return $_.CommandLine -match $legacyPattern
        }
    )
}

function Get-AllInstanceProcesses {
    @(
        Get-CimInstance Win32_Process -Filter (
            "Name = 'MuMuNxDevice.exe' OR " +
            "Name = 'MuMuPlayer.exe' OR " +
            "Name = 'NemuPlayer.exe'"
        )
    )
}

function Get-LocalAdbConnections {
    @(
        Get-NetTCPConnection -LocalPort $AdbPort -State Listen -ErrorAction SilentlyContinue
    )
}

function Test-LocalAdbPort {
    @(Get-LocalAdbConnections).Count -gt 0
}

function Test-RecoveryInProgress {
    if (-not (Test-Path -LiteralPath $RecoveryMarkerPath -PathType Leaf)) {
        return $false
    }

    try {
        $marker = Get-Item -LiteralPath $RecoveryMarkerPath

        if (((Get-Date) - $marker.LastWriteTime).TotalMinutes -gt 10) {
            Remove-Item -LiteralPath $RecoveryMarkerPath -Force -ErrorAction SilentlyContinue
            return $false
        }

        $ownerText = [IO.File]::ReadAllText($RecoveryMarkerPath).Trim()
        $ownerId = 0

        if ([int]::TryParse($ownerText, [ref]$ownerId)) {
            if (Get-Process -Id $ownerId -ErrorAction SilentlyContinue) {
                return $true
            }
        }
    }
    catch {
    }

    Remove-Item -LiteralPath $RecoveryMarkerPath -Force -ErrorAction SilentlyContinue
    return $false
}

function Test-MuMuManagerReply {
    param([Parameter(Mandatory = $true)]$Reply)

    if ($Reply.Output -match '"errcode"\s*:\s*(-?\d+)') {
        return ([int]$Matches[1] -eq 0)
    }

    return [bool]$Reply.Ok
}

function Test-MuMu {
    param([switch]$IgnoreRecoveryMarker)

    if (-not $IgnoreRecoveryMarker -and (Test-RecoveryInProgress)) {
        return [pscustomobject]@{
            Healthy = $false
            ScreenshotMs = $null
            Reason = 'recovery is already in progress'
        }
    }

    $targets = @(Get-TargetProcess)

    if ($targets.Count -ne 1) {
        return [pscustomobject]@{
            Healthy = $false
            ScreenshotMs = $null
            Reason = "expected one instance process; found $($targets.Count)"
        }
    }

    $connections = @(Get-LocalAdbConnections)

    if ($connections.Count -eq 0) {
        return [pscustomobject]@{
            Healthy = $false
            ScreenshotMs = $null
            Reason = "local ADB port $AdbPort is not listening"
        }
    }

    if ($targets[0].Name -eq 'MuMuNxDevice.exe') {
        $owners = @($connections | Select-Object -ExpandProperty OwningProcess -Unique)

        if ($owners -notcontains [int]$targets[0].ProcessId) {
            return [pscustomobject]@{
                Healthy = $false
                ScreenshotMs = $null
                Reason = "ADB port $AdbPort is not owned by target PID $($targets[0].ProcessId)"
            }
        }
    }

    try {
        $targetProcess = Get-Process -Id $targets[0].ProcessId -ErrorAction Stop

        if ($targetProcess.MainWindowHandle -ne 0 -and -not $targetProcess.Responding) {
            return [pscustomobject]@{
                Healthy = $false
                ScreenshotMs = $null
                Reason = 'MuMu window is not responding'
            }
        }
    }
    catch {
        return [pscustomobject]@{
            Healthy = $false
            ScreenshotMs = $null
            Reason = "MuMu process vanished: $($_.Exception.Message)"
        }
    }

    $shell = Invoke-MuMu -CliArgs @(
        'adb', '-v', "$InstanceIndex", 'shell', 'echo', 'MUMU_WATCHDOG_OK'
    ) -TimeoutSeconds $ShellTimeoutSeconds

    if ($shell.TimedOut -or $shell.Output -notmatch 'MUMU_WATCHDOG_OK') {
        return [pscustomobject]@{
            Healthy = $false
            ScreenshotMs = $null
            ShellExitCode = $shell.ExitCode
            ScreenshotExitCode = $null
            Reason = (
                "ADB shell failed (exit=$($shell.ExitCode), " +
                "timedOut=$($shell.TimedOut)): $($shell.Output)"
            )
        }
    }

    $screenshotTimer = [Diagnostics.Stopwatch]::StartNew()
    $screenshot = Invoke-MuMu -CliArgs @(
        'adb', '-v', "$InstanceIndex",
        'shell', 'screencap', '-p', '/dev/null',
        '&&', 'echo', 'MUMU_SCREENSHOT_OK'
    ) -TimeoutSeconds $ScreenshotTimeoutSeconds
    $screenshotTimer.Stop()
    $screenshotMs = [int][Math]::Round(
        $screenshotTimer.Elapsed.TotalMilliseconds
    )

    if (
        $screenshot.TimedOut -or
        $screenshot.Output -notmatch 'MUMU_SCREENSHOT_OK'
    ) {
        return [pscustomobject]@{
            Healthy = $false
            ScreenshotMs = $screenshotMs
            ShellExitCode = $shell.ExitCode
            ScreenshotExitCode = $screenshot.ExitCode
            Reason = (
                "ADB screencap probe failed after $screenshotMs ms " +
                "(exit=$($screenshot.ExitCode), " +
                "timedOut=$($screenshot.TimedOut)): $($screenshot.Output)"
            )
        }
    }

    return [pscustomobject]@{
        Healthy = $true
        ScreenshotMs = $screenshotMs
        ShellExitCode = $shell.ExitCode
        ScreenshotExitCode = $screenshot.ExitCode
        Reason = (
            'ADB shell and screencap success markers, port ownership, ' +
            'and process checks passed'
        )
    }
}

function Wait-ForInstanceExit {
    param(
        [Parameter(Mandatory = $true)][int]$OldProcessId,
        [ValidateRange(1, 60)][int]$TimeoutSeconds = 25
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)

    while ((Get-Date) -lt $deadline) {
        $oldProcess = Get-Process -Id $OldProcessId -ErrorAction SilentlyContinue
        $currentTargets = @(Get-TargetProcess)

        if (-not $oldProcess -and $currentTargets.Count -eq 0) {
            return $true
        }

        Start-Sleep -Milliseconds 500
    }

    return $false
}

function Reset-MuMuControlPlane {
    $remainingInstances = @(Get-AllInstanceProcesses)

    if ($remainingInstances.Count -gt 0) {
        Write-Log (
            "Controller reset skipped because $($remainingInstances.Count) " +
            'other MuMu instance process(es) are still running'
        )
        return $false
    }

    $managerPrefix = $ManagerDir.TrimEnd('\') + '\'
    $controlProcesses = @(
        Get-CimInstance Win32_Process -Filter (
            "Name = 'MuMuManager.exe' OR Name = 'MuMuNxMain.exe'"
        ) | Where-Object {
            if ([string]::IsNullOrWhiteSpace($_.ExecutablePath)) {
                return $false
            }

            try {
                return [IO.Path]::GetFullPath($_.ExecutablePath).StartsWith(
                    $managerPrefix,
                    [StringComparison]::OrdinalIgnoreCase
                )
            }
            catch {
                return $false
            }
        }
    )

    foreach ($controlProcess in $controlProcesses) {
        Write-Log "Resetting control process $($controlProcess.Name) PID $($controlProcess.ProcessId)"

        if ($controlProcess.Name -eq 'MuMuManager.exe') {
            Stop-ProcessTree -ProcessId ([int]$controlProcess.ProcessId)
        }
        else {
            Stop-Process -Id ([int]$controlProcess.ProcessId) -Force -ErrorAction SilentlyContinue
        }
    }

    Start-Sleep -Seconds 3

    if (Test-Path -LiteralPath $ControllerPath -PathType Leaf) {
        Start-Process -FilePath $ControllerPath -WorkingDirectory $ManagerDir | Out-Null
        Write-Log 'MuMu main controller restarted'
        Start-Sleep -Seconds 8
    }
    else {
        Write-Log "MuMuNxMain.exe not found at $ControllerPath; launch will rely on MuMuManager"
    }

    return $true
}

function Start-TargetInstance {
    $targets = @(Get-TargetProcess)

    if ($targets.Count -gt 1) {
        throw "Cannot launch: target process is ambiguous ($($targets.Count) matches)"
    }

    if ($targets.Count -eq 1) {
        Write-Log "Instance auto-started with PID $($targets[0].ProcessId); duplicate launch skipped"
        return
    }

    $launch = Invoke-MuMu -CliArgs @(
        'control', '-v', "$InstanceIndex", 'launch'
    ) -TimeoutSeconds 20

    if (Test-MuMuManagerReply -Reply $launch) {
        Write-Log "Launch request accepted: $($launch.Output)"
        return
    }

    Start-Sleep -Seconds 5
    $targetsAfterTimeout = @(Get-TargetProcess)

    if ($targetsAfterTimeout.Count -eq 1) {
        Write-Log (
            "Launch command did not return cleanly ($($launch.Output)), " +
            "but new PID $($targetsAfterTimeout[0].ProcessId) appeared; verifying health"
        )
        return
    }

    throw "MuMuManager launch failed: $($launch.Output)"
}

function Wait-ForMuMuRecovery {
    $deadline = (Get-Date).AddSeconds($StartupGraceSeconds)
    $confirmations = 0
    $lastReason = ''
    $nextProgressLog = [datetime]::MinValue
    $readySince = $null
    $seenProcessId = $null

    while ((Get-Date) -lt $deadline) {
        $targets = @(Get-TargetProcess)
        $portListening = Test-LocalAdbPort

        if ($targets.Count -eq 1 -and $targets[0].ProcessId -ne $seenProcessId) {
            $seenProcessId = [int]$targets[0].ProcessId
            Write-Log "New instance process detected: PID $seenProcessId"
        }

        if ($targets.Count -eq 1 -and $portListening) {
            if ($null -eq $readySince) {
                $readySince = Get-Date
                Write-Log 'New PID and ADB listener detected; allowing them to settle'
            }

            if (((Get-Date) - $readySince).TotalSeconds -ge 8) {
                $health = Test-MuMu -IgnoreRecoveryMarker

                if ($health.Healthy) {
                    $confirmations++
                    Write-Log (
                        "Recovery health confirmation $confirmations/" +
                        "$RecoveryConfirmations (screencap=$($health.ScreenshotMs)ms)"
                    )

                    if ($confirmations -ge $RecoveryConfirmations) {
                        return $true
                    }
                }
                else {
                    $confirmations = 0

                    if (
                        $health.Reason -ne $lastReason -or
                        (Get-Date) -ge $nextProgressLog
                    ) {
                        Write-Log "Waiting for healthy Android: $($health.Reason)"
                        $lastReason = $health.Reason
                        $nextProgressLog = (Get-Date).AddSeconds(30)
                    }
                }
            }
        }
        else {
            $readySince = $null
            $confirmations = 0

            if ((Get-Date) -ge $nextProgressLog) {
                Write-Log (
                    "Waiting for startup: target processes=$($targets.Count), " +
                    "ADB listening=$portListening"
                )
                $nextProgressLog = (Get-Date).AddSeconds(30)
            }
        }

        Start-Sleep -Seconds 5
    }

    return $false
}

function Restart-MuMu {
    if (Test-RecoveryInProgress) {
        throw 'Another recovery is already in progress'
    }

    [IO.File]::WriteAllText(
        $RecoveryMarkerPath,
        [string]$PID,
        [Text.Encoding]::ASCII
    )

    try {
        Write-Log (
            "Safe recovery started for instance $InstanceIndex " +
            "(local ADB port $AdbPort)"
        )

        $targets = @(Get-TargetProcess)

        if ($targets.Count -gt 1) {
            throw "Target process is ambiguous ($($targets.Count) matches)"
        }

        if ($targets.Count -eq 1) {
            $oldProcessId = [int]$targets[0].ProcessId
            $connections = @(Get-LocalAdbConnections)

            if (
                $targets[0].Name -eq 'MuMuNxDevice.exe' -and
                $connections.Count -gt 0
            ) {
                $owners = @(
                    $connections |
                        Select-Object -ExpandProperty OwningProcess -Unique
                )

                if ($owners -notcontains $oldProcessId) {
                    throw (
                        "Refusing to terminate PID $oldProcessId because ADB " +
                        "port $AdbPort is owned by $($owners -join ',')"
                    )
                }
            }

            Write-Log "Stopping exact unhealthy instance PID $oldProcessId"
            Stop-ProcessTree -ProcessId $oldProcessId

            if (-not (Wait-ForInstanceExit -OldProcessId $oldProcessId)) {
                throw "Old instance PID $oldProcessId did not exit cleanly"
            }

            Write-Log "Old instance PID $oldProcessId fully exited"
        }
        else {
            Write-Log 'No target instance process remains; continuing crash recovery'
        }

        $controllerWasReset = Reset-MuMuControlPlane

        if (-not $controllerWasReset) {
            Write-Log 'Continuing with per-instance launch without shared controller reset'
        }

        Start-TargetInstance

        if (-not (Wait-ForMuMuRecovery)) {
            throw (
                "MuMu did not pass $RecoveryConfirmations consecutive health " +
                "checks within $StartupGraceSeconds seconds"
            )
        }

        Write-Log 'MuMu recovery confirmed; watchdog re-armed'
    }
    finally {
        Remove-Item -LiteralPath $RecoveryMarkerPath -Force -ErrorAction SilentlyContinue
    }
}

if ($Check) {
    $test = Test-MuMu
    $listening = Test-LocalAdbPort
    $targets = @(Get-TargetProcess)
    $recovering = Test-RecoveryInProgress

    Write-Output (
        "Version=$ScriptVersion Host=$env:COMPUTERNAME Manager=$ManagerPath " +
        "Instance=$InstanceIndex ADB-port=$AdbPort"
    )
    Write-Output (
        "Healthy=$($test.Healthy) RecoveryInProgress=$recovering " +
        "PortListening=$listening TargetProcesses=$($targets.Count) " +
        "ShellExitCode=$($test.ShellExitCode) " +
        "ScreenshotExitCode=$($test.ScreenshotExitCode) " +
        "ScreenshotMs=$($test.ScreenshotMs) Reason=$($test.Reason)"
    )

    foreach ($target in $targets) {
        Write-Output (
            "TargetPID=$($target.ProcessId) TargetName=$($target.Name) " +
            "CommandLine=$($target.CommandLine)"
        )
    }

    if ($test.Healthy) {
        exit 0
    }

    exit 1
}

$monitorMutex = New-Object System.Threading.Mutex($false, $MonitorMutexName)
$ownsMonitorMutex = $false

try {
    try {
        $ownsMonitorMutex = $monitorMutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        $ownsMonitorMutex = $true
    }

    if (-not $ownsMonitorMutex) {
        Write-Log 'Another watchdog monitor is already running; duplicate process exiting'
        exit 0
    }

    Write-Log (
        "Watchdog v$ScriptVersion started: instance $InstanceIndex, " +
        "ADB port $AdbPort, shell timeout $ShellTimeoutSeconds seconds, " +
        "screencap timeout $ScreenshotTimeoutSeconds seconds"
    )

    $armed = $false
    $failures = 0
    $nextRestart = [datetime]::MinValue
    $nextHeartbeat = (Get-Date).AddMinutes(15)

    while ($true) {
        try {
            $health = Test-MuMu

            if ($health.Healthy) {
                if (-not $armed -or $failures -gt 0) {
                    Write-Log 'MuMu healthy; watchdog armed'
                }

                $armed = $true
                $failures = 0

                if ((Get-Date) -ge $nextHeartbeat) {
                    $targetCount = @(Get-TargetProcess).Count
                    Write-Log (
                        'Watchdog heartbeat: healthy, shell+screencap passed, ' +
                        "screencap=$($health.ScreenshotMs)ms, " +
                        "target processes=$targetCount"
                    )
                    $nextHeartbeat = (Get-Date).AddMinutes(15)
                }
            }
            elseif (
                $armed -or
                (Test-LocalAdbPort) -or
                (@(Get-TargetProcess).Count -gt 0)
            ) {
                if (-not $armed) {
                    $armed = $true
                    Write-Log 'Existing MuMu instance detected; watchdog adopted it'
                }

                $failures++
                Write-Log "Failure $failures/$FailuresToRestart : $($health.Reason)"

                if (
                    $failures -ge $FailuresToRestart -and
                    (Get-Date) -ge $nextRestart
                ) {
                    $nextRestart = (Get-Date).AddMinutes(10)
                    $failures = 0
                    Restart-MuMu
                    $armed = $true
                }
            }
            elseif ($failures -gt 0) {
                $failures = 0
                Write-Log 'MuMu has never been armed and is closed; waiting for manual startup'
            }
        }
        catch {
            Write-Log "Watchdog error: $($_.Exception.Message)"
        }

        Start-Sleep -Seconds $PollSeconds
    }
}
finally {
    Remove-Item -LiteralPath $RecoveryMarkerPath -Force -ErrorAction SilentlyContinue

    if ($ownsMonitorMutex) {
        try {
            [void]$monitorMutex.ReleaseMutex()
        }
        catch {
        }
    }

    $monitorMutex.Dispose()
}
