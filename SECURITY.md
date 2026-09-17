# Security and privacy

## Reporting

请不要在公开 Issue 中粘贴真实配置、日志、截图或错误归档。报告问题时应先替换用户名、主机名、IP 地址、设备序列号、服务器名称、邮箱、Token、Cookie 和通知凭据。

## Secret handling

- 密码和 Token 只能放在仓库外的权限 `0600` 配置文件中。
- 示例配置只能使用占位符和文档专用地址。
- 安装器不得在标准输出或 systemd 日志中打印密码。
- GitHub Actions 会运行 `tests/privacy_scan.py`，但自动扫描不能替代人工检查。
- 如果凭据曾进入 Git 历史，仅删除当前文件并不安全；应立即轮换凭据，并重写受影响的历史记录。

## Runtime boundaries

- 本机调度器接口只能监听 loopback，并拒绝非本机客户端。
- ADB 代理只应暴露在受信任网络中。
- 终止或重启模拟器前必须唯一识别目标实例，不能影响其他实例。
- 仓库不收集或上传游戏账号、日志、截图及通知内容。
