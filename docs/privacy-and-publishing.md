# 隐私与发布边界

## 可以公开

- 本仓库中的 `.py`、`.sh`、`.ps1`、systemd unit、Compose 片段；
- 只含占位值的 `.example` 配置；
- Home Assistant 自动化模板；
- 单元测试、CI、说明文档。

## 不应公开

- `/etc/alas-monitor.conf`、`/etc/default/*` 的实际部署副本；
- ALAS `config/alas.json`、游戏账号、通知 Token、SMTP/OnePush 字符串；
- 内网 IP、动态域名、NAS/Windows 用户名、主机名和共享目录；
- 日志、截图、状态 JSON、备份文件、crontab 导出；
- 含真实值的 Compose override 或 HAProxy 运行配置。

提交前执行：

```bash
python3 tests/privacy_scan.py
git diff --check
```

隐私扫描会阻止私网 IPv4、已知部署标识、邮箱、疑似明文凭据、真实 `.conf`、状态和备份文件。示例地址使用 RFC 5737 文档网段，不指向真实设备。
