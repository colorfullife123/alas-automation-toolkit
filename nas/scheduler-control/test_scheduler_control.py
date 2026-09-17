import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import install_alas_scheduler_control as repair


SAMPLE_APP = """\
def app():
    app = asgi_app(routes=[])
    return app
"""


class SchedulerRepairTests(unittest.TestCase):
    def test_patches_exact_return_and_is_idempotent(self):
        patched = repair.patched_app_text(SAMPLE_APP)
        self.assertIn(repair.IMPORT_LINE, patched)
        self.assertIn(repair.RETURN_LINE, patched)
        self.assertEqual(repair.patched_app_text(patched), patched)
        compile(patched, "app.py", "exec")

    def test_refuses_ambiguous_structure(self):
        ambiguous = SAMPLE_APP + "\n" + SAMPLE_APP.replace("app():", "other():")
        with self.assertRaises(repair.RepairError):
            repair.patched_app_text(ambiguous)

    def test_process_manager_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = Path(directory) / "process_manager.py"
            manager.write_text(
                "class ProcessManager:\n"
                "    def start(self): pass\n"
                "    def stop(self): pass\n",
                encoding="utf-8",
            )
            with patch.object(repair, "PROCESS_MANAGER", manager):
                repair.process_manager_compatible()

            manager.write_text(
                "class ProcessManager:\n"
                "    def start(self): pass\n",
                encoding="utf-8",
            )
            with (
                patch.object(repair, "PROCESS_MANAGER", manager),
                self.assertRaises(repair.RepairError),
            ):
                repair.process_manager_compatible()


if __name__ == "__main__":
    unittest.main()
