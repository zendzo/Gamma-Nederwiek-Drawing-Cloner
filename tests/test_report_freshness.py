import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import app as legacy


def _main(bundle_root):
    """A Main instance with only the widgets `_stage_complete` touches."""
    instance = legacy.Main.__new__(legacy.Main)
    instance._skipped = set()
    instance.files = []
    instance.dst = type("Dst", (), {"text": lambda self: str(bundle_root.parent)})()
    instance.bundle = type("Bundle", (), {"text": lambda self: bundle_root.name})()
    return instance


class StageCompleteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bundle = self.root / "BUNDLE"
        self.reports = self.bundle / "_GNI_REPORTS"
        self.reports.mkdir(parents=True)
        self.main = _main(self.bundle)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, payload):
        (self.reports / name).write_text(json.dumps(payload), encoding="utf-8")

    def test_discovery_requires_both_json_and_csv(self):
        self._write("title_block_discovery_report.json", {"complete": True})
        self.assertFalse(self.main._stage_complete("discovery"))
        (self.reports / "title_block_discovery_fields.csv").write_text("a\n", encoding="utf-8")
        self.assertTrue(self.main._stage_complete("discovery"))

    def test_cancelled_discovery_report_is_not_complete(self):
        # A cancelled sweep still writes a report; advancing on it ran the plan
        # stage against partial discovery data.
        self._write("title_block_discovery_report.json", {"complete": False})
        (self.reports / "title_block_discovery_fields.csv").write_text("a\n", encoding="utf-8")
        self.assertFalse(self.main._stage_complete("discovery"))
        self.assertEqual(self.main._report_state("discovery"), "incomplete")

    def test_cancelled_plan_report_is_not_complete(self):
        self._write("revision_pilot_write_plan.json", {"complete": False})
        self.assertFalse(self.main._stage_complete("plan"))
        self._write("revision_pilot_write_plan.json", {"complete": True})
        self.assertTrue(self.main._stage_complete("plan"))

    def test_legacy_reports_without_flag_still_count_as_complete(self):
        # Upgrading must not force a full re-run of previously finished stages.
        self._write("revision_pilot_write_plan.json", {"plans": []})
        self.assertTrue(self.main._stage_complete("plan"))

    def test_malformed_report_is_not_complete(self):
        (self.reports / "revision_pilot_write_plan.json").write_text("{not json", encoding="utf-8")
        self.assertFalse(self.main._stage_complete("plan"))

    def test_other_stages_only_need_their_report(self):
        for key, name in legacy.STAGE_REPORTS.items():
            if key in ("discovery", "plan"):
                continue
            with self.subTest(stage=key):
                self.assertFalse(self.main._stage_complete(key))
                self._write(name, {})
                self.assertTrue(self.main._stage_complete(key))


if __name__ == "__main__":
    unittest.main()
