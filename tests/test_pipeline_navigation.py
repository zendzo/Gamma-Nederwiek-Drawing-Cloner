"""Guards the primary-button / pipeline-row contract.

The button label is what the user drives the whole migration with, so it must
always point at the same stage `next_stage()` returns — including the moment a
stage finishes and the rows are re-rendered.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import app as legacy


class _FakeBox:
    """Stand-in for ``QMessageBox(...)`` used in widget style.

    ``_gate_blocked`` builds a box explicitly (``QMessageBox(...)`` +
    ``show()``) instead of calling the static convenience method, so patching
    only ``QMessageBox.information`` is not enough to keep a test headless.
    """

    Warning = error = information = Question = 0
    Ok = 1

    def __init__(self, *args, **kwargs):
        self.args = args

    def setObjectName(self, *args, **kwargs):
        return None

    def show(self):
        return None

    def raise_(self):
        return None

    def exec(self):
        return 1


def _silence_message_boxes():
    """Replace both dialog styles; returns state for _restore_message_boxes."""
    saved = {name: getattr(legacy.QMessageBox, name)
             for name in ("information", "warning", "critical", "exec", "question")}
    legacy.QMessageBox.information = staticmethod(lambda *a, **k: legacy.QMessageBox.Ok)
    legacy.QMessageBox.warning = staticmethod(lambda *a, **k: legacy.QMessageBox.Ok)
    legacy.QMessageBox.critical = staticmethod(lambda *a, **k: legacy.QMessageBox.Ok)
    legacy.QMessageBox.exec = lambda self, *a, **k: 1
    return saved


def _restore_message_boxes(saved):
    for name, value in saved.items():
        setattr(legacy.QMessageBox, name, value)


class _DialogPatchMixin:
    """Auto-silences every dialog so a failing assertion cannot hang the suite."""

    def setUp(self):
        self._saved_dialogs = _silence_message_boxes()

    def tearDown(self):
        # unittest runs subclass tearDown() first, then this one (MRO), so the
        # dialogs stay silenced until the widget and temp dir are gone.
        _restore_message_boxes(self._saved_dialogs)


class PipelineNavigationTests(_DialogPatchMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bundle = self.root / "BUNDLE"
        self.reports = self.bundle / "_GNI_REPORTS"
        self.reports.mkdir(parents=True)
        self.main = legacy.Main()
        self.main.src.setText(str(self.root))
        self.main.dst.setText(str(self.root))
        self.main.bundle.setText("BUNDLE")

    def tearDown(self):
        self.main.close()
        self.main.deleteLater()
        self.tmp.cleanup()

    def _write(self, key, payload=None):
        name = legacy.STAGE_REPORTS[key]
        (self.reports / name).write_text(
            json.dumps({"complete": True} if payload is None else payload),
            encoding="utf-8",
        )

    def _write_all(self, skip=()):
        self.main.files = [object()]
        for key in legacy.STAGE_REPORTS:
            if key not in skip:
                self._write(key)
        (self.reports / "title_block_discovery_fields.csv").write_text("a\n", encoding="utf-8")

    def test_empty_state_offers_scan(self):
        self.assertEqual(self.main.next_stage(), ("scan", "Scan source"))
        self.assertIn("Scan source", self.main.primary.text())

    def test_label_matches_next_stage_for_every_gap(self):
        # Every combination of "this stage and everything after it is missing"
        # must leave the button pointing at exactly that stage.
        keys = [key for key, _ in legacy.PIPELINE if key != "scan"]
        for index in range(len(keys)):
            with self.subTest(gap=keys[index]):
                for name in legacy.STAGE_REPORTS.values():
                    (self.reports / name).unlink(missing_ok=True)
                self._write_all(skip=keys[index:])
                self.main.refresh_pipeline()
                nxt = self.main.next_stage()
                self.assertIsNotNone(nxt, "pipeline must never be complete before pdf")
                self.assertEqual(nxt[0], keys[index])
                label = self.main.primary.text()
                if keys[index] == "publish":
                    self.assertTrue(label.startswith("Publish "))
                elif keys[index] == "pdf":
                    self.assertEqual(label, "Publish PDFs")
                else:
                    self.assertIn(legacy.PIPELINE[index + 1][1], label)

    def test_complete_pipeline_offers_reports_not_a_dead_end(self):
        self._write_all()
        self.main.refresh_pipeline()
        self.assertIsNone(self.main.next_stage())
        self.assertEqual(self.main.primary.text(), "Open Reports")

    def test_clicking_when_complete_switches_to_reports(self):
        self._write_all()
        self.main.refresh_pipeline()
        started = []
        self.main.run_stage = lambda key: started.append(key)
        # Patch both dialog styles: the 3-arg ``QMessageBox.information(...)``
        # convenience call and the widget-style ``QMessageBox(...)`` chain. An
        # unpatched modal here would block the whole suite forever.
        shown = _silence_message_boxes()
        try:
            self.main.run_next_stage()
        finally:
            _restore_message_boxes(shown)
        self.assertEqual(started, [])
        self.assertEqual(
            self.main.centralWidget().tabText(self.main.centralWidget().currentIndex()),
            "Reports",
        )

    def test_cancelled_report_reads_as_incomplete(self):
        self._write_all()
        self._write("pdf", {"complete": False, "cancelled": True})
        self.main.refresh_pipeline()
        self.assertEqual(self.main._report_state("pdf"), "incomplete")
        self.assertEqual(self.main.next_stage()[0], "pdf")

    def test_quarantined_report_makes_stage_pending_again(self):
        self._write_all()
        report = self.reports / legacy.STAGE_REPORTS["writer"]
        report.replace(Path(str(report) + ".stale"))
        self.main.refresh_pipeline()
        self.assertEqual(self.main.next_stage()[0], "writer")

    def test_advance_pipeline_label_is_current_after_refresh(self):
        # The regression: the "ready to publish" dialog reported a label computed
        # before the completed stage was marked DONE, so it said "all complete".
        self._write_all(skip=("publish", "pdf"))
        self.main.refresh_pipeline()
        self.main._chain = True
        seen = {}
        legacy.QMessageBox.information = staticmethod(
            lambda parent, title, text: seen.setdefault("text", text))
        self.main._advance_pipeline("dryrun")
        self.assertIn("Next action: Publish 0 validated pair(s)", seen.get("text", ""))
        self.assertNotIn("All stages complete", seen.get("text", ""))


class StageReportRowTests(_DialogPatchMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.reports = self.root / "BUNDLE" / "_GNI_REPORTS"
        self.reports.mkdir(parents=True)
        self.main = legacy.Main()
        self.main.dst.setText(str(self.root))
        self.main.bundle.setText("BUNDLE")

    def tearDown(self):
        self.main.close()
        self.main.deleteLater()
        self.tmp.cleanup()

    def test_pipeline_rows_are_selectable_so_select_row_works(self):
        # NoSelection made refresh_pipeline's selectRow() a silent no-op.
        self.assertNotEqual(
            self.main.stage_tbl.selectionMode(),
            legacy.QAbstractItemView.NoSelection,
        )

    def test_double_clicking_a_row_is_wired(self):
        self.main.open_stage_report = lambda row, column=None: setattr(self, "opened", row)
        self.main.stage_tbl.cellDoubleClicked.emit(3, 0)
        self.assertEqual(getattr(self, "opened", None), 3)

    def test_scan_row_opens_reports_instead_of_a_file(self):
        switched = []
        self.main.show_reports_tab = lambda: switched.append(True)
        self.main.open_stage_report(0)
        self.assertEqual(switched, [True])

    def test_missing_report_says_so_instead_of_raising(self):
        shown = []
        legacy.QMessageBox.information = staticmethod(
            lambda parent, title, text: shown.append(text))
        self.main.open_stage_report(5)
        self.assertEqual(len(shown), 1)
        self.assertIn("No report yet", shown[0])

    def test_stale_report_is_still_reachable(self):
        report = self.reports / legacy.STAGE_REPORTS["writer"]
        report.write_text("{}", encoding="utf-8")
        Path(str(report) + ".stale").write_text("{}", encoding="utf-8")
        report.unlink()
        opened = []
        self.main._open_path = lambda path: opened.append(Path(path))
        self.main.open_stage_report(5)
        self.assertEqual(opened, [Path(str(report) + ".stale")])


if __name__ == "__main__":
    unittest.main()
