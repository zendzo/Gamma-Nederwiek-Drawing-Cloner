"""A gate that rejects every item must leave the pipeline in a truthful state.

Regression cover for the P3-3 finding: the handler returned ``False`` but the
previous handoff report stayed on disk looking current, so the next stage ran on
stale data and every resume re-raised the same error. The stage must also name
the rejected drawings instead of reporting "nothing to process".
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

import app as legacy
from revision.stable_external_writer import StableWriterError


class _DialogPatchMixin:
    def setUp(self):
        self._saved = {}
        for name in ("information", "warning", "critical", "exec", "question"):
            self._saved[name] = getattr(QMessageBox, name)
        for name in ("information", "warning", "critical"):
            setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.Ok))
        QMessageBox.exec = lambda self, *a, **k: 1

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(QMessageBox, name, value)


class GateBlockedTests(_DialogPatchMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bundle = self.root / "BUNDLE"
        self.reports = self.bundle / "_GNI_REPORTS"
        self.reports.mkdir(parents=True)
        self.main = legacy.Main()
        self.main.src.setText(str(self.root))
        self.main.dst.setText(str(self.root))
        self.main.bundle.setText("BUNDLE")
        self.started = []
        self.main._start_bg = lambda operation, payload: self.started.append(operation)

    def tearDown(self):
        self.main.close()
        self.main.deleteLater()
        self.tmp.cleanup()
        super().tearDown()

    def test_all_rejected_writer_blocks_and_quarantines(self):
        plan = self.reports / "revision_pilot_write_plan.json"
        plan.write_text(json.dumps({"plans": [
            {"identity": "ID-1", "status": "BLOCKED"},
            {"identity": "ID-2", "status": "BLOCKED"},
        ]}), encoding="utf-8")
        stale = self.reports / "stable_external_writer_report.json"
        stale.write_text('{"results": []}', encoding="utf-8")

        self.assertFalse(self.main.run_controlled_writer())
        self.assertEqual(self.started, [], "no background job may start on a blocked gate")

        self.assertFalse(stale.is_file(), "the stale report must not stay in place")
        self.assertTrue(Path(str(stale) + ".stale").is_file())
        self.assertFalse(self.main._stage_complete("writer"))
        self.assertNotEqual(self.main.next_stage()[0], "validator")

        gate = json.loads((self.reports / "gate_rejected.json").read_text(encoding="utf-8"))
        self.assertEqual(gate["stage"], "Stable Writer")
        self.assertEqual(gate["count"], 2)
        self.assertEqual([item["identity"] for item in gate["rejected"]], ["ID-1", "ID-2"])
        self.assertEqual(gate["rejected"][0]["errors"], ["PLAN_NOT_READY:BLOCKED"])

    def test_gate_blocked_parses_every_rejected_identity(self):
        # Regression: the parser once split on every ':' before splitting the
        # detail, so `PROBE_REJECTED: A=E1,E2; B=E3` recorded only 'A' and
        # printed "nothing to process" for a fully rejected batch.
        path = self.reports / "probe_report.json"
        path.write_text("{}", encoding="utf-8")
        self.main._gate_blocked(
            "Probe", path, StableWriterError("PROBE_REJECTED: A=E1,E2; B=E3"))
        gate = json.loads((self.reports / "gate_rejected.json").read_text(encoding="utf-8"))
        self.assertEqual(gate["count"], 2)
        self.assertEqual(gate["rejected"][0], {"identity": "A", "errors": ["E1", "E2"]})
        self.assertEqual(gate["rejected"][1], {"identity": "B", "errors": ["E3"]})
        self.assertFalse(path.is_file(), "the stale report is quarantined")

    def test_gate_blocked_keeps_explicit_rejects(self):
        # The partial-accept path passes the list directly; it must win over any
        # parsing of the message text.
        path = self.reports / "probe_report.json"
        path.write_text("{}", encoding="utf-8")
        self.main._gate_blocked(
            "Probe", path, RuntimeError("PROBE_FAILED"),
            [{"identity": "X", "errors": ["WHY"]}])
        gate = json.loads((self.reports / "gate_rejected.json").read_text(encoding="utf-8"))
        self.assertEqual(gate["rejected"], [{"identity": "X", "errors": ["WHY"]}])

    def test_quarantine_creates_no_ghost_report(self):
        # _quarantine_report must tolerate a missing report (first run).
        self.main._gate_blocked("Probe", self.reports / "never_written.json",
                                RuntimeError("PROBE_REJECTED: A=E"))
        self.assertFalse((self.reports / "never_written.json").exists())
        self.assertFalse((self.reports / "never_written.json.stale").exists())


if __name__ == "__main__":
    unittest.main()
