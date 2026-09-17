# 运行状态检查手册

以下命令不读取或输出 MQTT 密码，适合日常巡检。

仓库内也提供了同等的一键检查：

```bash
sudo bash nas/status/alas_runtime_status.sh
```

## 一次查看全部 systemd 单元

```bash
sudo systemctl show \
  alas-supervisor.service \
  alas-monitor.service \
  alas-scheduler-repair.service \
  alas-scheduler-repair.timer \
  alas-adb-autostart.service \
  alas-adb-autostart.timer \
  alas-maintenance-guard.service \
  -p Id -p LoadState -p UnitFileState -p ActiveState -p SubState \
  -p Result -p ExecMainPID -p ExecMainStatus -p ExecStart \
  --no-pager
```

正常整合后的预期值：

| 单元 | enabled | active/sub | 说明 |
|---|---|---|---|
| `alas-supervisor.service` | enabled | active/running | 主监督进程 |
| `alas-monitor.service` | enabled | active/running | 实时人工接管告警 |
| `alas-scheduler-repair.timer` | enabled | active/waiting | 每分钟检查补丁 |
| `alas-scheduler-repair.service` | static | inactive/dead | oneshot 成功退出是正常状态 |
| `alas-adb-autostart.*` | disabled/static | inactive/dead | 已由 Supervisor 取代 |
| `alas-maintenance-guard.service` | disabled | inactive/dead | 已由 Supervisor 取代 |

## 查看进程、定时器和容器

```bash
pgrep -af 'alas_(supervisor|monitor|maintenance|adb)|install_alas_scheduler_control' || true

sudo systemctl list-timers --all --no-pager | grep -Ei 'NEXT|alas'

sudo docker inspect \
  -f '{{.Name}} state={{.State.Status}} running={{.State.Running}} restart={{.HostConfig.RestartPolicy.Name}}' \
  alas alas-adb-watcher alas-adb-failover
```

## 查看 ADB 主备和 Supervisor

```bash
sudo python3 /opt/scripts/alas_adb_failover.py show
sudo python3 /opt/scripts/alas_adb_failover.py status
sudo python3 /opt/scripts/alas_supervisor.py --check
```

## 查看日志

```bash
sudo journalctl -u alas-supervisor.service -n 100 --no-pager
sudo journalctl -u alas-monitor.service -n 100 --no-pager
sudo journalctl -u alas-scheduler-repair.service -n 100 --no-pager
```
