#!/usr/bin/env python3
"""Render, display and probe the privacy-local ALAS ADB failover config."""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import shlex
import socket
import tempfile
from pathlib import Path


VERSION = "1.0.0"
DEFAULT_CONFIG = Path(
    os.environ.get(
        "ALAS_ADB_FAILOVER_CONFIG",
        "/etc/alas-adb-failover.conf",
    )
)
HOSTNAME_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}"
    r"[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}"
    r"[A-Za-z0-9])?\Z"
)


class ConfigError(RuntimeError):
    pass


def read_config(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取配置 {path}: {exc}") from exc

    result: dict[str, str] = {}
    assignment = re.compile(
        r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$"
    )
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = assignment.match(line)
        if not match:
            raise ConfigError(f"第 {number} 行不是安全的 KEY=VALUE 配置")
        try:
            values = shlex.split(match.group(2), comments=True, posix=True)
        except ValueError as exc:
            raise ConfigError(f"第 {number} 行无法解析: {exc}") from exc
        if len(values) != 1:
            raise ConfigError(f"第 {number} 行必须只有一个值")
        result[match.group(1)] = values[0]
    return result


def valid_host(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return bool(HOSTNAME_RE.fullmatch(value))


def endpoint(host: str, port: int) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return f"{host}:{port}"
    if address.version == 6:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def settings(path: Path) -> dict[str, str | int]:
    raw = read_config(path)
    result: dict[str, str | int] = {
        "fixed_host": raw.get("ADB_FIXED_HOST", "127.0.0.1"),
        "fixed_port": raw.get("ADB_FIXED_PORT", "16555"),
        "primary_host": raw.get("ADB_PRIMARY_HOST", ""),
        "primary_port": raw.get("ADB_PRIMARY_PORT", ""),
        "backup_host": raw.get("ADB_BACKUP_HOST", ""),
        "backup_port": raw.get("ADB_BACKUP_PORT", ""),
        "connect_timeout": raw.get("ADB_CONNECT_TIMEOUT_SECONDS", "2"),
    }

    for key in ("fixed_host", "primary_host", "backup_host"):
        value = str(result[key])
        if not value or not valid_host(value):
            raise ConfigError(f"{key} 不是有效 IP 或域名")

    for key in (
        "fixed_port",
        "primary_port",
        "backup_port",
        "connect_timeout",
    ):
        try:
            number = int(str(result[key]))
        except ValueError as exc:
            raise ConfigError(f"{key} 必须是整数") from exc
        maximum = 300 if key == "connect_timeout" else 65535
        if not 1 <= number <= maximum:
            raise ConfigError(f"{key} 超出有效范围")
        result[key] = number
    return result


def render(value: dict[str, str | int]) -> str:
    fixed = endpoint(str(value["fixed_host"]), int(value["fixed_port"]))
    primary = endpoint(
        str(value["primary_host"]), int(value["primary_port"])
    )
    backup = endpoint(
        str(value["backup_host"]), int(value["backup_port"])
    )
    return f"""\
global
    log stdout format raw local0

defaults
    log global
    mode tcp
    option tcplog
    option log-health-checks
    timeout connect {int(value["connect_timeout"])}s
    timeout client 1d
    timeout server 1d

frontend adb_stable
    bind {fixed}
    default_backend mumu_adb

backend mumu_adb
    mode tcp
    option tcp-check
    default-server inter 2s fall 2 rise 1 on-marked-down shutdown-sessions

    server adb_primary {primary} check
    server adb_backup {backup} check backup
"""


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def tcp_state(host: str, port: int, timeout: int) -> str:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "online"
    except OSError:
        return "offline"


def show(value: dict[str, str | int], probe: bool = False) -> None:
    rows = (
        ("固定入口", "fixed_host", "fixed_port"),
        ("主设备", "primary_host", "primary_port"),
        ("备用设备", "backup_host", "backup_port"),
    )
    for label, host_key, port_key in rows:
        host = str(value[host_key])
        port = int(value[port_key])
        suffix = ""
        if probe:
            suffix = " " + tcp_state(
                host,
                port,
                int(value["connect_timeout"]),
            )
        print(f"{label}: {endpoint(host, port)}{suffix}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--version", action="version", version=VERSION)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("show", help="显示固定、主、备用端点")
    subparsers.add_parser("status", help="显示端点并进行 TCP 探测")
    render_parser = subparsers.add_parser("render", help="生成 HAProxy 配置")
    render_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    value = settings(args.config)
    if args.command == "show":
        show(value)
    elif args.command == "status":
        show(value, probe=True)
    else:
        atomic_write(args.output, render(value))
        print(f"HAProxy 配置已生成：{args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as exc:
        print(f"ADB 故障切换配置错误：{exc}", file=os.sys.stderr)
        raise SystemExit(1)
