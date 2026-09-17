#!/usr/bin/env bash

set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TARGET_SCRIPT="/opt/scripts/alas_monitor_realtime.sh"
TARGET_UNIT="/etc/systemd/system/alas-monitor.service"
CONFIG_FILE="/etc/alas-monitor.conf"

(( EUID == 0 )) || {
    printf '请使用 sudo bash install.sh 运行\n' >&2
    exit 1
}

for command_name in docker mosquitto_pub timeout flock sha256sum systemctl; do
    command -v "$command_name" >/dev/null 2>&1 || {
        printf '缺少依赖：%s\n' "$command_name" >&2
        exit 1
    }
done

timestamp="$(date '+%Y%m%d-%H%M%S')"
backup_dir="/var/backups/alas-error-notify/${timestamp}"
mkdir -p "$backup_dir"

for target in "$TARGET_SCRIPT" "$TARGET_UNIT"; do
    if [[ -f "$target" ]]; then
        cp -a "$target" "$backup_dir/$(basename "$target")"
    fi
done

if [[ ! -f "$CONFIG_FILE" ]]; then
    install -o root -g root -m 600 \
        "$PROJECT_DIR/alas-monitor.conf.example" "$CONFIG_FILE"
    printf '已创建配置模板：%s\n' "$CONFIG_FILE"
    printf '请先在本机填写 MQTT 与路径信息，再重新运行安装器。\n' >&2
    exit 2
fi

owner_mode="$(stat -c '%U:%G %a' "$CONFIG_FILE")"
if [[ "$owner_mode" != "root:root 600" ]]; then
    printf '为保护凭据，%s 必须为 root:root 且权限 600；当前为 %s\n' \
        "$CONFIG_FILE" "$owner_mode" >&2
    exit 1
fi

install -D -o root -g root -m 700 \
    "$PROJECT_DIR/alas_monitor_realtime.sh" "$TARGET_SCRIPT"
install -D -o root -g root -m 644 \
    "$PROJECT_DIR/alas-monitor.service" "$TARGET_UNIT"

# Remove only schedules that invoke this monitor; preserve unrelated cron jobs.
if crontab -l 2>/dev/null | grep -Fq '/opt/scripts/alas_monitor'; then
    crontab -l >"$backup_dir/root.crontab"
    { crontab -l | grep -Fv '/opt/scripts/alas_monitor' || true; } | crontab -
    printf '已移除旧轮询计划；备份位于 %s/root.crontab\n' "$backup_dir"
fi
rm -f /etc/cron.d/alas-monitor

systemctl daemon-reload
systemctl enable alas-monitor.service
systemctl restart alas-monitor.service
systemctl is-active --quiet alas-monitor.service

printf '实时监控已安装；备份：%s\n' "$backup_dir"
"$TARGET_SCRIPT" --check
printf '通知测试：sudo %s --test\n' "$TARGET_SCRIPT"
