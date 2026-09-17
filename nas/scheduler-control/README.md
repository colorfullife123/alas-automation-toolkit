# ALAS 本地调度器控制

该组件把三个最小接口挂到 ALAS WebUI 的 ASGI 应用：

| 路径 | 方法 | 作用 |
|---|---|---|
| `/__local_alas_scheduler__/v1/status` | GET | 查看默认任务调度器状态 |
| `/__local_alas_scheduler__/v1/start` | POST | 启动调度器 |
| `/__local_alas_scheduler__/v1/stop` | POST | 停止调度器 |

接口只接受回环来源。Supervisor 通过 `docker exec` 在容器内访问，不向局域网暴露控制权限。

## 为什么需要自动修复

ALAS 更新可能替换 `module/webui/app.py`。timer 每分钟检查接入点；检测到变化后先等待文件稳定，再做 AST 结构检查、备份、补丁、语法检查、容器重启及接口验证。任一步失败都会恢复更新后的原文件，并阻止同一版本反复重试。

## 安装与检查

```bash
sudo bash install.sh
sudoedit /etc/default/alas-scheduler-control
sudo chmod 600 /etc/default/alas-scheduler-control
sudo bash install.sh

sudo python3 /opt/scripts/install_alas_scheduler_control.py --check
sudo systemctl status alas-scheduler-repair.timer --no-pager
```

安装会重启一次 ALAS 容器；请避开正在执行任务的时间。
