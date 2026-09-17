#!/usr/bin/env python3
"""Unified ADB recovery and maintenance supervisor for the ALAS container.

The daemon keeps the existing HAProxy/ADB watcher topology untouched.  It
starts ALAS when the fixed ADB endpoint is online, records ADB outages and
publishes an MQTT recovery notification.  It also reads official Azur Lane CN
maintenance announcements, pauses the ALAS scheduler five minutes before a
maintenance window and waits for the configured server to recover.
"""

from __future__ import annotations

import argparse
import fcntl
import html
import json
import logging
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


VERSION = "2.1.0"
STATE_VERSION = 2
CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

LOCAL_ENV_FILE = Path(
    os.environ.get(
        "ALAS_SUPERVISOR_ENV_FILE",
        "/etc/default/alas-supervisor",
    )
)
LOCAL_ENV_NAMES = {
    "ALAS_CONTAINER",
    "ALAS_ROOT",
    "ALAS_ADB_SERVER_HOST",
    "ALAS_ADB_SERVER_PORT",
    "ALAS_ADB_SERIAL",
    "ALAS_SERVER_NAME",
    "ALAS_MQTT_CONFIG",
    "ALAS_MQTT_RECOVERY_TOPIC",
}


def load_local_environment(path: Path = LOCAL_ENV_FILE) -> None:
    """Read the systemd-compatible local defaults without executing them."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError(f"无法读取本地配置 {path}：{exc}") from exc

    assignment = re.compile(
        r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$"
    )
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = assignment.match(line)
        if not match:
            raise RuntimeError(f"本地配置第 {number} 行格式无效")
        name = match.group(1)
        if name not in LOCAL_ENV_NAMES or name in os.environ:
            continue
        try:
            values = shlex.split(match.group(2), comments=True, posix=True)
        except ValueError as exc:
            raise RuntimeError(
                f"本地配置第 {number} 行无法解析：{exc}"
            ) from exc
        if len(values) > 1:
            raise RuntimeError(f"本地配置第 {number} 行必须只有一个值")
        os.environ[name] = values[0] if values else ""

OFFICIAL_LIST_URL = "https://api.biligame.com/news/list.action"
OFFICIAL_DETAIL_URL = "https://api.biligame.com/news/{news_id}.action"
OFFICIAL_NEWS_URL = "https://game.bilibili.com/blhx/news/?news_detail_id={news_id}"
SERVER_STATE_URL = "http://sc.shiratama.cn/server/get_state"

GAME_EXTENSION_ID = 103
ALAS_CONTAINER = os.environ.get("ALAS_CONTAINER", "alas")
ALAS_ROOT = Path(os.environ.get("ALAS_ROOT", "/opt/AzurLaneAutoScript"))
ADB_SERVER = (
    os.environ.get("ALAS_ADB_SERVER_HOST", "127.0.0.1"),
    int(os.environ.get("ALAS_ADB_SERVER_PORT", "5037")),
)
ADB_SERIAL = os.environ.get("ALAS_ADB_SERIAL", "127.0.0.1:16555")
DEFAULT_SERVER_NAME = os.environ.get("ALAS_SERVER_NAME", "")

STATE_DIR = Path("/var/lib/alas-supervisor")
STATE_FILE_NAME = "state.json"
DAEMON_LOCK = Path("/run/alas-supervisor.lock")
LEGACY_ADB_STATE = Path("/var/lib/alas-adb-autostart/state.json")
LEGACY_MAINTENANCE_STATE = Path("/var/lib/alas-maintenance-guard/state.json")

MQTT_CONFIG_FILE = Path(
    os.environ.get("ALAS_MQTT_CONFIG", "/etc/alas-monitor.conf")
)
MQTT_RECOVERY_TOPIC = os.environ.get(
    "ALAS_MQTT_RECOVERY_TOPIC", "nas/alas/recovery"
)

LEAD_MINUTES = 5
ADB_POLL_SECONDS = 15
ADB_STABLE_CONFIRMATIONS = 3
SCHEDULE_REFRESH_SECONDS = 300
SERVER_POLL_SECONDS = 30
SERVER_AVAILABLE_CONFIRMATIONS = 2
SERVER_STATE_MAX_AGE_SECONDS = 600
DOCKER_STOP_TIMEOUT_SECONDS = 90

USER_AGENT = f"alas-supervisor/{VERSION}"


SERVER_GROUPS = {
    "cn_android": [
        "莱茵演习", "巴巴罗萨", "霸王行动", "冰山行动", "彩虹计划",
        "发电机计划", "瞭望台行动", "十字路口行动", "朱诺行动",
        "杜立特空袭", "地狱犬行动", "开罗宣言", "奥林匹克行动",
        "小王冠行动", "波茨坦公告", "白色方案", "瓦尔基里行动",
        "曼哈顿计划", "八月风暴", "秋季旅行", "水星行动", "莱茵河卫兵",
        "北极光计划", "长戟计划", "暴雨行动", "水仙行动", "冬月计划",
        "长弓计划", "裁决协议", "帷幕计划",
    ],
    "cn_ios": [
        "夏威夷", "珊瑚海", "中途岛", "铁底湾", "所罗门", "马里亚纳",
        "莱特湾", "硫磺岛", "冲绳岛", "阿留申群岛", "马耳他",
    ],
    "cn_channel": [
        "皇家巡游", "大西洋宪章", "十字军行动", "龙骑兵行动", "冥王星行动",
        "群岛计划",
    ],
}


TITLE_DATE_RE = re.compile(
    r"(?P<year>20\d{2})年\s*(?P<month>\d{1,2})月\s*(?P<day>\d{1,2})日"
    r"\s*(?P<hour>\d{1,2})[:：](?P<minute>\d{2})"
)

SCHEDULE_RE = re.compile(
    r"(?:司令部[^。；]{0,30}?)?将于\s*"
    r"(?:(?P<syear>20\d{2})年\s*)?"
    r"(?P<smonth>\d{1,2})月\s*(?P<sday>\d{1,2})日\s*"
    r"(?P<shour>\d{1,2})[:：](?P<sminute>\d{2})\s*"
    r"(?:~|～|—|－|-|至|到)\s*"
    r"(?:(?:(?P<eyear>20\d{2})年\s*)?"
    r"(?P<emonth>\d{1,2})月\s*(?P<eday>\d{1,2})日\s*)?"
    r"(?P<ehour>\d{1,2})[:：](?P<eminute>\d{2})"
)


class SupervisorError(RuntimeError):
    """Expected operational failure."""


@dataclass(frozen=True)
class MaintenanceEvent:
    news_id: int
    title: str
    start: datetime
    end: datetime
    url: str

    @property
    def stop_at(self) -> datetime:
        return self.start - timedelta(minutes=LEAD_MINUTES)

    def to_dict(self) -> dict[str, Any]:
        return {
            "news_id": self.news_id,
            "title": self.title,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "url": self.url,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MaintenanceEvent":
        return cls(
            news_id=int(value["news_id"]),
            title=str(value["title"]),
            start=datetime.fromisoformat(value["start"]),
            end=datetime.fromisoformat(value["end"]),
            url=str(value["url"]),
        )


@dataclass(frozen=True)
class ServerTarget:
    config_value: str
    server_name: str
    package_name: str
    source: str


@dataclass(frozen=True)
class ServerReading:
    server_name: str
    state: int
    last_update: int
    age_seconds: int

    @property
    def available(self) -> bool:
        # ALAS considers state=1 maintenance.  Fresh non-maintenance data is live.
        return self.state != 1 and -300 <= self.age_seconds <= SERVER_STATE_MAX_AGE_SECONDS


@dataclass(frozen=True)
class RuntimeConfig:
    container: str
    alas_root: Path
    state_dir: Path
    adb_server_host: str
    adb_server_port: int
    adb_serial: str
    server_name: str
    mqtt_config_file: Path
    mqtt_recovery_topic: str

    @property
    def state_file(self) -> Path:
        return self.state_dir / STATE_FILE_NAME


def now_china() -> datetime:
    return datetime.now(CHINA_TZ)


def now_text() -> str:
    return now_china().isoformat(timespec="seconds")


def format_dt(value: datetime) -> str:
    return value.astimezone(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S %z")


def format_duration(seconds: float) -> str:
    seconds_int = max(0, int(seconds))
    minutes, seconds_int = divmod(seconds_int, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分钟"
    if minutes:
        return f"{minutes} 分钟 {seconds_int} 秒"
    return f"{seconds_int} 秒"


def run_command(
    args: list[str], *, timeout: int = 30, allow_failure: bool = False
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SupervisorError(f"命令超时：{args[0]}") from exc
    if result.returncode and not allow_failure:
        detail = (result.stderr or result.stdout).strip()
        raise SupervisorError(detail or f"命令失败：{args[0]}，退出码 {result.returncode}")
    return result


def fetch_json(
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    method: str = "GET",
    timeout: int = 20,
    bypass_proxy: bool = False,
) -> dict[str, Any]:
    curl = shutil.which("curl")
    if not curl:
        raise SupervisorError("找不到 curl")

    args = [
        curl,
        "-4",
        "--silent",
        "--show-error",
        "--fail",
        "--location",
        "--connect-timeout",
        "8",
        "--max-time",
        str(timeout),
        "--retry",
        "2",
        "--retry-delay",
        "1",
        "--user-agent",
        USER_AGENT,
    ]
    if bypass_proxy:
        args.extend(["--noproxy", "*"])
    method = method.upper()
    if params:
        args.append("--get")
    if method == "POST":
        args.extend(["--request", "POST"])
    elif method != "GET":
        raise ValueError(f"不支持的 HTTP 方法：{method}")
    for key, value in (params or {}).items():
        args.extend(["--data-urlencode", f"{key}={value}"])
    args.append(url)

    result = run_command(args, timeout=timeout + 10)
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SupervisorError(f"接口返回的不是 JSON：{url}") from exc
    if not isinstance(value, dict):
        raise SupervisorError(f"接口 JSON 结构异常：{url}")
    return value


def html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(?:script|style)\b[^>]*>.*?</(?:script|style)>", "", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?i)</(?:p|div|li|tr|h[1-6])\s*>", "\n", value)
    value = re.sub(r"(?s)<[^>]+>", "", value)
    value = html.unescape(value).replace("\xa0", " ")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n+", "\n", value)
    return value.strip()


def title_datetime(title: str) -> Optional[datetime]:
    match = TITLE_DATE_RE.search(title)
    if not match:
        return None
    try:
        return datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            tzinfo=CHINA_TZ,
        )
    except ValueError:
        return None


def parse_event(news: dict[str, Any]) -> MaintenanceEvent:
    title = str(news.get("title") or "")
    if "维护公告" not in title:
        raise SupervisorError(f"不是维护公告：{title}")

    title_start = title_datetime(title)
    body = html_to_text(str(news.get("content") or ""))
    match = SCHEDULE_RE.search(body[:12000])
    if not match:
        raise SupervisorError(f"无法从公告解析维护时段：{title}")

    published = str(news.get("modifyTime") or news.get("ctime") or "")
    try:
        published_year: Optional[int] = datetime.strptime(
            published[:19], "%Y-%m-%d %H:%M:%S"
        ).year
    except (ValueError, TypeError):
        published_year = None

    start_year = int(
        match.group("syear")
        or (title_start.year if title_start else 0)
        or published_year
        or now_china().year
    )
    start = datetime(
        start_year,
        int(match.group("smonth")),
        int(match.group("sday")),
        int(match.group("shour")),
        int(match.group("sminute")),
        tzinfo=CHINA_TZ,
    )

    if match.group("emonth") and match.group("eday"):
        end_year = int(match.group("eyear") or start_year)
        end = datetime(
            end_year,
            int(match.group("emonth")),
            int(match.group("eday")),
            int(match.group("ehour")),
            int(match.group("eminute")),
            tzinfo=CHINA_TZ,
        )
        if end < start and not match.group("eyear"):
            end = end.replace(year=end.year + 1)
    else:
        end = start.replace(
            hour=int(match.group("ehour")), minute=int(match.group("eminute"))
        )
        if end <= start:
            end += timedelta(days=1)

    duration = end - start
    if duration <= timedelta(0) or duration > timedelta(days=3):
        raise SupervisorError(f"维护时长异常：{title}，{duration}")

    news_id = int(news["id"])
    return MaintenanceEvent(
        news_id=news_id,
        title=title,
        start=start,
        end=end,
        url=OFFICIAL_NEWS_URL.format(news_id=news_id),
    )


def fetch_event(news_id: int) -> MaintenanceEvent:
    payload = fetch_json(OFFICIAL_DETAIL_URL.format(news_id=news_id))
    if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        raise SupervisorError(f"官方公告详情接口失败：news_id={news_id}")
    return parse_event(payload["data"])


def fetch_upcoming_events(reference: datetime) -> list[MaintenanceEvent]:
    payload = fetch_json(
        OFFICIAL_LIST_URL,
        params={
            "gameExtensionId": GAME_EXTENSION_ID,
            "positionId": 2,
            "pageNum": 1,
            "pageSize": 50,
            "typeId": 1,
        },
    )
    if payload.get("code") != 0 or not isinstance(payload.get("data"), list):
        raise SupervisorError("官方公告列表接口失败")

    events: list[MaintenanceEvent] = []
    lower = reference - timedelta(days=2)
    upper = reference + timedelta(days=60)
    for item in payload["data"]:
        if not isinstance(item, dict) or "维护公告" not in str(item.get("title") or ""):
            continue
        guess = title_datetime(str(item.get("title") or ""))
        if guess and not (lower <= guess <= upper):
            continue
        try:
            event = fetch_event(int(item["id"]))
        except (SupervisorError, KeyError, TypeError, ValueError) as exc:
            logging.warning("跳过无法解析的公告：%s", exc)
            continue
        if event.end >= reference and event.start <= upper:
            events.append(event)
    return sorted(events, key=lambda event: event.start)


def read_alas_json(config: RuntimeConfig) -> tuple[str, str]:
    path = config.alas_root / "config" / "alas.json"
    try:
        with path.open("r", encoding="utf-8") as file:
            raw = json.load(file)
        emulator = raw["Alas"]["Emulator"]
        return str(emulator.get("ServerName") or "disabled"), str(
            emulator.get("PackageName") or "auto"
        )
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SupervisorError(f"无法读取 ALAS 配置 {path}：{exc}") from exc


def resolve_server_target(config: RuntimeConfig) -> ServerTarget:
    config_value = "unreadable"
    package_name = "unknown"
    try:
        config_value, package_name = read_alas_json(config)
    except SupervisorError as exc:
        logging.warning("%s；仍使用显式港区 %s", exc, config.server_name)

    all_names = {name for names in SERVER_GROUPS.values() for name in names}
    if config.server_name:
        if config.server_name not in all_names:
            raise SupervisorError(f"未知的国服港区：{config.server_name}")
        return ServerTarget(config_value, config.server_name, package_name, "explicit")

    if config_value != "disabled":
        try:
            group, index_text = config_value.rsplit("-", 1)
            if not group.startswith("cn_"):
                raise SupervisorError(f"当前 ALAS 服务器不是国服：{config_value}")
            return ServerTarget(
                config_value,
                SERVER_GROUPS[group][int(index_text)],
                package_name,
                "ALAS config",
            )
        except (ValueError, KeyError, IndexError) as exc:
            raise SupervisorError(f"无法解析 ServerName={config_value}：{exc}") from exc
    raise SupervisorError("未配置显式港区，且 ALAS ServerName=disabled")


def fetch_server_reading(target: ServerTarget) -> ServerReading:
    try:
        payload = fetch_json(
            SERVER_STATE_URL,
            params={"server_name": target.server_name},
            method="POST",
            timeout=15,
            bypass_proxy=True,
        )
    except SupervisorError as direct_error:
        logging.warning("服务器状态直连失败，尝试系统网络路径：%s", direct_error)
        payload = fetch_json(
            SERVER_STATE_URL,
            params={"server_name": target.server_name},
            method="POST",
            timeout=15,
            bypass_proxy=False,
        )
    try:
        state = int(payload["state"])
        last_update = int(payload["last_update"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SupervisorError(f"服务器状态返回异常：{payload}") from exc
    return ServerReading(
        server_name=target.server_name,
        state=state,
        last_update=last_update,
        age_seconds=int(time.time()) - last_update,
    )


class Docker:
    def __init__(self, container: str) -> None:
        self.container = container
        self.binary = shutil.which("docker")
        if not self.binary:
            raise SupervisorError("找不到 docker")

    def _run(self, *args: str, timeout: int = 30) -> str:
        result = run_command([self.binary, *args], timeout=timeout)
        return result.stdout.strip()

    def ensure_exists(self) -> None:
        self._run("inspect", self.container)

    def running(self) -> bool:
        return (
            self._run("inspect", "--format", "{{.State.Running}}", self.container)
            == "true"
        )

    def named_container_running(self, name: str) -> bool:
        result = run_command(
            [self.binary, "inspect", "--format", "{{.State.Running}}", name],
            allow_failure=True,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def restart_policy(self) -> dict[str, Any]:
        raw = self._run(
            "inspect", "--format", "{{json .HostConfig.RestartPolicy}}", self.container
        )
        try:
            value = json.loads(raw)
            return {
                "Name": str(value.get("Name") or "no"),
                "MaximumRetryCount": int(value.get("MaximumRetryCount") or 0),
            }
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise SupervisorError(f"无法解析 Docker 重启策略：{raw}") from exc

    def set_restart_policy(self, policy: dict[str, Any]) -> None:
        name = str(policy.get("Name") or "no")
        retries = int(policy.get("MaximumRetryCount") or 0)
        value = f"on-failure:{retries}" if name == "on-failure" and retries > 0 else name
        self._run("update", f"--restart={value}", self.container)

    def stop(self) -> None:
        if self.running():
            self._run(
                "stop",
                "--time",
                str(DOCKER_STOP_TIMEOUT_SECONDS),
                self.container,
                timeout=DOCKER_STOP_TIMEOUT_SECONDS + 30,
            )

    def start(self) -> None:
        if not self.running():
            self._run("start", self.container, timeout=60)


    # ALAS_SCHEDULER_MERGE_V1
    def container_started_at(self) -> str:
        if not self.running():
            return ""
        return self._run(
            "inspect",
            "--format",
            "{{.State.StartedAt}}",
            self.container,
        )

    def scheduler_request(self, action: str) -> dict[str, Any]:
        if action not in {"status", "start", "stop"}:
            raise SupervisorError(
                f"不支持的调度器操作：{action}"
            )

        if not self.running():
            raise SupervisorError(
                "ALAS 容器未运行，无法控制调度器"
            )

        method = "GET" if action == "status" else "POST"
        data_expression = (
            "None" if action == "status" else "b''"
        )
        url = (
            "http://127.0.0.1:22267/"
            f"__local_alas_scheduler__/v1/{action}"
        )

        python_code = (
            "import sys,urllib.request;"
            f"request=urllib.request.Request({url!r},"
            f"data={data_expression},method={method!r});"
            "response=urllib.request.urlopen("
            "request,timeout=10);"
            "sys.stdout.write("
            "response.read().decode('utf-8'))"
        )

        raw = self._run(
            "exec",
            self.container,
            "python",
            "-c",
            python_code,
            timeout=20,
        )

        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise SupervisorError(
                f"调度器响应无效：{raw!r}"
            ) from exc

        if (
            not isinstance(payload, dict)
            or not payload.get("ok")
        ):
            raise SupervisorError(
                f"调度器请求失败：{payload!r}"
            )

        return payload


def read_exact(sock: socket.socket, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise SupervisorError("ADB server 提前关闭连接")
        data.extend(chunk)
    return bytes(data)


def adb_devices(config: RuntimeConfig) -> dict[str, str]:
    service = b"host:devices"
    try:
        with socket.create_connection(
            (config.adb_server_host, config.adb_server_port), timeout=3
        ) as sock:
            sock.settimeout(3)
            sock.sendall(f"{len(service):04x}".encode("ascii") + service)
            status = read_exact(sock, 4)
            if status not in (b"OKAY", b"FAIL"):
                raise SupervisorError(f"ADB 响应无效：{status!r}")
            size = int(read_exact(sock, 4), 16)
            if size > 65536:
                raise SupervisorError("ADB 设备列表异常过大")
            body = read_exact(sock, size).decode("utf-8", "replace")
            if status == b"FAIL":
                raise SupervisorError(f"ADB server 报错：{body}")
    except (OSError, ValueError) as exc:
        raise SupervisorError(f"无法查询 ADB：{exc}") from exc

    devices: dict[str, str] = {}
    for line in body.splitlines():
        if "\t" in line:
            serial, state = line.split("\t", 1)
            devices[serial.strip()] = state.strip().split(" ", 1)[0]
    return devices


def adb_target_online(config: RuntimeConfig) -> bool:
    return adb_devices(config).get(config.adb_serial) == "device"


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SupervisorError(f"状态文件损坏：{path}：{exc}") from exc


def new_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "maintenance": {"phase": "idle"},
        "adb": {},
        "updated_at": now_text(),
    }


def normalize_state(value: dict[str, Any]) -> dict[str, Any]:
    value["version"] = STATE_VERSION
    if not isinstance(value.get("maintenance"), dict):
        value["maintenance"] = {"phase": "idle"}
    value["maintenance"].setdefault("phase", "idle")
    if not isinstance(value.get("adb"), dict):
        value["adb"] = {}
    return value


def migrate_legacy_state(config: RuntimeConfig) -> dict[str, Any]:
    state = new_state()
    legacy_adb = read_json_object(LEGACY_ADB_STATE)
    if legacy_adb:
        state["adb"] = {
            key: legacy_adb[key]
            for key in (
                "status",
                "offline_since",
                "confirmations",
                "last_recovered_at",
                "updated_at",
            )
            if key in legacy_adb
        }

    legacy_maintenance = read_json_object(LEGACY_MAINTENANCE_STATE)
    if legacy_maintenance:
        phase = str(legacy_maintenance.get("phase") or "idle")
        maintenance: dict[str, Any] = {"phase": phase if phase in {"armed", "holding"} else "idle"}
        for key in (
            "event",
            "stop_at",
            "entered_at",
            "was_running",
            "restart_policy",
            "available_confirmations",
            "stop_completed",
        ):
            if key in legacy_maintenance and maintenance["phase"] in {"armed", "holding"}:
                maintenance[key] = legacy_maintenance[key]
        if isinstance(legacy_maintenance.get("event"), dict) and phase not in {"armed", "holding"}:
            maintenance["last_event"] = legacy_maintenance["event"]
        if "completed_at" in legacy_maintenance:
            maintenance["last_completed_at"] = legacy_maintenance["completed_at"]
        state["maintenance"] = maintenance

    state["migrated_at"] = now_text()
    state["updated_at"] = now_text()
    save_state(config, state)
    logging.info(
        "已迁移旧状态：维护=%s，ADB=%s",
        state["maintenance"].get("phase"),
        state["adb"].get("status", "未初始化"),
    )
    return state


def load_state(config: RuntimeConfig, *, migrate: bool = False) -> dict[str, Any]:
    if not config.state_file.exists():
        return migrate_legacy_state(config) if migrate else new_state()
    return normalize_state(read_json_object(config.state_file))


def save_state(config: RuntimeConfig, value: dict[str, Any]) -> None:
    config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    value["version"] = STATE_VERSION
    value["updated_at"] = now_text()
    temp = config.state_file.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, config.state_file)


def read_mqtt_settings(config: RuntimeConfig) -> dict[str, str]:
    try:
        source = config.mqtt_config_file.read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError as exc:
        raise SupervisorError(
            f"无法从 {config.mqtt_config_file} 读取 MQTT 配置：{exc}"
        ) from exc

    settings: dict[str, str] = {}
    assignment = re.compile(
        r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$"
    )
    for line in source.splitlines():
        match = assignment.match(line)
        if not match or match.group(1) not in {
            "MQTT_HOST",
            "MQTT_PORT",
            "MQTT_USER",
            "MQTT_PASS",
        }:
            continue
        try:
            values = shlex.split(match.group(2), comments=True, posix=True)
        except ValueError as exc:
            raise SupervisorError(f"MQTT 配置 {match.group(1)} 无法解析：{exc}") from exc
        if len(values) != 1:
            raise SupervisorError(f"MQTT 配置 {match.group(1)} 无法安全解析")
        settings[match.group(1)] = values[0]

    missing = [name for name in ("MQTT_HOST",) if not settings.get(name)]
    if missing:
        raise SupervisorError("缺少 MQTT 配置：" + ", ".join(missing))
    settings.setdefault("MQTT_PORT", "1883")
    return settings


def publish_mqtt(
    config: RuntimeConfig,
    *,
    title: str,
    message: str,
    event: str = "recovered",
    downtime: Optional[str] = None,
) -> None:
    settings = read_mqtt_settings(config)
    mosquitto_pub = shutil.which("mosquitto_pub")
    if mosquitto_pub is None:
        raise SupervisorError("找不到 mosquitto_pub")

    payload: dict[str, Any] = {
        "event": event,
        "status": "online",
        "title": title,
        "message": message,
        "host": socket.gethostname(),
        "container": config.container,
        "adb_serial": config.adb_serial,
        "time": now_text(),
    }
    if downtime is not None:
        payload["downtime"] = downtime

    command = [
        mosquitto_pub,
        "-h",
        settings["MQTT_HOST"],
        "-p",
        settings["MQTT_PORT"],
        "-q",
        "1",
        "-t",
        config.mqtt_recovery_topic,
        "-m",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    ]
    if settings.get("MQTT_USER"):
        command.extend(["-u", settings["MQTT_USER"]])
    if settings.get("MQTT_PASS"):
        command.extend(["-P", settings["MQTT_PASS"]])

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode:
        error = (result.stderr or result.stdout).strip()
        raise SupervisorError(error or "MQTT 发布失败")


def send_onepush(docker: Docker, config: RuntimeConfig, title: str, content: str) -> None:
    notify_code = r'''
import json
import os
from module.notify import handle_notify

with open("config/alas.json", "r", encoding="utf-8") as file:
    data = json.load(file)
onepush_config = data.get("Alas", {}).get("Error", {}).get("OnePushConfig", "")
if not isinstance(onepush_config, str) or not onepush_config.strip():
    raise SystemExit(2)
success = handle_notify(
    onepush_config,
    title=os.environ["NOTICE_TITLE"],
    content=os.environ["NOTICE_CONTENT"],
)
raise SystemExit(0 if success else 1)
'''
    result = subprocess.run(
        [
            docker.binary,
            "exec",
            "-i",
            "-e",
            f"NOTICE_TITLE={title}",
            "-e",
            f"NOTICE_CONTENT={content}",
            config.container,
            "sh",
            "-lc",
            "cd /app/AzurLaneAutoScript && python -",
        ],
        input=notify_code,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise SupervisorError(detail or "OnePush 通知失败")


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.file: Optional[Any] = None

    def acquire(self) -> None:
        if self.file is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            file.close()
            raise SupervisorError(f"已有另一个 supervisor 实例：{self.path}") from exc
        self.file = file

    def release(self) -> None:
        if self.file is None:
            return
        try:
            fcntl.flock(self.file, fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None


class SupervisorDaemon:
    HISTORY_KEYS = (
        "last_event",
        "last_completed_at",
        "last_container_started",
        "last_adb_online",
        "last_scheduler_started",
        "last_manual_release_at",
    )

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.docker = Docker(config.container)
        self.stop_requested = False
        self.last_schedule_refresh = 0.0
        self.last_server_poll = 0.0
        self.cached_events: list[MaintenanceEvent] = []
        self.state = load_state(config, migrate=True)

    def request_stop(self, _signum: int, _frame: Any) -> None:
        self.stop_requested = True

    def save(self) -> None:
        save_state(self.config, self.state)

    def maintenance_with_history(self, **values: Any) -> dict[str, Any]:
        current = self.state.get("maintenance", {})
        result = {key: current[key] for key in self.HISTORY_KEYS if key in current}
        result.update(values)
        return result

    def run(self) -> int:
        singleton = FileLock(DAEMON_LOCK)
        singleton.acquire()
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        self.docker.ensure_exists()
        logging.info(
            "统一守卫启动：容器=%s，ADB=%s，港区=%s，提前=%s分钟",
            self.config.container,
            self.config.adb_serial,
            self.config.server_name,
            LEAD_MINUTES,
        )
        try:
            while not self.stop_requested:
                try:
                    delay = self.step()
                except Exception as exc:  # Keep safety monitoring alive on transient failures.
                    logging.exception("本轮检查失败：%s", exc)
                    delay = ADB_POLL_SECONDS
                deadline = time.monotonic() + max(1.0, float(delay))
                while not self.stop_requested and time.monotonic() < deadline:
                    time.sleep(min(1.0, deadline - time.monotonic()))
            return 0
        finally:
            if self.state["maintenance"].get("phase") == "holding":
                logging.warning("服务退出时仍处于维护期；ALAS 调度器保持暂停，重启后继续续管")
            singleton.release()

    def step(self) -> float:
        reference = now_china()
        maintenance = self.state["maintenance"]
        if maintenance.get("phase") == "holding":
            return self.step_holding(reference)

        # A saved stop deadline always wins over a potentially slow network refresh.
        if maintenance.get("phase") == "armed" and isinstance(maintenance.get("event"), dict):
            saved_event = MaintenanceEvent.from_dict(maintenance["event"])
            if reference >= saved_event.stop_at:
                self.enter_hold(saved_event, reference)
                return 1

        try:
            self.update_schedule(reference)
        except SupervisorError as exc:
            logging.warning("维护公告检查失败；ADB 监控继续运行：%s", exc)

        if self.state["maintenance"].get("phase") == "holding":
            return 1

        try:
            self.monitor_adb()
        except SupervisorError as exc:
            logging.warning("ADB 检查失败：%s", exc)

        delay = float(ADB_POLL_SECONDS)
        maintenance = self.state["maintenance"]
        if maintenance.get("phase") == "armed" and isinstance(maintenance.get("event"), dict):
            event = MaintenanceEvent.from_dict(maintenance["event"])
            delay = min(delay, max(1.0, (event.stop_at - reference).total_seconds()))
        return delay

    def refresh_schedule(self, reference: datetime) -> None:
        now_mono = time.monotonic()
        if now_mono - self.last_schedule_refresh < SCHEDULE_REFRESH_SECONDS:
            return
        self.last_schedule_refresh = now_mono
        self.cached_events = fetch_upcoming_events(reference)
        logging.info("官方维护公告刷新完成，可用未来时段=%d", len(self.cached_events))

    def update_schedule(self, reference: datetime) -> None:
        maintenance = self.state["maintenance"]
        saved_event: Optional[MaintenanceEvent] = None
        if maintenance.get("phase") == "armed" and isinstance(maintenance.get("event"), dict):
            saved_event = MaintenanceEvent.from_dict(maintenance["event"])

        try:
            self.refresh_schedule(reference)
        except SupervisorError:
            if saved_event is None:
                raise
            logging.warning("官方公告刷新失败，继续使用已保存的维护预约")

        event = next((item for item in self.cached_events if item.end >= reference), None)
        if event is None and saved_event is not None and saved_event.end >= reference:
            event = saved_event

        if event is None:
            if maintenance.get("phase") == "armed":
                self.state["maintenance"] = self.maintenance_with_history(phase="idle")
                self.save()
            return

        if reference >= event.stop_at:
            self.enter_hold(event, reference)
            return

        armed = self.maintenance_with_history(
            phase="armed",
            event=event.to_dict(),
            stop_at=event.stop_at.isoformat(),
        )
        if maintenance != armed:
            self.state["maintenance"] = armed
            self.save()
            logging.info(
                "已预约维护：%s；将在 %s 暂停 ALAS 调度器",
                event.title,
                format_dt(event.stop_at),
            )

    def enter_hold(self, event: MaintenanceEvent, reference: datetime) -> None:
        target = resolve_server_target(self.config)
        running = self.docker.running()
        self.state["maintenance"] = self.maintenance_with_history(
            phase="holding",
            event=event.to_dict(),
            entered_at=reference.isoformat(),
            was_running=running,
            available_confirmations=0,
            scheduler_stop_completed=False,
        )
        self.save()
        self.enforce_hold(reference)
        logging.warning(
            "维护期调度器暂停已生效：%s；港区=%s；计划维护 %s ~ %s",
            event.title,
            target.server_name,
            format_dt(event.start),
            format_dt(event.end),
        )

    # ALAS_MAINTENANCE_SCHEDULER_ONLY_V1
    def enforce_hold(self, reference: datetime) -> None:
        maintenance = self.state["maintenance"]

        # 维护期间不停止容器，也不修改 Docker 重启策略。
        # 若容器在维护期间重启，下一轮检查会再次暂停调度器。
        if not self.docker.running():
            if maintenance.get("scheduler_stop_completed") is not False:
                maintenance["scheduler_stop_completed"] = False
                maintenance["last_enforced_at"] = reference.isoformat()
                self.save()
            return

        scheduler = self.docker.scheduler_request("stop")

        if scheduler.get("running"):
            raise SupervisorError(
                "维护期内 ALAS 调度器未能暂停"
            )

        action = scheduler.get("action", "unknown")
        changed = (
            maintenance.get("scheduler_stop_completed") is not True
            or action == "stopped"
        )

        if changed:
            maintenance["scheduler_stop_completed"] = True
            maintenance["last_enforced_at"] = reference.isoformat()
            self.save()

            logging.warning(
                "维护期：ALAS 调度器=%s；容器保持运行",
                action,
            )

    def refresh_held_event(self, event: MaintenanceEvent) -> MaintenanceEvent:
        now_mono = time.monotonic()
        if now_mono - self.last_schedule_refresh < SCHEDULE_REFRESH_SECONDS:
            return event
        self.last_schedule_refresh = now_mono
        try:
            current = fetch_event(event.news_id)
        except SupervisorError as exc:
            logging.warning("维护公告详情刷新失败，沿用已保存时段：%s", exc)
            return event
        if current.start != event.start or current.end != event.end:
            logging.warning(
                "官方维护时段已更新：%s ~ %s",
                format_dt(current.start),
                format_dt(current.end),
            )
        return current

    def step_holding(self, reference: datetime) -> float:
        self.enforce_hold(reference)
        maintenance = self.state["maintenance"]
        event = MaintenanceEvent.from_dict(maintenance["event"])
        event = self.refresh_held_event(event)
        if event.to_dict() != maintenance.get("event"):
            maintenance["event"] = event.to_dict()
            self.save()

        if reference < event.end:
            return min(
                float(ADB_POLL_SECONDS),
                max(1.0, (event.end - reference).total_seconds()),
            )

        now_mono = time.monotonic()
        elapsed = now_mono - self.last_server_poll
        if elapsed < SERVER_POLL_SECONDS:
            return max(1.0, SERVER_POLL_SECONDS - elapsed)
        self.last_server_poll = now_mono

        try:
            target = resolve_server_target(self.config)
            reading = fetch_server_reading(target)
            logging.info(
                "开服确认：港区=%s state=%d 状态年龄=%ds available=%s",
                reading.server_name,
                reading.state,
                reading.age_seconds,
                reading.available,
            )
            confirmations = int(maintenance.get("available_confirmations") or 0)
            confirmations = confirmations + 1 if reading.available else 0
        except SupervisorError as exc:
            logging.warning("开服状态暂不可确认：%s", exc)
            confirmations = 0

        maintenance["available_confirmations"] = confirmations
        self.save()
        if confirmations >= SERVER_AVAILABLE_CONFIRMATIONS:
            self.release_after_maintenance(reference)
        return float(SERVER_POLL_SECONDS)

    def release_after_maintenance(
        self,
        reference: datetime,
    ) -> None:
        maintenance = self.state["maintenance"]

        try:
            online = adb_target_online(self.config)
        except SupervisorError as exc:
            logging.warning(
                "开服后 ADB 状态无法确认：%s",
                exc,
            )
            online = False

        scheduler_started = False

        if not online:
            logging.warning(
                "服务器已恢复，但 ADB 离线；"
                "调度器保持暂停，等待 ADB 稳定恢复"
            )
        elif not self.docker.running():
            logging.warning(
                "服务器已恢复，但 ALAS 容器未运行；"
                "不主动启动容器，交由正常监控处理"
            )
        else:
            scheduler = self.docker.scheduler_request("start")
            scheduler_started = bool(scheduler.get("running"))

            if not scheduler_started:
                raise SupervisorError(
                    "服务器已恢复，但 ALAS 调度器启动失败"
                )

            logging.warning(
                "服务器已恢复；ALAS 调度器=%s；容器保持运行",
                scheduler.get("action", "unknown"),
            )

        event = maintenance.get("event")
        self.state["maintenance"] = {
            "phase": "idle",
            "last_event": event,
            "last_completed_at": reference.isoformat(),
            "last_container_started": False,
            "last_scheduler_started": scheduler_started,
            "last_adb_online": online,
        }
        self.save()

    def monitor_adb(self) -> None:
        online = adb_target_online(self.config)
        running = self.docker.running()
        adb_state = self.state["adb"]
        current_time = time.time()
        previous_status = adb_state.get("status")

        # ADB 离线时保留容器，但持续确保调度器停止。
        # 持续检查可以覆盖“离线期间 ALAS 容器重启”的情况。
        if not online:
            offline_since = (
                adb_state.get("offline_since")
                if previous_status == "offline"
                else current_time
            )
            offline_since = offline_since or current_time
            scheduler_action = "container_not_running"
            started_at = ""

            if running:
                started_at = self.docker.container_started_at()
                scheduler = self.docker.scheduler_request("stop")

                if scheduler.get("running"):
                    raise SupervisorError(
                        "ADB 离线，但调度器未进入停止状态"
                    )

                scheduler_action = scheduler.get(
                    "action",
                    "unknown",
                )

            if (
                previous_status != "offline"
                or scheduler_action == "stopped"
            ):
                logging.warning(
                    "MuMu ADB 离线：%s；调度器=%s",
                    self.config.adb_serial,
                    scheduler_action,
                )

            self.state["adb"] = {
                "status": "offline",
                "offline_since": offline_since,
                "confirmations": 0,
                "container_started_at": started_at,
                "updated_at": now_text(),
            }
            self.save()
            return

        # 首次建立在线基线。
        if not adb_state:
            if not running:
                logging.warning(
                    "ADB 在线；正在启动 ALAS 容器"
                )
                self.docker.start()
                running = self.docker.running()

            if not running:
                raise SupervisorError(
                    "ADB 在线，但 ALAS 容器启动失败"
                )

            scheduler = self.docker.scheduler_request("status")
            if not scheduler.get("running"):
                scheduler = self.docker.scheduler_request("start")

            self.state["adb"] = {
                "status": "online",
                "offline_since": None,
                "confirmations": 0,
                "container_started_at": (
                    self.docker.container_started_at()
                ),
                "updated_at": now_text(),
            }
            self.save()
            logging.info(
                "ADB 基线已建立：ADB=online，ALAS=running，"
                "调度器=%s",
                scheduler.get("action", "running"),
            )
            return

        # 从离线恢复后先累计稳定确认，不提前打开调度器。
        if previous_status == "offline":
            self.state["adb"] = {
                "status": "recovering",
                "offline_since": adb_state.get(
                    "offline_since"
                ),
                "confirmations": 1,
                "container_started_at": (
                    self.docker.container_started_at()
                    if running
                    else ""
                ),
                "updated_at": now_text(),
            }
            self.save()
            logging.info(
                "MuMu ADB 已返回，等待稳定确认 1/%d",
                ADB_STABLE_CONFIRMATIONS,
            )
            return

        if previous_status == "recovering":
            confirmations = (
                int(adb_state.get("confirmations") or 0) + 1
            )

            if confirmations < ADB_STABLE_CONFIRMATIONS:
                adb_state["confirmations"] = confirmations
                adb_state["updated_at"] = now_text()
                self.save()
                logging.info(
                    "ADB 恢复确认 %d/%d",
                    confirmations,
                    ADB_STABLE_CONFIRMATIONS,
                )
                return

            if not running:
                logging.warning(
                    "ADB 已稳定恢复；正在启动 ALAS 容器"
                )
                self.docker.start()
                running = self.docker.running()

            if not running:
                raise SupervisorError(
                    "ADB 已恢复，但 ALAS 容器启动失败"
                )

            scheduler = self.docker.scheduler_request("start")

            if not scheduler.get("running"):
                raise SupervisorError(
                    "ALAS 调度器未进入运行状态"
                )

            offline_since = (
                adb_state.get("offline_since")
                or current_time
            )
            downtime = format_duration(
                current_time - float(offline_since)
            )
            title = "ALAS 调度器已恢复运行"
            message = (
                "MuMu 模拟器已重新上线，"
                "ALAS 调度器已恢复运行\n"
                f"设备：{socket.gethostname()}\n"
                f"容器：{self.config.container}\n"
                f"ADB：{self.config.adb_serial}\n"
                f"中断时长：约 {downtime}\n"
                f"恢复时间：{now_text()}"
            )

            publish_mqtt(
                self.config,
                title=title,
                message=message,
                event="recovered",
                downtime=downtime,
            )

            self.state["adb"] = {
                "status": "online",
                "offline_since": None,
                "confirmations": 0,
                "container_started_at": (
                    self.docker.container_started_at()
                ),
                "last_recovered_at": now_text(),
                "updated_at": now_text(),
            }
            self.save()
            logging.warning(
                "ALAS 调度器恢复：%s；MQTT 恢复通知已发布",
                scheduler.get("action", "unknown"),
            )
            return

        # 正常在线时检测容器启动时间。
        # 只有容器确实重新启动才自动打开调度器，
        # 不会覆盖用户手动停止调度器的操作。
        if not running:
            logging.warning(
                "ADB 在线；正在启动 ALAS 容器"
            )
            self.docker.start()
            running = self.docker.running()

        if not running:
            raise SupervisorError(
                "ADB 在线，但 ALAS 容器启动失败"
            )

        started_at = self.docker.container_started_at()
        previous_started_at = adb_state.get(
            "container_started_at"
        )

        if not previous_started_at:
            scheduler = self.docker.scheduler_request("status")
            if not scheduler.get("running"):
                scheduler = self.docker.scheduler_request("start")

            adb_state["container_started_at"] = started_at
            adb_state["status"] = "online"
            adb_state["updated_at"] = now_text()
            self.save()
            logging.info(
                "已记录 ALAS 容器启动时间；调度器=%s",
                scheduler.get("action", "running"),
            )
            return

        if started_at != previous_started_at:
            scheduler = self.docker.scheduler_request("start")

            if not scheduler.get("running"):
                raise SupervisorError(
                    "ALAS 重启后调度器未恢复运行"
                )

            adb_state["container_started_at"] = started_at
            adb_state["status"] = "online"
            adb_state["updated_at"] = now_text()
            self.save()
            logging.warning(
                "检测到 ALAS 容器重新启动；调度器=%s",
                scheduler.get("action", "unknown"),
            )
            return

        if previous_status != "online":
            adb_state["status"] = "online"
            adb_state["offline_since"] = None
            adb_state["confirmations"] = 0
            adb_state["container_started_at"] = started_at
            adb_state["updated_at"] = now_text()
            self.save()


def check_status(config: RuntimeConfig) -> int:
    print(f"ALAS unified supervisor v{VERSION}")
    reference = now_china()
    print(f"北京时间: {format_dt(reference)}")

    docker = Docker(config.container)
    docker.ensure_exists()
    print(f"ALAS 容器: {'running' if docker.running() else 'stopped'}")
    print(f"Docker 重启策略: {docker.restart_policy()}")
    print(
        "ADB 基础容器: "
        f"failover={'running' if docker.named_container_running('alas-adb-failover') else 'not-running'}, "
        f"watcher={'running' if docker.named_container_running('alas-adb-watcher') else 'not-running'}"
    )

    state = load_state(config, migrate=False)
    maintenance = state["maintenance"]
    adb_state = state["adb"]
    print(f"维护状态: {maintenance.get('phase', 'idle')}")
    if isinstance(maintenance.get("event"), dict):
        event = MaintenanceEvent.from_dict(maintenance["event"])
        print(f"当前维护公告: {event.title}")
        print(f"当前维护时段: {format_dt(event.start)} ~ {format_dt(event.end)}")
    if isinstance(maintenance.get("last_event"), dict):
        event = MaintenanceEvent.from_dict(maintenance["last_event"])
        print(f"上次维护时段: {format_dt(event.start)} ~ {format_dt(event.end)}")
        print(f"上次确认开服: {maintenance.get('last_completed_at', 'unknown')}")
    print(f"ADB 监控状态: {adb_state.get('status', 'not-initialized')}")

    target = resolve_server_target(config)
    print(
        f"ALAS ServerName: {target.config_value}; 港区: {target.server_name}; "
        f"来源: {target.source}; PackageName: {target.package_name}"
    )
    reading = fetch_server_reading(target)
    print(
        f"服务器状态: state={reading.state}; last_update_age={reading.age_seconds}s; "
        f"available={reading.available}"
    )

    events = fetch_upcoming_events(reference)
    if events:
        event = events[0]
        print(f"下一维护公告: {event.title}")
        print(f"调度器暂停时间: {format_dt(event.stop_at)}")
        print(f"维护时段: {format_dt(event.start)} ~ {format_dt(event.end)}")
        print(f"公告链接: {event.url}")
    else:
        print("下一维护公告: 当前未发现尚未结束的维护时段")

    devices = adb_devices(config)
    print(f"ADB devices: {devices or '(none)'}")
    print(
        f"目标 ADB {config.adb_serial}: "
        f"{'online' if devices.get(config.adb_serial) == 'device' else 'offline'}"
    )
    return 0


def show_history(config: RuntimeConfig) -> int:
    state = load_state(config, migrate=False)
    maintenance = state["maintenance"]
    event_raw = maintenance.get("last_event")
    if not isinstance(event_raw, dict):
        print("尚无由统一守卫完成的维护记录")
        return 0
    event = MaintenanceEvent.from_dict(event_raw)
    print(f"公告: {event.title}")
    print(f"维护时段: {format_dt(event.start)} ~ {format_dt(event.end)}")
    print(f"确认开服: {maintenance.get('last_completed_at', 'unknown')}")
    print(f"公告链接: {event.url}")
    return 0


def manual_release(config: RuntimeConfig, force_start: bool) -> int:
    state = load_state(config, migrate=True)
    maintenance = state["maintenance"]
    docker = Docker(config.container)
    docker.ensure_exists()
    policy = maintenance.get("restart_policy")
    if isinstance(policy, dict):
        docker.set_restart_policy(policy)
    if force_start:
        docker.start()
    history = {
        key: maintenance[key]
        for key in SupervisorDaemon.HISTORY_KEYS
        if key in maintenance
    }
    history.update({"phase": "idle", "last_manual_release_at": now_text()})
    state["maintenance"] = history
    save_state(config, state)
    print("维护状态已手动解除。服务运行时，尚未结束的公告可能再次触发锁定。")
    return 0


def test_mqtt(config: RuntimeConfig) -> int:
    publish_mqtt(
        config,
        title="ALAS 恢复通知测试",
        message=(
            "MQTT 与 Home Assistant 通知链测试成功\n"
            f"设备：{socket.gethostname()}\n"
            f"ADB：{config.adb_serial}\n"
            f"测试时间：{now_text()}"
        ),
        event="test",
    )
    print(f"{now_text()} MQTT recovery notification test published")
    return 0


def test_onepush(config: RuntimeConfig) -> int:
    docker = Docker(config.container)
    if not docker.running():
        raise SupervisorError("ALAS 容器未运行")
    send_onepush(
        docker,
        config,
        "ALAS 恢复通知测试",
        (
            "恢复通知配置测试成功\n"
            f"设备：{socket.gethostname()}\n"
            f"容器：{config.container}\n"
            f"ADB：{config.adb_serial}\n"
            f"测试时间：{now_text()}"
        ),
    )
    print(f"{now_text()} Legacy OnePush/SMTP notification test succeeded")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--daemon", action="store_true", help="持续运行统一守卫")
    mode.add_argument("--check", action="store_true", help="只读检查所有数据源")
    mode.add_argument("--history", action="store_true", help="查看上次维护记录")
    mode.add_argument("--release", action="store_true", help="手动解除维护状态")
    mode.add_argument("--test-mqtt", action="store_true", help="发送 MQTT 测试通知")
    mode.add_argument("--test-notify", action="store_true", help="发送旧 OnePush 测试通知")
    mode.add_argument("--version", action="store_true", help="显示版本")
    parser.add_argument("--force-start", action="store_true", help="与 --release 同用并启动 ALAS")
    parser.add_argument(
        "--container", default=os.environ.get("ALAS_CONTAINER", ALAS_CONTAINER)
    )
    parser.add_argument(
        "--alas-root",
        type=Path,
        default=Path(os.environ.get("ALAS_ROOT", str(ALAS_ROOT))),
    )
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    parser.add_argument(
        "--adb-host",
        default=os.environ.get("ALAS_ADB_SERVER_HOST", ADB_SERVER[0]),
    )
    parser.add_argument(
        "--adb-port",
        type=int,
        default=int(os.environ.get("ALAS_ADB_SERVER_PORT", str(ADB_SERVER[1]))),
    )
    parser.add_argument(
        "--adb-serial", default=os.environ.get("ALAS_ADB_SERIAL", ADB_SERIAL)
    )
    parser.add_argument(
        "--server-name",
        default=os.environ.get("ALAS_SERVER_NAME", DEFAULT_SERVER_NAME),
    )
    parser.add_argument(
        "--mqtt-config",
        type=Path,
        default=Path(os.environ.get("ALAS_MQTT_CONFIG", str(MQTT_CONFIG_FILE))),
    )
    parser.add_argument(
        "--mqtt-topic",
        default=os.environ.get(
            "ALAS_MQTT_RECOVERY_TOPIC", MQTT_RECOVERY_TOPIC
        ),
    )
    return parser


def main() -> int:
    load_local_environment()
    args = build_parser().parse_args()
    if args.version:
        print(VERSION)
        return 0
    if args.force_start and not args.release:
        raise SupervisorError("--force-start 只能与 --release 同用")

    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    config = RuntimeConfig(
        container=args.container,
        alas_root=args.alas_root,
        state_dir=args.state_dir,
        adb_server_host=args.adb_host,
        adb_server_port=args.adb_port,
        adb_serial=args.adb_serial,
        server_name=args.server_name,
        mqtt_config_file=args.mqtt_config,
        mqtt_recovery_topic=args.mqtt_topic,
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.check:
        return check_status(config)
    if args.history:
        return show_history(config)
    if args.release:
        return manual_release(config, args.force_start)
    if args.test_mqtt:
        return test_mqtt(config)
    if args.test_notify:
        return test_onepush(config)
    return SupervisorDaemon(config).run()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SupervisorError, RuntimeError, OSError, ValueError, KeyError) as exc:
        logging.error("%s", exc)
        sys.exit(1)
