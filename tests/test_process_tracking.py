import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from runtime.process_tracking import ChildRegistry, prune_job_root, run_console


def _sleep_command(seconds=30):
    if os.name == "nt":
        return [sys.executable, "-c", "import time; time.sleep(%s)" % seconds]
    return ["/bin/sh", "-c", "sleep %s" % seconds]


class RunConsoleTests(unittest.TestCase):
    def test_returns_completed_process_like_subprocess_run(self):
        command = [sys.executable, "-c", "print('hello'); raise SystemExit(3)"]
        result = run_console(command, capture_output=True)
        self.assertEqual(result.returncode, 3)
        self.assertIn(b"hello", result.stdout)

    def test_timeout_is_raised_and_child_is_not_left_running(self):
        registry = ChildRegistry()
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            # Patch the module registry so the test does not touch global state.
            import runtime.process_tracking as tracking
            original = tracking.child_registry
            tracking.child_registry = registry
            try:
                run_console(command, capture_output=True, timeout=0.3)
            finally:
                tracking.child_registry = original
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(registry.active, 0)

    def test_registry_tracks_live_children_and_can_terminate_them(self):
        registry = ChildRegistry()
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        process = subprocess.Popen(command)
        registry.register(process)
        self.assertEqual(registry.active, 1)
        registry.begin_shutdown()
        registry.terminate_all(grace=5)
        process.wait(timeout=10)
        self.assertIsNotNone(process.poll())

    def test_register_after_shutdown_terminates_immediately(self):
        registry = ChildRegistry()
        registry.begin_shutdown()
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        registry.register(process)
        process.wait(timeout=10)
        self.assertIsNotNone(process.poll())


class CloseReopensSpawnTests(unittest.TestCase):
    """A GUI close must not leave the registry rejecting every future child.

    ``Main.closeEvent`` calls `begin_shutdown()`; without a matching
    `cancel_shutdown()` the global registry stayed latched and any later
    `run_console` was killed on spawn (returncode -15, empty output). The suite
    only exposed this once a GUI test closed a window before this test ran.
    """

    def test_close_event_unlatches_the_registry(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication, QMessageBox

        saved = {name: getattr(QMessageBox, name)
                 for name in ("information", "warning", "critical", "question", "exec")}
        for name in ("information", "warning", "critical"):
            setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.Ok))
        QMessageBox.exec = lambda self, *a, **k: 1
        try:
            app = QApplication.instance() or QApplication([])
            import app as legacy
            import runtime.process_tracking as tracking

            main = legacy.Main()
            main.close()
            self.assertFalse(tracking.child_registry._shutting_down)
            result = run_console(
                [sys.executable, "-c", "print('ok'); raise SystemExit(3)"],
                capture_output=True,
            )
            self.assertEqual(result.returncode, 3)
            self.assertIn(b"ok", result.stdout)
            main.deleteLater()
        finally:
            for name, value in saved.items():
                setattr(QMessageBox, name, value)


class PruneJobRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "GNI"
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _job(self, name, age_days):
        folder = self.root / name
        folder.mkdir()
        (folder / "file.txt").write_text("x", encoding="utf-8")
        stamp = time.time() - age_days * 86400
        os.utime(folder, (stamp, stamp))
        return folder

    def test_old_jobs_are_removed_and_recent_jobs_kept(self):
        old = self._job("old_job", 30)
        recent = self._job("recent_job", 1)
        removed, failed = prune_job_root(self.root, keep_days=7)
        self.assertEqual((removed, failed), (1, 0))
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_trusted_runtime_is_never_removed(self):
        trusted = self._job("trusted_runtime", 365)
        removed, _ = prune_job_root(self.root, keep_days=7)
        self.assertEqual(removed, 0)
        self.assertTrue(trusted.exists())

    def test_missing_root_is_not_an_error(self):
        self.assertEqual(prune_job_root(self.root / "nope", keep_days=7), (0, 0))


if __name__ == "__main__":
    unittest.main()
