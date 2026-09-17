#!/usr/bin/env bash

set -Eeuo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
config_file="/etc/alas-adb-failover.conf"
target_tool="/opt/scripts/alas_adb_failover.py"

(( EUID == 0 )) || {
    printf '请使用 sudo bash install.sh [--apply] 运行\n' >&2
    exit 1
}

if [[ ! -f "$config_file" ]]; then
    install -D -o root -g root -m 600 \
        "$project_dir/alas-adb-failover.conf.example" "$config_file"
    printf '已创建配置模板：%s\n' "$config_file"
    printf '请填写本机路径和三个 ADB 端点后重新运行。\n' >&2
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

[[ -f "$ALAS_ROOT/docker-compose.yml" ]] || {
    printf '找不到 %s/docker-compose.yml\n' "$ALAS_ROOT" >&2
    exit 1
}

timestamp="$(date '+%Y%m%d-%H%M%S')"
backup_dir="/var/backups/alas-adb-failover/${timestamp}"
mkdir -p "$backup_dir"
for target in \
    "$target_tool" \
    "$ALAS_ROOT/config/haproxy-adb.cfg" \
    "$ALAS_ROOT/docker-compose.adb-failover.yml"; do
    if [[ -f "$target" ]]; then
        cp -a "$target" "$backup_dir/$(basename "$target")"
    fi
done

install -D -o root -g root -m 755 \
    "$project_dir/alas_adb_failover.py" "$target_tool"
python3 "$target_tool" --config "$config_file" render \
    --output "$ALAS_ROOT/config/haproxy-adb.cfg"
install -o root -g root -m 644 \
    "$project_dir/docker-compose.adb-failover.yml" \
    "$ALAS_ROOT/docker-compose.adb-failover.yml"

printf '配置已安装；备份：%s\n' "$backup_dir"
python3 "$target_tool" --config "$config_file" show

if [[ "${1:-}" == "--apply" ]]; then
    fixed_endpoint="${ADB_FIXED_HOST}:${ADB_FIXED_PORT}"
    (
        cd "$ALAS_ROOT"
        ADB_FIXED_ENDPOINT="$fixed_endpoint" docker compose \
            -f docker-compose.yml \
            -f docker-compose.adb-failover.yml \
            up -d ALAS adb_failover adb_watcher
    )
    printf '容器已更新。\n'
else
    printf '尚未改动容器；确认后执行：sudo bash install.sh --apply\n'
fi
