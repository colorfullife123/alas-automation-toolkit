#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "请使用 sudo bash install.sh 运行" >&2
    exit 1
fi

installer_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source_script="${installer_dir}/alas_supervisor.py"
source_unit="${installer_dir}/alas-supervisor.service"
source_rollback="${installer_dir}/rollback.sh"

target_script="/opt/scripts/alas_supervisor.py"
target_unit="/etc/systemd/system/alas-supervisor.service"
target_rollback="/opt/scripts/alas_supervisor_rollback.sh"
config_file="/etc/default/alas-supervisor"
mqtt_config_file="/etc/alas-monitor.conf"

legacy_guard_service="alas-maintenance-guard.service"
legacy_adb_service="alas-adb-autostart.service"
legacy_adb_timer="alas-adb-autostart.timer"
new_service="alas-supervisor.service"

timestamp="$(date +%Y%m%d-%H%M%S)"
backup_dir="/var/backups/alas-supervisor/${timestamp}"
migration_started=0
install_succeeded=0
previous_supervisor_active="unknown"
previous_supervisor_enabled="unknown"

rollback_on_error() {
    local exit_code=$?
    if [[ ${migration_started} -eq 1 && ${install_succeeded} -eq 0 ]]; then
        echo >&2
        echo "新服务安装失败，正在恢复旧服务……" >&2
        systemctl disable --now "${new_service}" >/dev/null 2>&1 || true
        old_script="${backup_dir}/${target_script#/}"
        old_unit="${backup_dir}/${target_unit#/}"
        if [[ ${previous_supervisor_active} == active ]] && \
           [[ -f ${old_script} && -f ${old_unit} ]]; then
            cp -a "${old_script}" "${target_script}"
            cp -a "${old_unit}" "${target_unit}"
            systemctl daemon-reload
            systemctl restart "${new_service}" >/dev/null 2>&1 || true
            if [[ ${previous_supervisor_enabled} == enabled ]]; then
                systemctl enable "${new_service}" >/dev/null 2>&1 || true
            else
                systemctl disable "${new_service}" >/dev/null 2>&1 || true
            fi
        else
            if systemctl cat "${legacy_guard_service}" >/dev/null 2>&1; then
                systemctl enable --now "${legacy_guard_service}" >/dev/null 2>&1 || true
            fi
            if systemctl cat "${legacy_adb_timer}" >/dev/null 2>&1; then
                systemctl enable --now "${legacy_adb_timer}" >/dev/null 2>&1 || true
            fi
        fi
    fi
    exit "${exit_code}"
}
trap rollback_on_error ERR

for command_name in python3 curl docker systemctl install mosquitto_pub; do
    if ! command -v "${command_name}" >/dev/null 2>&1; then
        echo "缺少命令：${command_name}" >&2
        exit 1
    fi
done

previous_supervisor_active="$(
    systemctl is-active "${new_service}" 2>/dev/null || true
)"
previous_supervisor_enabled="$(
    systemctl is-enabled "${new_service}" 2>/dev/null || true
)"

if [[ ! -f ${config_file} ]]; then
    install -D -o root -g root -m 600 \
        "${installer_dir}/alas-supervisor.env.example" "${config_file}"
    echo "已创建配置模板：${config_file}" >&2
    echo "请填写本机路径、固定 ADB 入口和港区后重新运行。" >&2
    exit 2
fi

if [[ "$(stat -c '%U:%G %a' "${config_file}")" != "root:root 600" ]]; then
    echo "${config_file} 必须为 root:root 且权限 600" >&2
    exit 1
fi

# shellcheck source=/dev/null
set -a
source "${config_file}"
set +a
mqtt_config_file="${ALAS_MQTT_CONFIG:-${mqtt_config_file}}"

for source_file in "${source_script}" "${source_unit}" "${source_rollback}"; do
    if [[ ! -f ${source_file} ]]; then
        echo "安装包不完整：缺少 ${source_file}" >&2
        exit 1
    fi
done

for container_name in \
    "${ALAS_CONTAINER:-alas}" alas-adb-failover alas-adb-watcher; do
    if ! docker inspect "${container_name}" >/dev/null 2>&1; then
        echo "找不到 Docker 容器：${container_name}，未进行安装" >&2
        exit 1
    fi
done

if [[ ! -f ${mqtt_config_file} ]]; then
    echo "找不到 ${mqtt_config_file}，请先安装 error-notify 组件" >&2
    exit 1
fi

if [[ "$(stat -c '%U:%G %a' "${mqtt_config_file}")" != "root:root 600" ]]; then
    echo "${mqtt_config_file} 必须为 root:root 且权限 600" >&2
    exit 1
fi

if [[ -x /opt/scripts/install_alas_scheduler_control.py ]]; then
    /usr/bin/python3 /opt/scripts/install_alas_scheduler_control.py --check >/dev/null
else
    echo "缺少 ALAS 调度器控制安装器；请先安装 scheduler-control 组件" >&2
    exit 1
fi

echo "正在进行统一守卫只读预检……"
python3 "${source_script}" --check

mkdir -p "${backup_dir}"
for backup_file in \
    /opt/scripts/alas_adb_autostart.py \
    /opt/scripts/alas_maintenance_guard.py \
    /etc/systemd/system/alas-adb-autostart.service \
    /etc/systemd/system/alas-adb-autostart.timer \
    /etc/systemd/system/alas-maintenance-guard.service \
    /var/lib/alas-adb-autostart/state.json \
    /var/lib/alas-maintenance-guard/state.json \
    "${target_script}" \
    "${target_unit}" \
    "${config_file}"; do
    if [[ -e ${backup_file} ]]; then
        backup_relative="${backup_file#/}"
        backup_parent="${backup_dir}/${backup_relative%/*}"
        mkdir -p "${backup_parent}"
        cp -a "${backup_file}" "${backup_parent}/"
    fi
done

install -D -m 0755 "${source_script}" "${target_script}"
install -D -m 0644 "${source_unit}" "${target_unit}"
install -D -m 0755 "${source_rollback}" "${target_rollback}"
systemctl daemon-reload

migration_started=1

# Stop the periodic starter first, then hand maintenance ownership to the new daemon.
systemctl disable --now "${legacy_adb_timer}" >/dev/null 2>&1 || true
systemctl stop "${legacy_adb_service}" >/dev/null 2>&1 || true
systemctl disable "${legacy_adb_service}" >/dev/null 2>&1 || true
systemctl disable --now "${legacy_guard_service}" >/dev/null 2>&1 || true

systemctl enable "${new_service}"
systemctl restart "${new_service}"
sleep 3
systemctl is-active --quiet "${new_service}"

install_succeeded=1
trap - ERR

echo
echo "===== 统一服务 ====="
systemctl --no-pager --full status "${new_service}" | sed -n '1,18p'
echo
echo "===== 旧服务状态 ====="
for unit_name in "${legacy_guard_service}" "${legacy_adb_timer}"; do
    printf '%s: enabled=%s active=%s\n' \
        "${unit_name}" \
        "$(systemctl is-enabled "${unit_name}" 2>/dev/null || true)" \
        "$(systemctl is-active "${unit_name}" 2>/dev/null || true)"
done
echo
echo "===== 最新日志 ====="
journalctl -u "${new_service}" -n 30 --no-pager -o cat
echo
echo "备份位置：${backup_dir}"
echo "安装完成：保留 alas、alas-adb-failover、alas-adb-watcher 三个容器。"
