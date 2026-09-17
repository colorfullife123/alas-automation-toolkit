#!/usr/bin/env bash

set -uo pipefail

if (( EUID != 0 )); then
    printf '请使用 sudo bash %s [--quick] 运行\n' "$0" >&2
    exit 1
fi

case "${1:-}" in
    "") full_check=1 ;;
    --quick) full_check=0 ;;
    *)
        printf '用法：%s [--quick]\n' "$0" >&2
        exit 2
        ;;
esac

units=(
    alas-supervisor.service
    alas-monitor.service
    alas-scheduler-repair.service
    alas-scheduler-repair.timer
    alas-adb-autostart.service
    alas-adb-autostart.timer
    alas-maintenance-guard.service
)

printf '%s\n' '===== systemd 单元 ====='
systemctl show "${units[@]}" \
    -p Id -p LoadState -p UnitFileState -p ActiveState -p SubState \
    -p Result -p ExecMainPID -p ExecMainStatus -p ExecStart \
    --no-pager 2>&1 || true

printf '\n%s\n' '===== ALAS 定时器 ====='
systemctl list-timers --all --no-pager 2>&1 | \
    awk 'NR == 1 || tolower($0) ~ /alas/' || true

printf '\n%s\n' '===== 相关进程 ====='
pgrep -af \
    'alas_(supervisor|monitor|maintenance|adb)|install_alas_scheduler_control' \
    || printf '%s\n' '(none)'

printf '\n%s\n' '===== Docker 容器 ====='
for container in alas alas-adb-watcher alas-adb-failover; do
    docker inspect \
        -f '{{.Name}} state={{.State.Status}} running={{.State.Running}} restart={{.HostConfig.RestartPolicy.Name}} started={{.State.StartedAt}}' \
        "$container" 2>&1 || true
done

printf '\n%s\n' '===== ADB 主备配置 ====='
if [[ -x /opt/scripts/alas_adb_failover.py ]]; then
    /opt/scripts/alas_adb_failover.py show 2>&1 || true
else
    printf '%s\n' '未安装 alas_adb_failover.py'
fi

printf '\n%s\n' '===== 调度器补丁 ====='
if [[ -x /opt/scripts/install_alas_scheduler_control.py ]]; then
    /opt/scripts/install_alas_scheduler_control.py --check 2>&1 || true
else
    printf '%s\n' '未安装 install_alas_scheduler_control.py'
fi

if (( full_check )); then
    printf '\n%s\n' '===== Supervisor 完整检查 ====='
    if [[ -x /opt/scripts/alas_supervisor.py ]]; then
        /opt/scripts/alas_supervisor.py --check 2>&1 || true
    else
        printf '%s\n' '未安装 alas_supervisor.py'
    fi
fi
