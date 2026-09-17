# 一键状态检查

```bash
sudo bash alas_runtime_status.sh
```

该命令汇总 systemd 单元、timer、相关进程、三个 Docker 容器、ADB 主备配置、调度器补丁及 Supervisor 完整检查。它不会读取或输出 MQTT 密码。

只检查本机状态、不访问维护公告和服务器状态接口：

```bash
sudo bash alas_runtime_status.sh --quick
```
