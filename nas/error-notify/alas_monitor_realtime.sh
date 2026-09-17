#!/usr/bin/env bash

set -uo pipefail

VERSION="2.0.0"
CONFIG_FILE="${ALAS_MONITOR_CONFIG:-/etc/alas-monitor.conf}"

# Non-secret defaults. CONFIG_FILE overrides these values.
CONTAINER="alas"
MQTT_PORT="1883"
MQTT_TOPIC="nas/alas/error"
MQTT_QOS="1"
MQTT_TIMEOUT_SECONDS="10"
ERROR_DIR=""
IMAGE_TARGET=""
IMAGE_MAX_AGE_MINUTES="30"
SCREENSHOT_DELAY_SECONDS="4"
STATE_DIR="/var/lib/alas-manual-alert"
LOCK_FILE="/run/alas-manual-alert.lock"
LOOKBACK="15m"
RECONNECT_SECONDS="2"
ALERT_COOLDOWN_SECONDS="300"
WAIT_PATH=""
HOST_LABEL="$(hostname 2>/dev/null || printf 'NAS')"
MANUAL_PATTERN='Request human takeover|Manual intervention required'

die() {
    printf '错误：%s\n' "$*" >&2
    exit 1
}

[[ -r "$CONFIG_FILE" ]] || die "配置文件不可读：${CONFIG_FILE}"

# This file must be root-owned mode 600; it may contain MQTT credentials.
# shellcheck source=/dev/null
source "$CONFIG_FILE"

for command_name in docker mosquitto_pub timeout flock sha256sum; do
    command -v "$command_name" >/dev/null 2>&1 || \
        die "缺少命令：${command_name}"
done

[[ -n "${MQTT_HOST:-}" ]] || die "MQTT_HOST 未配置"
[[ -n "${MQTT_TOPIC:-}" ]] || die "MQTT_TOPIC 未配置"
[[ "$MQTT_QOS" =~ ^[012]$ ]] || die "MQTT_QOS 必须是 0、1 或 2"
for value_name in \
    MQTT_TIMEOUT_SECONDS IMAGE_MAX_AGE_MINUTES \
    SCREENSHOT_DELAY_SECONDS RECONNECT_SECONDS ALERT_COOLDOWN_SECONDS; do
    value="${!value_name}"
    [[ "$value" =~ ^[0-9]+$ ]] || die "${value_name} 必须是非负整数"
done

umask 077

publish_mqtt() {
    local message="$1"
    local -a args=(
        -h "$MQTT_HOST"
        -p "$MQTT_PORT"
        -q "$MQTT_QOS"
        -t "$MQTT_TOPIC"
        -m "$message"
    )

    [[ -z "${MQTT_USER:-}" ]] || args+=( -u "$MQTT_USER" )
    [[ -z "${MQTT_PASS:-}" ]] || args+=( -P "$MQTT_PASS" )

    # Do not retain alerts: old failures must not replay after HA reconnects.
    timeout "${MQTT_TIMEOUT_SECONDS}s" mosquitto_pub "${args[@]}"
}

container_running() {
    [[ "$(
        docker inspect --format '{{.State.Running}}' \
            "$CONTAINER" 2>/dev/null
    )" == "true" ]]
}

clean_control_codes() {
    sed -E $'s/\x1B\\[[0-9;?]*[ -\\/]*[@-~]//g'
}

find_recent_image() {
    [[ -n "$ERROR_DIR" && -d "$ERROR_DIR" ]] || return 0
    find "$ERROR_DIR" \
        -type f \
        -name '*.png' \
        -mmin "-${IMAGE_MAX_AGE_MINUTES}" \
        -printf '%T@ %p\n' \
        2>/dev/null |
        sort -nr |
        head -n 1 |
        cut -d' ' -f2-
}

copy_latest_image() {
    local image
    image="$(find_recent_image)"
    [[ -n "$image" && -n "$IMAGE_TARGET" ]] || return 0

    mkdir -p "$(dirname "$IMAGE_TARGET")"
    if install -m 644 "$image" "$IMAGE_TARGET"; then
        printf '%s 截图已更新：%s\n' "$(date '+%F %T')" "$image"
    else
        printf '%s 截图更新失败：%s\n' \
            "$(date '+%F %T')" "$image" >&2
    fi
}

recent_context() {
    docker logs --since "$LOOKBACK" --timestamps "$CONTAINER" \
        2>&1 | clean_control_codes | tail -n 60 || true
}

handle_event() {
    local line="$1" event_id last_event_file last_event_time_file
    local last_event last_time now context message temp_state
    event_id="$(printf '%s\n' "$line" | sha256sum | awk '{print $1}')"
    last_event_file="$STATE_DIR/last_event.sha256"
    last_event_time_file="$STATE_DIR/last_event.epoch"
    last_event="$(head -n 1 "$last_event_file" 2>/dev/null || true)"
    last_time="$(head -n 1 "$last_event_time_file" 2>/dev/null || true)"
    now="$(date +%s)"

    if [[ "$last_event" == "$event_id" ]] && \
       [[ "$last_time" =~ ^[0-9]+$ ]] && \
       (( now - last_time < ALERT_COOLDOWN_SECONDS )); then
        return 0
    fi

    (( SCREENSHOT_DELAY_SECONDS == 0 )) || sleep "$SCREENSHOT_DELAY_SECONDS"
    copy_latest_image

    context="$(recent_context)"
    if (( ${#context} > 3000 )); then
        context="${context: -3000}"
    fi

    message="$(
        printf '%s\n' \
            'ALAS 需要手动接管' \
            "设备：${HOST_LABEL}" \
            "检测时间：$(date '+%F %T %Z')" \
            '' \
            'ALAS 已完成自动重试，但仍无法恢复。' \
            "最终状态：${line}" \
            '' \
            '最近日志：' \
            "$context"
    )"

    if publish_mqtt "$message"; then
        temp_state="${last_event_file}.tmp.$$"
        printf '%s\n' "$event_id" >"$temp_state"
        chmod 600 "$temp_state"
        mv -f "$temp_state" "$last_event_file"
        temp_state="${last_event_time_file}.tmp.$$"
        printf '%s\n' "$now" >"$temp_state"
        chmod 600 "$temp_state"
        mv -f "$temp_state" "$last_event_time_file"
        printf '%s 人工接管通知已发送：%s\n' \
            "$(date '+%F %T')" "$line"
        return 0
    fi

    printf '%s 人工接管通知发送失败；保持未去重，稍后重试\n' \
        "$(date '+%F %T')" >&2
    return 1
}

show_check() {
    printf '版本：%s\n' "$VERSION"
    printf '配置：%s\n' "$CONFIG_FILE"
    printf '容器：%s (%s)\n' \
        "$CONTAINER" \
        "$(container_running && printf '运行中' || printf '未运行')"
    printf 'MQTT：%s:%s → %s (QoS %s)\n' \
        "$MQTT_HOST" "$MQTT_PORT" "$MQTT_TOPIC" "$MQTT_QOS"
    printf '人工接管规则：%s\n' "$MANUAL_PATTERN"
    printf '相同事件冷却：%s 秒\n' "$ALERT_COOLDOWN_SECONDS"
    if [[ -n "$WAIT_PATH" ]]; then
        printf '前置路径：%s (%s)\n' \
            "$WAIT_PATH" \
            "$([[ -d "$WAIT_PATH" ]] && printf '就绪' || printf '未就绪')"
    fi
}

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

case "${1:-}" in
    --test)
        publish_mqtt "$(
            printf '%s\n' \
                'ALAS 人工接管通知测试' \
                "设备：${HOST_LABEL}" \
                'MQTT → Home Assistant 通知链工作正常。' \
                '实际运行只会在 ALAS 明确请求人工接管时推送。'
        )" || die "MQTT 人工接管通知测试发送失败"
        printf 'MQTT 人工接管通知测试发送成功\n'
        exit 0
        ;;
    --check)
        show_check
        exit 0
        ;;
    --version)
        printf '%s\n' "$VERSION"
        exit 0
        ;;
    --once)
        # Test/diagnostic mode: inspect the current lookback and exit.
        line="$(recent_context | grep -Ei "$MANUAL_PATTERN" | tail -n 1 || true)"
        [[ -z "$line" ]] || handle_event "$line"
        exit 0
        ;;
    "")
        ;;
    *)
        printf '用法：%s [--check|--test|--once|--version]\n' "$0" >&2
        exit 2
        ;;
esac

exec 9>"$LOCK_FILE"
flock -n 9 || die "已有监控实例正在运行"

while true; do
    if [[ -n "$WAIT_PATH" && ! -d "$WAIT_PATH" ]]; then
        sleep "$RECONNECT_SECONDS"
        continue
    fi

    if ! container_running; then
        sleep "$RECONNECT_SECONDS"
        continue
    fi

    # --tail 0 intentionally ignores historical failures on service startup.
    while IFS= read -r line; do
        clean_line="$(printf '%s\n' "$line" | clean_control_codes)"
        if printf '%s\n' "$clean_line" | grep -Eiq "$MANUAL_PATTERN"; then
            handle_event "$clean_line" || true
        fi
    done < <(docker logs --follow --tail 0 "$CONTAINER" 2>&1)

    # docker logs exits when the container restarts; reconnect to the new log.
    sleep "$RECONNECT_SECONDS"
done
