#!/usr/bin/env python3

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import stat
import subprocess
import sys
import tempfile
import time


def load_local_environment():
    path = Path(
        os.environ.get(
            "ALAS_SCHEDULER_ENV_FILE",
            "/etc/default/alas-scheduler-control",
        )
    )
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError(
            f"无法读取本地配置 {path}: {exc}"
        ) from exc

    allowed = {
        "ALAS_ROOT",
        "ALAS_CONTAINER",
        "ALAS_SCHEDULER_PAYLOAD",
        "ALAS_SCHEDULER_STATE_DIR",
        "ALAS_SCHEDULER_BACKUP_DIR",
        "ALAS_SCHEDULER_STABLE_SECONDS",
    }
    assignment = re.compile(
        r"^\s*(?:export\s+)?"
        r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$"
    )
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = assignment.match(line)
        if not match:
            raise RuntimeError(
                f"本地配置第 {number} 行格式无效"
            )
        name = match.group(1)
        if name not in allowed or name in os.environ:
            continue
        values = shlex.split(
            match.group(2), comments=True, posix=True
        )
        if len(values) > 1:
            raise RuntimeError(
                f"本地配置第 {number} 行必须只有一个值"
            )
        os.environ[name] = values[0] if values else ""


load_local_environment()


BASE = Path(
    os.environ.get("ALAS_ROOT", "/opt/AzurLaneAutoScript")
)
APP_FILE = BASE / "module/webui/app.py"
MODULE_FILE = BASE / "module/webui/local_scheduler_control.py"
PROCESS_MANAGER = BASE / "module/webui/process_manager.py"

PAYLOAD_FILE = Path(
    os.environ.get(
        "ALAS_SCHEDULER_PAYLOAD",
        "/opt/lib/alas-scheduler-control/"
        "local_scheduler_control.py",
    )
)

STATE_DIR = Path(
    os.environ.get(
        "ALAS_SCHEDULER_STATE_DIR",
        "/var/lib/alas-scheduler-repair",
    )
)
BLOCK_FILE = STATE_DIR / "blocked.json"
BACKUP_ROOT = Path(
    os.environ.get(
        "ALAS_SCHEDULER_BACKUP_DIR",
        "/var/backups/alas-scheduler-control",
    )
)

CONTAINER = os.environ.get("ALAS_CONTAINER", "alas")
STABLE_SECONDS = int(
    os.environ.get("ALAS_SCHEDULER_STABLE_SECONDS", "90")
)

IMPORT_LINE = (
    "from module.webui.local_scheduler_control "
    "import LocalSchedulerControl"
)
RETURN_LINE = (
    "return LocalSchedulerControl(app, updater.event)"
)


class RepairError(RuntimeError):
    pass


def run(command, timeout=30, check=False):
    result = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )

    if check and result.returncode != 0:
        message = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"exit={result.returncode}"
        )
        raise RepairError(message)

    return result


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        reference = path.stat()
        mode = stat.S_IMODE(reference.st_mode)
        uid = reference.st_uid
        gid = reference.st_gid
    else:
        reference = path.parent.stat()
        mode = 0o644
        uid = reference.st_uid
        gid = reference.st_gid

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )

    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())

        os.chmod(temporary_name, mode)
        os.chown(temporary_name, uid, gid)
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def docker_running():
    result = run(
        [
            "docker",
            "inspect",
            "--format",
            "{{.State.Running}}",
            CONTAINER,
        ],
        timeout=15,
    )
    return (
        result.returncode == 0
        and result.stdout.strip() == "true"
    )


def restart_container():
    run(
        ["docker", "restart", CONTAINER],
        timeout=150,
        check=True,
    )


def scheduler_status():
    code = (
        "import json,urllib.request;"
        "u='http://127.0.0.1:22267/"
        "__local_alas_scheduler__/v1/status';"
        "r=urllib.request.urlopen(u,timeout=10);"
        "p=json.loads(r.read().decode('utf-8'));"
        "assert p.get('ok') is True;"
        "print(json.dumps(p,separators=(',',':')))"
    )

    return run(
        [
            "docker",
            "exec",
            CONTAINER,
            "python",
            "-c",
            code,
        ],
        timeout=20,
    )


def wait_for_interface(timeout=120):
    deadline = time.monotonic() + timeout
    last_error = ""

    while time.monotonic() < deadline:
        if docker_running():
            result = scheduler_status()

            if result.returncode == 0:
                try:
                    payload = json.loads(
                        result.stdout.strip()
                    )
                except ValueError:
                    payload = {}

                if payload.get("ok") is True:
                    return payload

            last_error = (
                result.stderr.strip()
                or result.stdout.strip()
                or "接口尚未就绪"
            )

        time.sleep(2)

    raise RepairError(
        "调度器接口在等待期内未恢复："
        + last_error[-500:]
    )


def process_manager_compatible():
    try:
        tree = ast.parse(
            PROCESS_MANAGER.read_text(encoding="utf-8")
        )
    except (OSError, SyntaxError) as exc:
        raise RepairError(
            f"无法解析 ProcessManager：{exc}"
        ) from exc

    classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "ProcessManager"
    ]

    if len(classes) != 1:
        raise RepairError(
            "没有唯一识别到 ProcessManager 类"
        )

    methods = {
        node.name
        for node in classes[0].body
        if isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef),
        )
    }

    missing = {"start", "stop"} - methods
    if missing:
        raise RepairError(
            "ProcessManager 接口发生变化，缺少："
            + ", ".join(sorted(missing))
        )


def hook_present(text):
    return (
        IMPORT_LINE in text
        and RETURN_LINE in text
    )


def patched_app_text(text):
    if hook_present(text):
        return text

    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise RepairError(
            f"新版 app.py 语法异常：{exc}"
        ) from exc

    candidates = []

    for node in ast.walk(tree):
        if not isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            continue

        creates_app = False
        return_nodes = []

        for statement in node.body:
            if isinstance(statement, ast.Assign):
                assigns_app = any(
                    isinstance(target, ast.Name)
                    and target.id == "app"
                    for target in statement.targets
                )
                value = statement.value
                calls_asgi = (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "asgi_app"
                )

                if assigns_app and calls_asgi:
                    creates_app = True

            if (
                isinstance(statement, ast.Return)
                and isinstance(statement.value, ast.Name)
                and statement.value.id == "app"
            ):
                return_nodes.append(statement)

        if creates_app:
            candidates.extend(return_nodes)

    if len(candidates) != 1:
        raise RepairError(
            "新版 app.py 接入结构发生变化："
            f"找到 {len(candidates)} 个候选位置；"
            "已停止自动修改"
        )

    target = candidates[0]
    lines = text.splitlines(keepends=True)

    start = target.lineno - 1
    end = target.end_lineno or target.lineno

    original_line = lines[start]
    indentation = original_line[
        : len(original_line) - len(original_line.lstrip())
    ]
    newline = "\r\n" if "\r\n" in text else "\n"

    replacement = [
        indentation + IMPORT_LINE + newline,
        newline,
        indentation + RETURN_LINE + newline,
    ]

    lines[start:end] = replacement
    result = "".join(lines)

    if not hook_present(result):
        raise RepairError(
            "生成的新接入代码未通过结构检查"
        )

    return result


def compile_source(source, filename):
    try:
        compile(source, filename, "exec")
    except SyntaxError as exc:
        raise RepairError(
            f"{filename} 语法检查失败：{exc}"
        ) from exc


def create_backup(app_bytes, module_bytes):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    directory = BACKUP_ROOT / (
        f"{stamp}-{os.getpid()}"
    )
    directory.mkdir(parents=True, exist_ok=False)

    (directory / "app.py").write_bytes(app_bytes)

    module_existed = module_bytes is not None
    if module_existed:
        (
            directory / "local_scheduler_control.py"
        ).write_bytes(module_bytes)

    write_json(
        directory / "manifest.json",
        {
            "module_existed": module_existed,
            "created_at": time.strftime(
                "%Y-%m-%dT%H:%M:%S%z"
            ),
        },
    )

    return directory


def restore_backup(directory):
    manifest = read_json(directory / "manifest.json")
    app_backup = directory / "app.py"

    if not app_backup.is_file():
        raise RepairError(
            "回滚备份缺少 app.py"
        )

    atomic_write(
        APP_FILE,
        app_backup.read_bytes(),
    )

    module_backup = (
        directory / "local_scheduler_control.py"
    )

    if manifest.get("module_existed"):
        if not module_backup.is_file():
            raise RepairError(
                "回滚备份缺少控制模块"
            )

        atomic_write(
            MODULE_FILE,
            module_backup.read_bytes(),
        )
    else:
        try:
            MODULE_FILE.unlink()
        except FileNotFoundError:
            pass


def status():
    app_text = APP_FILE.read_text(encoding="utf-8")
    payload = PAYLOAD_FILE.read_bytes()
    module_matches = (
        MODULE_FILE.is_file()
        and MODULE_FILE.read_bytes() == payload
    )

    return {
        "ok": hook_present(app_text) and module_matches,
        "hook": hook_present(app_text),
        "module": module_matches,
        "container_running": docker_running(),
        "blocked": BLOCK_FILE.exists(),
    }


def repair(force=False):
    process_manager_compatible()

    app_bytes = APP_FILE.read_bytes()
    app_text = app_bytes.decode("utf-8")
    payload_bytes = PAYLOAD_FILE.read_bytes()
    current_module = (
        MODULE_FILE.read_bytes()
        if MODULE_FILE.exists()
        else None
    )

    new_app_text = patched_app_text(app_text)
    new_app_bytes = new_app_text.encode("utf-8")

    app_changed = new_app_bytes != app_bytes
    module_changed = current_module != payload_bytes

    if not app_changed and not module_changed:
        try:
            BLOCK_FILE.unlink()
        except FileNotFoundError:
            pass

        print(
            json.dumps(
                {
                    "ok": True,
                    "changed": False,
                    "status": "healthy",
                },
                ensure_ascii=False,
            )
        )
        return 0

    source_hash = sha256(app_bytes)
    blocked = read_json(BLOCK_FILE)

    if (
        not force
        and blocked.get("sha256") == source_hash
    ):
        print(
            json.dumps(
                {
                    "ok": False,
                    "changed": False,
                    "status": "blocked",
                    "reason": blocked.get(
                        "reason",
                        "此前自动修复验证失败",
                    ),
                },
                ensure_ascii=False,
            )
        )
        return 0

    if (
        app_changed
        and not force
        and time.time() - APP_FILE.stat().st_mtime
        < STABLE_SECONDS
    ):
        print(
            json.dumps(
                {
                    "ok": True,
                    "changed": False,
                    "status": "waiting_for_stable_file",
                },
                ensure_ascii=False,
            )
        )
        return 0

    compile_source(
        new_app_text,
        str(APP_FILE),
    )
    compile_source(
        payload_bytes.decode("utf-8"),
        str(MODULE_FILE),
    )

    was_running = docker_running()
    backup = create_backup(
        app_bytes,
        current_module,
    )

    try:
        if app_changed:
            atomic_write(
                APP_FILE,
                new_app_bytes,
            )

        if module_changed:
            atomic_write(
                MODULE_FILE,
                payload_bytes,
            )

        if was_running:
            restart_container()
            payload = wait_for_interface()
        else:
            payload = {
                "deferred": True,
                "reason": "container_stopped",
            }

    except Exception as exc:
        restore_backup(backup)

        if was_running:
            try:
                restart_container()
            except Exception:
                pass

        STATE_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )
        write_json(
            BLOCK_FILE,
            {
                "sha256": source_hash,
                "reason": str(exc),
                "failed_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%S%z"
                ),
                "backup": str(backup),
            },
        )

        raise RepairError(
            "自动修复验证失败，已恢复更新后的原文件；"
            f"本版本已阻止重复重试：{exc}"
        ) from exc

    try:
        BLOCK_FILE.unlink()
    except FileNotFoundError:
        pass

    print(
        json.dumps(
            {
                "ok": True,
                "changed": True,
                "status": "repaired",
                "app_changed": app_changed,
                "module_changed": module_changed,
                "container_restarted": was_running,
                "backup": str(backup),
                "interface": payload,
            },
            ensure_ascii=False,
        )
    )
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
    )
    parser.add_argument(
        "--force",
        action="store_true",
    )
    parser.add_argument(
        "--clear-block",
        action="store_true",
    )
    args = parser.parse_args()

    if args.clear_block:
        try:
            BLOCK_FILE.unlink()
        except FileNotFoundError:
            pass
        print("已清除自动修复阻止状态")
        return 0

    required = [
        APP_FILE,
        PROCESS_MANAGER,
        PAYLOAD_FILE,
    ]
    missing = [
        str(path)
        for path in required
        if not path.is_file()
    ]

    if missing:
        raise RepairError(
            "缺少必要文件：" + ", ".join(missing)
        )

    if args.check:
        print(
            json.dumps(
                status(),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    return repair(force=args.force)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RepairError, RuntimeError, ValueError) as exc:
        print(
            f"ALAS 调度器自动修复失败：{exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)
