import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import alas_supervisor as supervisor


class FakeDocker:
    def __init__(self, _container):
        self.is_running = True
        self.policy = {"Name": "unless-stopped", "MaximumRetryCount": 0}
        self.start_count = 0
        self.stop_count = 0
        self.started_at = "2026-09-17T11:00:00Z"
        self.scheduler_running = True
        self.scheduler_actions = []

    def ensure_exists(self):
        return None

    def running(self):
        return self.is_running

    def named_container_running(self, _name):
        return True

    def restart_policy(self):
        return dict(self.policy)

    def set_restart_policy(self, policy):
        self.policy = dict(policy)

    def stop(self):
        self.stop_count += 1
        self.is_running = False

    def start(self):
        self.start_count += 1
        self.is_running = True

    def container_started_at(self):
        return self.started_at if self.is_running else ""

    def scheduler_request(self, action):
        self.scheduler_actions.append(action)
        if action == "start":
            was_running = self.scheduler_running
            self.scheduler_running = True
            result = "already_running" if was_running else "started"
        elif action == "stop":
            was_running = self.scheduler_running
            self.scheduler_running = False
            result = "stopped" if was_running else "already_stopped"
        else:
            result = "running" if self.scheduler_running else "stopped"
        return {
            "ok": True,
            "running": self.scheduler_running,
            "action": result,
        }


class SupervisorTests(unittest.TestCase):
    def make_config(self, root: Path) -> supervisor.RuntimeConfig:
        alas_root = root / "alas"
        (alas_root / "config").mkdir(parents=True)
        (alas_root / "config" / "alas.json").write_text(
            json.dumps(
                {
                    "Alas": {
                        "Emulator": {
                            "ServerName": "disabled",
                            "PackageName": "com.bilibili.azurlane",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        return supervisor.RuntimeConfig(
            container="alas",
            alas_root=alas_root,
            state_dir=root / "state",
            adb_server_host="127.0.0.1",
            adb_server_port=5037,
            adb_serial="192.0.2.10:16555",
            server_name="中途岛",
            mqtt_config_file=root / "alas-monitor.conf",
            mqtt_recovery_topic="nas/alas/recovery",
        )

    def test_parses_split_html_and_cross_midnight(self):
        ordinary = supervisor.parse_event(
            {
                "id": 18328,
                "title": "2026维护公告（2026年9月8日10:00）",
                "modifyTime": "2026-09-07 11:00:00",
                "content": (
                    "<p>司令部将于<span>9月8日</span><span>10:00~1</span>"
                    "<span>6</span><span>:00</span>对以下港区进行改造</p>"
                ),
            }
        )
        self.assertEqual(ordinary.start.hour, 10)
        self.assertEqual(ordinary.end.hour, 16)

        overnight = supervisor.parse_event(
            {
                "id": 1,
                "title": "2026维护公告（2026年12月31日23:00）",
                "modifyTime": "2026-12-30 11:00:00",
                "content": "司令部将于12月31日23:00至1月1日02:00进行改造",
            }
        )
        self.assertEqual(overnight.end.year, 2027)
        self.assertEqual(overnight.end.day, 1)

    def test_loads_local_environment_without_executing_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "supervisor.env"
            env_file.write_text(
                'ALAS_ROOT="/srv/ALAS data"\n'
                "ALAS_SERVER_NAME=\n"
                "IGNORED_VALUE='$(touch should-not-run)'\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                supervisor.load_local_environment(env_file)
                self.assertEqual(os.environ["ALAS_ROOT"], "/srv/ALAS data")
                self.assertEqual(os.environ["ALAS_SERVER_NAME"], "")
                self.assertNotIn("IGNORED_VALUE", os.environ)

    def test_explicit_server_overrides_disabled_alas_value(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            target = supervisor.resolve_server_target(config)
            self.assertEqual(target.server_name, "中途岛")
            self.assertEqual(target.source, "explicit")
            self.assertEqual(target.config_value, "disabled")

    def test_migrates_both_legacy_states(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            old_adb = root / "old-adb.json"
            old_guard = root / "old-guard.json"
            old_adb.write_text(
                json.dumps(
                    {
                        "status": "offline",
                        "offline_since": 123.0,
                        "confirmations": 0,
                    }
                ),
                encoding="utf-8",
            )
            event = {
                "news_id": 9,
                "title": "测试维护公告",
                "start": "2026-09-17T10:00:00+08:00",
                "end": "2026-09-17T16:00:00+08:00",
                "url": "https://example.invalid/9",
            }
            old_guard.write_text(
                json.dumps(
                    {
                        "phase": "holding",
                        "event": event,
                        "restart_policy": {
                            "Name": "unless-stopped",
                            "MaximumRetryCount": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(supervisor, "LEGACY_ADB_STATE", old_adb),
                patch.object(supervisor, "LEGACY_MAINTENANCE_STATE", old_guard),
            ):
                state = supervisor.load_state(config, migrate=True)

            self.assertEqual(state["adb"]["status"], "offline")
            self.assertEqual(state["maintenance"]["phase"], "holding")
            self.assertEqual(state["maintenance"]["event"]["news_id"], 9)
            self.assertTrue(config.state_file.exists())

    def test_adb_recovery_starts_alas_and_publishes_after_three_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            state = supervisor.new_state()
            state["adb"] = {
                "status": "offline",
                "offline_since": time.time() - 90,
                "confirmations": 0,
            }
            supervisor.save_state(config, state)

            with patch.object(supervisor, "Docker", FakeDocker):
                daemon = supervisor.SupervisorDaemon(config)
            daemon.docker.is_running = False

            with (
                patch.object(supervisor, "adb_target_online", return_value=True),
                patch.object(supervisor, "publish_mqtt") as publish,
            ):
                daemon.monitor_adb()
                self.assertEqual(daemon.state["adb"]["status"], "recovering")
                self.assertFalse(daemon.docker.running())
                daemon.monitor_adb()
                self.assertEqual(daemon.state["adb"]["confirmations"], 2)
                daemon.monitor_adb()

            self.assertEqual(daemon.state["adb"]["status"], "online")
            self.assertEqual(daemon.docker.start_count, 1)
            self.assertTrue(daemon.docker.scheduler_running)
            publish.assert_called_once()

    def test_maintenance_wins_and_history_survives_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            with patch.object(supervisor, "Docker", FakeDocker):
                daemon = supervisor.SupervisorDaemon(config)

            start = datetime(2026, 9, 17, 10, 0, tzinfo=supervisor.CHINA_TZ)
            event = supervisor.MaintenanceEvent(
                news_id=99,
                title="2026维护公告（2026年9月17日10:00）",
                start=start,
                end=start + timedelta(hours=5),
                url="https://example.invalid/99",
            )
            daemon.cached_events = [event]
            daemon.last_schedule_refresh = time.monotonic()

            with patch.object(supervisor, "now_china", return_value=event.stop_at):
                daemon.step()
            self.assertEqual(daemon.state["maintenance"]["phase"], "holding")
            self.assertTrue(daemon.docker.running())
            self.assertFalse(daemon.docker.scheduler_running)
            self.assertEqual(daemon.docker.policy["Name"], "unless-stopped")

            reading = supervisor.ServerReading(
                server_name="中途岛",
                state=0,
                last_update=1,
                age_seconds=1,
            )
            after = event.end + timedelta(minutes=1)
            with (
                patch.object(supervisor, "fetch_server_reading", return_value=reading),
                patch.object(supervisor, "adb_target_online", return_value=True),
            ):
                daemon.last_server_poll = 0
                daemon.step_holding(after)
                self.assertEqual(daemon.state["maintenance"]["phase"], "holding")
                daemon.last_server_poll = 0
                daemon.step_holding(after)

            maintenance = daemon.state["maintenance"]
            self.assertEqual(maintenance["phase"], "idle")
            self.assertEqual(maintenance["last_event"]["news_id"], 99)
            self.assertIn("last_completed_at", maintenance)
            self.assertTrue(daemon.docker.running())
            self.assertTrue(daemon.docker.scheduler_running)
            self.assertEqual(daemon.docker.policy["Name"], "unless-stopped")

    def test_manual_scheduler_stop_survives_until_container_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            state = supervisor.new_state()
            state["adb"] = {
                "status": "online",
                "offline_since": None,
                "confirmations": 0,
                "container_started_at": "same-start",
            }
            supervisor.save_state(config, state)

            with patch.object(supervisor, "Docker", FakeDocker):
                daemon = supervisor.SupervisorDaemon(config)
            daemon.docker.started_at = "same-start"
            daemon.docker.scheduler_running = False

            with patch.object(supervisor, "adb_target_online", return_value=True):
                daemon.monitor_adb()
                self.assertFalse(daemon.docker.scheduler_running)
                self.assertNotIn("start", daemon.docker.scheduler_actions)

                daemon.docker.started_at = "new-start"
                daemon.monitor_adb()
            self.assertTrue(daemon.docker.scheduler_running)
            self.assertIn("start", daemon.docker.scheduler_actions)

    def test_schedule_failure_does_not_block_adb_monitoring(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            with patch.object(supervisor, "Docker", FakeDocker):
                daemon = supervisor.SupervisorDaemon(config)
            with (
                patch.object(
                    daemon,
                    "update_schedule",
                    side_effect=supervisor.SupervisorError("network down"),
                ),
                patch.object(daemon, "monitor_adb") as monitor,
            ):
                daemon.step()
            monitor.assert_called_once()


if __name__ == "__main__":
    unittest.main()
