#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "请使用 sudo bash rollback.sh 运行" >&2
    exit 1
fi

state_file="/var/lib/alas-supervisor/state.json"
if [[ -f ${state_file} ]]; then
    phase="$(python3 - "${state_file}" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as file:
    state = json.load(file)
print(state.get("maintenance", {}).get("phase", "idle"))
PY
)"
    if [[ ${phase} == holding ]]; then
        echo "统一守卫正在维护锁定中，拒绝自动回滚。请等待维护完成后再执行。" >&2
        exit 1
    fi
fi

systemctl disable --now alas-supervisor.service >/dev/null 2>&1 || true

if systemctl cat alas-maintenance-guard.service >/dev/null 2>&1; then
    systemctl enable --now alas-maintenance-guard.service
fi
if systemctl cat alas-adb-autostart.timer >/dev/null 2>&1; then
    systemctl enable --now alas-adb-autostart.timer
fi

echo "已停用统一守卫，并重新启用原维护守卫与 ADB timer。"
systemctl is-active alas-maintenance-guard.service 2>/dev/null || true
systemctl is-active alas-adb-autostart.timer 2>/dev/null || true
