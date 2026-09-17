# Changelog

## 1.0.0 - 2026-09-17

- 整合 `alas-error-notify` 与 `mumu-watchdog` 的现有功能。
- 纳入当前 Supervisor v2.1：ADB 断线与维护期间只暂停调度器，容器保持运行；容器真正重启后自动恢复调度器。
- 加入仅限回环的调度器接口、ALAS 更新后自动修复、接口验证与失败回滚。
- 加入 HAProxy ADB 主备配置生成、角色/端点查看、TCP 状态探测与独立 Compose overlay。
- 将实时告警收敛为最终人工接管标志，并加入截图复制、持久去重、冷却和容器重启后日志重连。
- 加入统一运行状态检查、Home Assistant 模板、单元测试与 GitHub Actions。
- 将 MQTT、路径、设备端点和港区全部移到仓库外的 root-only 配置。
- 移除部署环境中的用户、主机、内网地址、域名、港区默认值和凭据。
