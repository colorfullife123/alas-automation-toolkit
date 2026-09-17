# ALAS Unified Supervisor

Supervisor 合并原来的 ADB 自动启动与维护守卫职责，并协调本仓库的本地调度器接口。

## 状态规则

| 情况 | 容器 | ALAS 调度器 |
|---|---|---|
| ADB 正常 | 保持运行 | 保持用户当前状态 |
| ADB 断线 | 保持运行 | 暂停 |
| ADB 连续 3 次恢复 | 必要时启动 | 启动并发送恢复通知 |
| 官方维护前 5 分钟 | 保持运行 | 暂停 |
| 港区确认开服且 ADB 在线 | 保持运行 | 启动 |
| 容器自身重新启动 | Docker 策略处理 | 自动启动 |

因此，它不会持续强制打开调度器：用户手动停止调度器后，只要容器启动时间未变化且 ADB 没有经历恢复，Supervisor 会保留手动停止状态。这避免了与“模拟器或 ALAS 重启后自动恢复”的逻辑冲突。

## 前置条件

按以下顺序安装：

1. `nas/adb-failover/`；
2. `nas/error-notify/`；
3. `nas/scheduler-control/`；
4. 本目录。

第一次运行安装器会创建 `/etc/default/alas-supervisor` 模板并退出。填入本机 ALAS 根目录、固定 ADB 入口和港区，再次运行即可。

```bash
sudo bash install.sh
sudoedit /etc/default/alas-supervisor
sudo chmod 600 /etc/default/alas-supervisor
sudo bash install.sh
```

## 检查

```bash
sudo python3 /opt/scripts/alas_supervisor.py --check
sudo python3 /opt/scripts/alas_supervisor.py --history
sudo systemctl status alas-supervisor.service --no-pager
sudo journalctl -u alas-supervisor.service -n 100 --no-pager
```

`--check` 是只读操作，会显示固定 ADB 入口、当前 ADB 状态、维护信息、ALAS 港区解析结果及基础容器状态。
