# ADB 主备故障切换

HAProxy 向 ALAS 暴露一个固定端点，并按健康检查结果选择远程 MuMu ADB。`adb_primary` 没有 `backup` 标记，因此始终优先；`adb_backup` 只在主设备失联时接管。

## 查看当前域名、端口和角色

```bash
sudo python3 /opt/scripts/alas_adb_failover.py show
sudo python3 /opt/scripts/alas_adb_failover.py status

# 同时核对 HAProxy 实际加载的 server 行
sudo docker exec alas-adb-failover \
  awk '$1 == "server" {print}' /usr/local/etc/haproxy/haproxy.cfg
```

输出中的“主设备”就是默认设备，“备用设备”只在主设备 TCP 健康检查失败时接管。

## 设置主、备用设备

```bash
sudoedit /etc/alas-adb-failover.conf
sudo chmod 600 /etc/alas-adb-failover.conf
sudo bash install.sh --apply
```

把首选模拟器填入 `ADB_PRIMARY_HOST/PORT`，后备模拟器填入 `ADB_BACKUP_HOST/PORT`。ALAS 的设备序列号始终填固定入口，不直接填任何远程模拟器地址。

安装器生成独立的 `docker-compose.adb-failover.yml`，不会覆盖用户原有的 `docker-compose.override.yml`。不带 `--apply` 时只生成并显示配置，不改动正在运行的容器。

## 切换特性

- 检查间隔 2 秒，连续 2 次失败判定主设备离线；
- 主设备恢复后连续 1 次通过即自动切回；
- `shutdown-sessions` 会断开旧 TCP 会话，`adb_watcher` 随后重连固定入口；
- Supervisor 只观察固定入口，因此不会与 HAProxy 的主备选择冲突。
