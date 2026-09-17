#!/usr/bin/env bash

set -Eeuo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
config_file="/etc/default/alas-scheduler-control"
target_installer="/opt/scripts/install_alas_scheduler_control.py"
target_payload="/opt/lib/alas-scheduler-control/local_scheduler_control.py"
target_service="/etc/systemd/system/alas-scheduler-repair.service"
target_timer="/etc/systemd/system/alas-scheduler-repair.timer"

(( EUID == 0 )) || {
    printf '请使用 sudo bash install.sh 运行\n' >&2
    exit 1
}

for command_name in python3 docker systemctl install; do
    command -v "$command_name" >/dev/null 2>&1 || {
        printf '缺少依赖：%s\n' "$command_name" >&2
        exit 1
    }
done

if [[ ! -f "$config_file" ]]; then
    install -D -o root -g root -m 600 \
        "$project_dir/alas-scheduler-control.env.example" "$config_file"
    printf '已创建配置模板：%s\n' "$config_file"
    printf '请填写 ALAS_ROOT 后重新运行安装器。\n' >&2
    exit 2
fi

[[ "$(stat -c '%U:%G %a' "$config_file")" == "root:root 600" ]] || {
    printf '%s 必须为 root:root 且权限 600\n' "$config_file" >&2
    exit 1
}

# shellcheck source=/dev/null
set -a
source "$config_file"
set +a

: "${ALAS_ROOT:?请在 $config_file 设置 ALAS_ROOT}"
for required in \
    "$ALAS_ROOT/module/webui/app.py" \
    "$ALAS_ROOT/module/webui/process_manager.py"; do
    [[ -f "$required" ]] || {
        printf '找不到 ALAS 文件：%s\n' "$required" >&2
        exit 1
    }
done

timestamp="$(date '+%Y%m%d-%H%M%S')"
backup_dir="/var/backups/alas-scheduler-control-package/${timestamp}"
mkdir -p "$backup_dir"

for target in \
    "$target_installer" "$target_payload" "$target_service" "$target_timer"; do
    if [[ -f "$target" ]]; then
        cp -a "$target" "$backup_dir/$(basename "$target")"
    fi
done

install -D -o root -g root -m 755 \
    "$project_dir/install_alas_scheduler_control.py" "$target_installer"
install -D -o root -g root -m 644 \
    "$project_dir/local_scheduler_control.py" "$target_payload"
install -D -o root -g root -m 644 \
    "$project_dir/alas-scheduler-repair.service" "$target_service"
install -D -o root -g root -m 644 \
    "$project_dir/alas-scheduler-repair.timer" "$target_timer"

# The repairer makes its own app.py/module backup and restores it if the local
# endpoint does not become healthy after the container restart.
python3 "$target_installer" --repair --force

systemctl daemon-reload
systemctl enable --now alas-scheduler-repair.timer

printf '调度器控制已安装；软件包备份：%s\n' "$backup_dir"
python3 "$target_installer" --check
