# Changelog

All notable changes to this project will be documented in this file.

## 1.1.2 - 2026-09-14

- Fix a false-negative health result when MuMuManager returns the expected ADB output with a nonzero or stale native process exit code.
- Complete and refresh the Process object after a bounded wait before recording its diagnostic exit code.
- Judge the shell probe by its timeout state and MUMU_WATCHDOG_OK marker.
- Append MUMU_SCREENSHOT_OK only after the Android screencap command succeeds, and require that marker for screenshot health.
- Judge control requests by their JSON errcode when available instead of rejecting a valid response solely because of the native exit code.
- Include shell and screenshot native exit codes in Check output for diagnostics.
- Keep the preflight fail-closed; no bypass is required.

## 1.1.1 - 2026-09-14

- Replace the unsafe restart/shutdown fallback with exact-PID recovery for an already unresponsive instance.
- Give every MuMuManager command a hard deadline and terminate its process tree on timeout.
- Reset MuMuNxMain and stale MuMuManager processes before relaunching in single-instance mode.
- Skip shared controller reset when another MuMu instance is running.
- Require the old instance PID to disappear before relaunching.
- Treat launch success only as request acceptance, never as proof of recovery.
- Wait for a new PID and ADB listener to stabilize, then require three consecutive shell and screencap confirmations.
- Add a recovery marker so manual checks return quickly while recovery is active.
- Add a named mutex to prevent duplicate monitors for the same instance.
- Verify ADB port ownership during normal health checks and recovery.
- Add separate shell timeout, startup grace, and recovery-confirmation settings.
- Reduce the default screenshot timeout from 15 seconds to 10 seconds.
- Disable the deliberate Freeze test because suspending the complete VM process can deadlock MuMu's shared control plane.
- Update installer rollback, task arguments, tests, examples, and safety documentation.

## 1.1.0 - 2026-09-14

- Add a real Android screencap probe to detect false-healthy emulator hangs.
- Keep screenshot probe output in /dev/null to avoid image storage and disk growth.
- Add a configurable screenshot timeout with a safe 15-second default.
- Distinguish ADB shell failures from screenshot-pipeline failures in logs.
- Include the watchdog version and passed probes in startup and heartbeat records.
- Record screenshot-probe latency in health output, failures, and heartbeats.
- Update health tests and installation arguments for the stronger probe.
- Preflight the strong probe before replacing a running installation.

## 1.0.0 - 2026-09-13

- Add MuMu 15 instance recognition through MuMuNxDevice.exe --vm.
- Add legacy MuMu process recognition.
- Add ADB port and shell health checks.
- Add three-stage restart fallback: restart, shutdown, exact-PID termination and launch.
- Add per-instance logging and periodic healthy heartbeat.
- Add current-user scheduled-task installer with logon and one-minute recovery triggers.
- Add rollback-safe upgrades and conservative uninstallation.
- Add health, scheduled-task recovery and deliberate-freeze tests.
