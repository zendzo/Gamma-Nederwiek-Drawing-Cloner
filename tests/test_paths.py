"""Portable path resolution: source checkout vs frozen bundle vs read-only folder.

The frozen case is simulated by patching ``sys.frozen``/``sys._MEIPASS``, so these
tests run anywhere and never need a real PyInstaller build.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from config.settings import Settings
from runtime import paths

REPO_ROOT = Path(__file__).resolve().parents[1]


class _IsolatedRoots(unittest.TestCase):
    """Give every test a private data root and a cold path cache."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._local = self.tmp / "localappdata"
        self._local.mkdir()
        self._env = mock.patch.dict(
            os.environ,
            {"GNI_DATA_DIR": str(self.tmp / "portal"), "LOCALAPPDATA": str(self._local)},
        )
        self._env.start()
        paths.reset_cache()

    def tearDown(self):
        paths.reset_cache()
        self._env.stop()
        self._tmp.cleanup()


class SourceRootTests(_IsolatedRoots):
    def test_app_root_is_repository_root_in_a_checkout(self):
        self.assertEqual(paths.app_root(), REPO_ROOT)

    def test_data_root_honours_the_override(self):
        self.assertEqual(paths.data_root(), (self.tmp / "portal").resolve())

    def test_settings_path_sits_under_the_data_root(self):
        expected = (self.tmp / "portal" / "config" / "settings.json").resolve()
        self.assertEqual(paths.settings_path(), expected)

    def test_version_reads_the_version_file(self):
        expected = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(paths.version(), expected)
        self.assertNotEqual(paths.DEFAULT_VERSION, "", "a fallback label must exist")

    def test_fallback_version_does_not_drift_from_the_version_file(self):
        # DEFAULT_VERSION is only used when VERSION is missing from a bundle,
        # but a stale value would print the wrong build in that case.
        expected = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(paths.DEFAULT_VERSION, expected)


class FrozenRootTests(_IsolatedRoots):
    def setUp(self):
        super().setUp()
        self.bundle = self.tmp / "GammaNederwiek"
        self.internal = self.bundle / "_internal"
        self.internal.mkdir(parents=True)
        self.exe = self.bundle / "GammaNederwiek.exe"
        self.exe.write_bytes(b"")
        self._frozen = mock.patch.object(sys, "frozen", True, create=True)
        self._meipass = mock.patch.object(sys, "_MEIPASS", str(self.internal), create=True)
        self._executable = mock.patch.object(sys, "executable", str(self.exe))
        self._frozen.start()
        self._meipass.start()
        self._executable.start()
        # The portable folder only wins when no explicit override is set.
        os.environ.pop("GNI_DATA_DIR", None)
        paths.reset_cache()

    def tearDown(self):
        self._frozen.stop()
        self._meipass.stop()
        self._executable.stop()
        paths.reset_cache()
        super().tearDown()

    def test_app_root_is_the_bundle_runtime_folder(self):
        self.assertEqual(paths.app_root(), self.internal.resolve())

    def test_data_root_is_the_portable_folder_next_to_the_exe(self):
        self.assertEqual(paths.data_root(), self.bundle.resolve())

    def test_frozen_settings_stay_outside_the_runtime_folder(self):
        # A onefile build extracts `_internal` to %TEMP% and deletes it on exit,
        # so settings written there would vanish; the exe folder must win.
        self.assertNotEqual(paths.settings_path().parent.parent, self.internal)

    def test_version_falls_back_to_the_code_default_without_a_version_file(self):
        self.assertEqual(paths.version(), paths.DEFAULT_VERSION)


class ReadOnlyFolderTests(_IsolatedRoots):
    def setUp(self):
        super().setUp()
        self._env.stop()  # exercise the real fallback chain, not the override
        os.environ["LOCALAPPDATA"] = str(self._local)
        paths.reset_cache()

    def test_data_root_falls_back_to_localappdata_when_the_app_folder_is_read_only(self):
        blocker = self.tmp / "not-a-folder"
        blocker.write_text("x", encoding="utf-8")
        with mock.patch.object(paths, "app_root", return_value=blocker):
            self.assertEqual(
                paths.data_root(), (self._local / paths.APP_DIR_NAME).resolve()
            )

    def test_settings_save_does_not_raise_on_a_read_only_target(self):
        blocker = self.tmp / "not-a-folder"
        blocker.write_text("x", encoding="utf-8")
        settings = Settings(destination_bundle_name="BUNDLE")
        written = settings.save(blocker / "config" / "settings.json")
        self.assertEqual(written, paths.fallback_settings_path())
        self.assertEqual(
            json.loads(written.read_text(encoding="utf-8"))["destination_bundle_name"],
            "BUNDLE",
        )

    def test_settings_save_round_trips(self):
        target = self.tmp / "writable" / "config" / "settings.json"
        Settings(source_folder="S", destination_bundle_name="B").save(target)
        self.assertEqual(Settings.load(target).source_folder, "S")


class CoreConsoleTests(unittest.TestCase):
    def test_detection_never_returns_a_bogus_path(self):
        from runtime.coreconsole import detect

        found = detect()
        if found:
            self.assertTrue(
                Path(found).is_file(), f"detect() returned a missing file: {found}"
            )
            self.assertEqual(Path(found).name.lower(), "accoreconsole.exe")


if __name__ == "__main__":
    unittest.main()
