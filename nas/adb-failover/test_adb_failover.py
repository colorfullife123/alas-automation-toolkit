import tempfile
import unittest
from pathlib import Path

import alas_adb_failover as failover


class AdbFailoverTests(unittest.TestCase):
    def write_config(self, root: Path, primary="mumu-a.example") -> Path:
        path = root / "failover.conf"
        path.write_text(
            "ADB_FIXED_HOST=127.0.0.1\n"
            "ADB_FIXED_PORT=16555\n"
            f"ADB_PRIMARY_HOST={primary}\n"
            "ADB_PRIMARY_PORT=16384\n"
            "ADB_BACKUP_HOST=192.0.2.10\n"
            "ADB_BACKUP_PORT=16416\n"
            "ADB_CONNECT_TIMEOUT_SECONDS=2\n",
            encoding="utf-8",
        )
        return path

    def test_primary_and_backup_roles_are_rendered(self):
        with tempfile.TemporaryDirectory() as directory:
            value = failover.settings(self.write_config(Path(directory)))
            text = failover.render(value)
        self.assertIn("server adb_primary mumu-a.example:16384 check\n", text)
        self.assertIn("server adb_backup 192.0.2.10:16416 check backup\n", text)
        self.assertNotIn("adb_primary mumu-a.example:16384 check backup", text)

    def test_rejects_haproxy_injection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(Path(directory), "host.example check backup")
            with self.assertRaises(failover.ConfigError):
                failover.settings(path)


if __name__ == "__main__":
    unittest.main()
