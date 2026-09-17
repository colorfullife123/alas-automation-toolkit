# ALAS 人工接管实时告警

该服务从 `docker logs --follow --tail 0` 读取新日志，只匹配 ALAS 的最终人工接管标志。`WARNING`、`Traceback`、`GameStuckError` 等通常仍可由 ALAS 自行恢复，不会单独触发通知。

## 安装

```bash
sudo bash install.sh
sudoedit /etc/alas-monitor.conf
sudo chown root:root /etc/alas-monitor.conf
sudo chmod 600 /etc/alas-monitor.conf
sudo bash install.sh
```

配置文件同时供 Supervisor 发送“ADB 已恢复”消息使用。真实 Broker 地址、用户名、密码、路径和设备名称不得提交到 Git。

## 检查

```bash
sudo /opt/scripts/alas_monitor_realtime.sh --check
sudo /opt/scripts/alas_monitor_realtime.sh --test
sudo systemctl status alas-monitor.service --no-pager
sudo journalctl -u alas-monitor.service -n 100 --no-pager
```

`--once` 仅用于诊断和测试，会扫描配置的回看窗口；正常 systemd 服务不会重放启动前的旧错误。
