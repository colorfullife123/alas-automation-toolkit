#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

FAKE_BIN="$TEMP_DIR/bin"
FAKE_LOG="$TEMP_DIR/docker.log"
MQTT_CAPTURE="$TEMP_DIR/mqtt.capture"
CONFIG_FILE="$TEMP_DIR/monitor.conf"
STATE_DIR="$TEMP_DIR/state"
ERROR_DIR="$TEMP_DIR/error"
IMAGE_TARGET="$TEMP_DIR/shared/latest.png"

mkdir -p "$FAKE_BIN" "$ERROR_DIR"

cat >"$FAKE_BIN/docker" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

case "${1:-}" in
    inspect)
        printf 'true\n'
        ;;
    logs)
        cat "$FAKE_LOG"
        ;;
    *)
        exit 1
        ;;
esac
SH

cat >"$FAKE_BIN/mosquitto_pub" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

message=""
topic=""
while (( $# )); do
    case "$1" in
        -m)
            message="$2"
            shift 2
            ;;
        -t)
            topic="$2"
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done

{
    printf '%s\n' '---'
    printf 'topic=%s\n' "$topic"
    printf '%s\n' "$message"
} >>"$MQTT_CAPTURE"
SH

chmod +x "$FAKE_BIN/docker" "$FAKE_BIN/mosquitto_pub"

cat >"$CONFIG_FILE" <<EOF
CONTAINER="alas"
MQTT_HOST="mqtt.example"
MQTT_PORT="1883"
MQTT_USER="tester"
MQTT_PASS="not-a-secret"
MQTT_TOPIC="nas/alas/error"
MQTT_QOS="1"
MQTT_TIMEOUT_SECONDS="5"
ERROR_DIR="$ERROR_DIR"
IMAGE_TARGET="$IMAGE_TARGET"
IMAGE_MAX_AGE_MINUTES="30"
SCREENSHOT_DELAY_SECONDS="0"
STATE_DIR="$STATE_DIR"
LOCK_FILE="$TEMP_DIR/monitor.lock"
LOOKBACK="15m"
RECONNECT_SECONDS="0"
ALERT_COOLDOWN_SECONDS="300"
HOST_LABEL="test-host"
MANUAL_PATTERN='Request human takeover|Manual intervention required'
EOF

export PATH="$FAKE_BIN:$PATH"
export FAKE_LOG MQTT_CAPTURE
export ALAS_MONITOR_CONFIG="$CONFIG_FILE"
monitor="$PROJECT_DIR/nas/error-notify/alas_monitor_realtime.sh"

printf '%s\n' \
    'WARNING transient ADB disconnect' \
    'ERROR GameStuckError' \
    'INFO Task call: Restart' \
    >"$FAKE_LOG"

bash "$monitor" --once
[[ ! -e "$MQTT_CAPTURE" ]] || {
    printf 'FAIL: recoverable errors produced an alert\n' >&2
    exit 1
}

printf 'fake png\n' >"$ERROR_DIR/error.png"
printf '%s\n' \
    'CRITICAL Task failed after automatic retries' \
    'CRITICAL Request human takeover' \
    >"$FAKE_LOG"

bash "$monitor" --once
grep -Fq 'ALAS 需要手动接管' "$MQTT_CAPTURE"
grep -Fq 'Request human takeover' "$MQTT_CAPTURE"
[[ -f "$IMAGE_TARGET" ]]

first_count="$(grep -c '^---$' "$MQTT_CAPTURE")"
bash "$monitor" --once
second_count="$(grep -c '^---$' "$MQTT_CAPTURE")"
[[ "$first_count" == "$second_count" ]] || {
    printf 'FAIL: the same event was not deduplicated\n' >&2
    exit 1
}

bash "$monitor" --test
grep -Fq 'ALAS 人工接管通知测试' "$MQTT_CAPTURE"

printf 'PASS: filtering, alerting, screenshot copy and deduplication\n'
