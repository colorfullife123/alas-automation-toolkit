# MuMu Watchdog

一个面向 Windows 的 MuMu 模拟器实例监控与自动恢复脚本。它定期检查 ADB shell、真实截图链路、ADB 端口归属和实例进程；连续失败达到阈值后执行经过验证的安全恢复。

项目主要用于需要长期保持 ADB 在线的自动化场景，例如 AzurLaneAutoScript（ALAS），也可以独立使用。

## v1.1.2 修复重点

v1.1.0 在实例完全冻结时会先调用 MuMuManager 的 restart 和 shutdown。管理命令可能与冻结实例一起卡住；随后即使 launch 返回 errcode 0，也只代表请求已接收，不能证明 Android 已恢复。

v1.1.2 改为：

- 管理命令全部具有硬超时；超时后结束该命令的完整进程树。
- ADB 探针以“未超时且成功标记已返回”为准，不再依赖 MuMuManager 不稳定的进程退出码。
- 截图命令仅在 screencap 成功后返回 MUMU_SCREENSHOT_OK。
- 不再对已经确认无响应的实例调用 restart 或 shutdown。
- 只精确结束匹配到的目标实例 PID。
- 单实例场景会重置同目录下的 MuMuNxMain 和 MuMuManager，再重新启动主控制器。
- 如果检测到其他 MuMu 实例，跳过共享控制器重置，避免影响它们。
- launch 后等待新 PID 和 ADB 端口稳定。
- 必须连续通过 3 次 ADB shell 与 screencap，才记录恢复成功。
- 恢复期间写入状态标记；手动健康检查会立即返回，不会叠加新的管理命令。
- 同一实例只允许一个 Watchdog 监控进程运行。
- 禁用危险的进程冻结测试。

## 功能

- 支持 MuMu 15 的 MuMuNxDevice.exe 实例识别。
- 兼容旧版 MuMuPlayer.exe、NemuPlayer.exe 实例参数。
- 验证目标 PID 是否拥有配置的本地 ADB 监听端口。
- 使用 screencap -p /dev/null 检测截图链路“假健康”，不会保存截图。
- 默认连续失败 3 次后恢复，减少瞬时抖动造成的误操作。
- 默认提供 180 秒启动宽限期与 3 次恢复确认。
- 登录 Windows 后自动运行。
- 每分钟触发一次计划任务，用于拉起意外退出的 Watchdog。
- 每 15 分钟记录一次健康心跳。
- 升级前自动备份原脚本与计划任务，安装失败时自动回滚。

## 运行要求

- Windows 10 或 Windows 11。
- Windows PowerShell 5.1。
- MuMu 模拟器及其 MuMuManager.exe。
- 已启用对应实例的 ADB 端口。
- 安装需要管理员 PowerShell。
- 计划任务使用当前交互式 Windows 用户运行。

## 安装或升级

下载仓库 ZIP 并解压，以管理员身份打开 Windows PowerShell，进入解压目录后执行：

在 PowerShell 中请使用下面的完整命令：

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 `
    -ManagerPath "D:\Program Files\Netease\MuMu\nx_main\MuMuManager.exe" `
    -InstanceIndex 0 `
    -AdbPort 16384
~~~

安装器会：

1. 在 MuMu 正常运行时执行新版强健康预检。
2. 备份现有脚本和计划任务。
3. 停止旧 Watchdog。
4. 安装新版脚本并重建计划任务。
5. 启动任务并确认状态为 Running。
6. 任一步失败时恢复旧版本。

默认任务名称：

~~~text
MuMu Watchdog - Instance 0
~~~

## 参数

| 参数 | 默认值 | 说明 |
| --- | ---: | --- |
| ManagerPath | 必填 | MuMuManager.exe 完整路径 |
| InstanceIndex | 0 | MuMu 实例编号 |
| AdbPort | 16384 | 对应实例的本地 ADB 端口 |
| PollSeconds | 30 | 常规检查间隔，范围 10–300 秒 |
| FailuresToRestart | 3 | 连续失败阈值，范围 2–10 |
| ShellTimeoutSeconds | 6 | ADB shell 硬超时，范围 3–30 秒 |
| ScreenshotTimeoutSeconds | 10 | 截图探针硬超时，范围 5–60 秒 |
| StartupGraceSeconds | 180 | 恢复后的启动宽限期，范围 60–600 秒 |
| RecoveryConfirmations | 3 | 恢复所需连续健康次数，范围 2–5 |

不建议把 FailuresToRestart 改为 1。短时负载抖动可能触发不必要的重启。

## 健康检查

健康检查不会重启或结束 MuMu：

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\test_watchdog.ps1 `
    -Mode Health `
    -ManagerPath "D:\Program Files\Netease\MuMu\nx_main\MuMuManager.exe" `
    -InstanceIndex 0 `
    -AdbPort 16384
~~~

正常结果示例：

~~~text
Version=1.1.1 Host=DESKTOP Manager=... Instance=0 ADB-port=16384
Healthy=True RecoveryInProgress=False PortListening=True TargetProcesses=1 ShellExitCode=1 ScreenshotExitCode=1 ScreenshotMs=320
~~~

部分 MuMuManager 版本即使 ADB 命令成功，也可能返回非零的 Windows 进程退出码。v1.1.2 会保留该退出码用于诊断，但健康判断以硬超时状态和 Android 成功标记为准。

管理命令超时时会被强制终止，不会无限卡在检查步骤。如果 Watchdog 正在恢复，检查会快速返回：

~~~text
Healthy=False RecoveryInProgress=True Reason=recovery is already in progress
~~~

## 安全恢复流程

当连续失败达到阈值时：

1. 唯一匹配目标实例，并核对 ADB 端口所有者。
2. 精确结束该实例 PID 及其子进程。
3. 等待旧 PID 完全消失。
4. 没有其他实例时，只重置 MuMuNxMain 和同目录 MuMuManager；不会结束 MuMuNxService。
5. 启动主控制器并发送一次 launch。
6. 等待新 PID 与 ADB 监听稳定。
7. 连续执行 shell 与 screencap 健康检查。
8. 达到恢复确认次数后重新布防 Watchdog。
9. 超过启动宽限期仍不健康时记录失败，并进入 10 分钟冷却，避免重启风暴。

launch 返回成功不再被视为恢复成功。

## 安全测试

普通健康测试：

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\test_watchdog.ps1 `
    -Mode Health `
    -ManagerPath "D:\Program Files\Netease\MuMu\nx_main\MuMuManager.exe"
~~~

计划任务自恢复测试：

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\test_watchdog.ps1 `
    -Mode TaskRecovery `
    -InstanceIndex 0
~~~

Freeze 模式从 v1.1.2 起被禁用。NtSuspendProcess 会暂停实例内所有线程，可能让 MuMu 的共享控制层一起进入等待状态；它不能安全代表普通的 Android 卡顿。

## 日志

~~~powershell
Get-Content "$env:LOCALAPPDATA\MuMuWatchdog\instance-0.log" -Tail 80
~~~

新版典型恢复日志：

~~~text
Failure 3/3 : ADB shell failed: timeout after 6 seconds; process tree terminated
Safe recovery started for instance 0
Stopping exact unhealthy instance PID 3712
Old instance PID 3712 fully exited
Resetting control process MuMuNxMain.exe PID 1234
MuMu main controller restarted
Launch request accepted
New instance process detected: PID 10592
New PID and ADB listener detected; allowing them to settle
Recovery health confirmation 1/3
Recovery health confirmation 2/3
Recovery health confirmation 3/3
MuMu recovery confirmed; watchdog re-armed
~~~

## Windows 重启后的行为

计划任务使用当前用户的 InteractiveToken：

- Windows 登录安装任务的用户后，Watchdog 自动启动。
- 停留在登录界面时不会运行。
- MuMu 从未启动过时，Watchdog 不会主动首次启动它。
- MuMu 一旦被 Watchdog 布防，后续崩溃或退出会按故障处理并尝试恢复。

## 多实例

可为每个实例分别运行安装器，并配置独立实例号和 ADB 端口。

恢复目标实例时，如果仍检测到其他 MuMu 实例进程，Watchdog 不会重置共享主控制器，只执行目标实例级恢复。此时若共享控制层本身已经卡死，Watchdog会记录恢复失败，而不会冒险结束其他实例。

## 关于 0x800710E0

任务运行时，每分钟触发器会再次请求启动。任务策略为 IgnoreNew，因此任务计划程序可能显示 0x800710E0。只要任务状态仍为 Running、日志持续产生，这表示重复启动被忽略，不表示 Watchdog 崩溃。

## 卸载

保留脚本和日志：

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1 `
    -InstanceIndex 0
~~~

同时删除未被其他实例使用的脚本和该实例日志：

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1 `
    -InstanceIndex 0 `
    -RemoveFiles
~~~

## 安全边界

- 不包含用户名、SID、密码、MQTT 凭据或固定用户目录。
- 强制终止前必须唯一匹配目标实例。
- 仅在没有其他实例运行时重置共享控制器。
- 不结束 MuMuNxService 或 MuMuNxSVC。
- 安装失败自动回滚。
- 禁止通过冻结整个虚拟机进程测试恢复。

## Acknowledgements

- MuMuPlayer and the bundled MuMuManager command-line interface
- Microsoft PowerShell, Windows Task Scheduler, and Windows management APIs
- AzurLaneAutoScript, as an ADB automation workload used for integration testing

This project is independently developed and is not affiliated with NetEase, Microsoft, or the AzurLaneAutoScript project.

## License

[MIT](../../LICENSE)
