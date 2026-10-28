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
    # Yes, not Ok: a test that forgets to answer "yes" must still be able to
    # drive a confirmation, and every existing test either patches this or is
    # asserting that the button correctly refuses to run.
    legacy.QMessageBox.question = staticmethod(lambda *a, **k: legacy.QMessageBox.Yes)
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


class NewJobTests(_DialogPatchMixin, unittest.TestCase):
    """The New Job button replaces closing and reopening the app.

    It must stay disabled until the PDF stage is complete, never run under a
    live worker, warn when the next job reuses an existing bundle, and clear
    the reports of the job that actually finished.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.reports = self.root / "BUNDLE" / "_GNI_REPORTS"
        self.reports.mkdir(parents=True)
        # app.Main() would otherwise rewrite the real config/settings.json on
        # every new_job() -> save(), so point the module constants at the temp
        # dir for the duration of the test.
        self._saved_paths = (legacy.CFG, legacy.BASE)
        legacy.CFG = self.root / "settings.json"
        legacy.BASE = self.root
        self.main = legacy.Main()
        self.main.src.setText(str(self.root))
        self.main.dst.setText(str(self.root))
        self.main.bundle.setText("BUNDLE")
        # A finished job always leaves its bundle on disk, so the confirm path
        # is the "bundle already exists" warning. Answer Yes by default; tests
        # that need a refusal or that inspect the dialog override these.
        self.dialogs = []
        self._answer_yes()

    def _answer_yes(self):
        for name in ("warning", "question"):
            setattr(legacy.QMessageBox, name, staticmethod(
                lambda parent, title, text, *a, **k: (
                    self.dialogs.append((title, text)), legacy.QMessageBox.Yes)[1]))

    def tearDown(self):
        self.main.close()
        self.main.deleteLater()
        legacy.CFG, legacy.BASE = self._saved_paths
        self.tmp.cleanup()
        super().tearDown()

    def _write(self, key, payload=None):
        name = legacy.STAGE_REPORTS[key]
        (self.reports / name).write_text(
            json.dumps({"complete": True} if payload is None else payload),
            encoding="utf-8",
        )

    def _finish_pipeline(self):
        self.main.files = [object()]
        for key in legacy.STAGE_REPORTS:
            self._write(key)
        (self.reports / "title_block_discovery_fields.csv").write_text("a\n", encoding="utf-8")
        self.main.refresh_pipeline()

    def test_button_sits_under_the_primary_button(self):
        layout = self.main.primary.parentWidget().layout()
        widgets = [layout.itemAt(i).widget() for i in range(layout.count())]
        self.assertIs(widgets[widgets.index(self.main.primary) + 1], self.main.newjobb)

    def test_disabled_on_a_fresh_session(self):
        self.assertFalse(self.main.newjobb.isEnabled())
        self.assertFalse(self.main.job_finished())

    def test_disabled_while_the_pipeline_is_incomplete(self):
        self._finish_pipeline()
        (self.reports / legacy.STAGE_REPORTS["pdf"]).unlink()
        self.main.refresh_pipeline()
        self.assertFalse(self.main.newjobb.isEnabled())

    def test_enabled_only_after_the_pdf_stage(self):
        self._finish_pipeline()
        self.main.refresh_pipeline()
        self.assertTrue(self.main.job_finished())
        self.assertTrue(self.main.newjobb.isEnabled())
        # A run in progress locks it again even with every report present.
        self.main._set_pipeline_busy(True)
        self.assertFalse(self.main.newjobb.isEnabled())
        self.main._set_pipeline_busy(False)
        self.assertTrue(self.main.newjobb.isEnabled())

    def test_cancelled_pdf_report_keeps_the_button_disabled(self):
        self._finish_pipeline()
        self._write("pdf", {"complete": False, "cancelled": True})
        self.main.refresh_pipeline()
        self.assertFalse(self.main.newjobb.isEnabled())

    def test_new_job_deletes_the_reports_and_restarts_at_scan(self):
        self._finish_pipeline()
        self.main.groups = [object()]
        self.main.support = [object()]
        self.main.support_results = [object()]
        self.main.discovery_results = [object()]
        self.main.revision_plans = [object()]
        self.main._skipped.add("clone")
        self.main.stage_tbl.setRowCount(3)
        for name in ("tbl", "stbl", "dtbl", "ptbl"):
            getattr(self.main, name).setRowCount(4)
        self.assertTrue(self.main.new_job())
        # Reports are the pipeline state: they must be gone from disk too, or
        # next_stage() keeps pointing at a finished job and the primary button
        # stays on "Open Reports".
        self.assertEqual(sorted(p.name for p in self.reports.iterdir()), [])
        self.assertEqual(self.main.next_stage(), ("scan", "Scan source"))
        self.assertIn("Run migration", self.main.primary.text())
        self.assertIn("Scan source", self.main.primary.text())
        self.assertEqual(self.main.reports_root(), self.reports)
        for name in ("files", "groups", "support", "support_results",
                     "discovery_results", "revision_plans"):
            self.assertEqual(getattr(self.main, name), [], name)
        self.assertEqual(self.main._skipped, set())
        for name in ("tbl", "stbl", "dtbl", "ptbl", "rtbl"):
            self.assertEqual(getattr(self.main, name).rowCount(), 0, name)
        self.assertFalse(self.main.newjobb.isEnabled())

    def test_new_job_works_after_reopening_the_app(self):
        # The reported bug: a restored session had every report but no scan in
        # memory, so job_finished() was False and the button did nothing.
        self._finish_pipeline()
        self.main.files = []
        self.main.refresh_pipeline()
        self.assertTrue(self.main.job_finished())
        self.assertTrue(self.main.newjobb.isEnabled())
        self.assertTrue(self.main.new_job())
        self.assertIn("Scan source", self.main.primary.text())

    def test_new_job_does_not_touch_published_outputs(self):
        self._finish_pipeline()
        published = self.root / "BUNDLE" / "A" / "1. SP" / "CAD" / "DWG.dwg"
        published.parent.mkdir(parents=True, exist_ok=True)
        published.write_bytes(b"DWG")
        self.assertTrue(self.main.new_job())
        self.assertTrue(published.is_file())

    def test_declining_the_confirmation_keeps_reports_and_results(self):
        self._finish_pipeline()
        self.main.groups = [object()]
        legacy.QMessageBox.warning = staticmethod(
            lambda *a, **k: legacy.QMessageBox.No)
        self.assertFalse(self.main.new_job())
        self.assertEqual(len(self.main.groups), 1)
        self.assertTrue((self.reports / legacy.STAGE_REPORTS["pdf"]).is_file())

    def test_no_op_while_busy(self):
        self._finish_pipeline()
        self.main.groups = [object()]
        self.main._set_pipeline_busy(True)
        self.assertFalse(self.main.new_job())
        self.assertEqual(len(self.main.groups), 1)
        self.assertTrue((self.reports / legacy.STAGE_REPORTS["pdf"]).is_file())

    def test_unfinished_job_is_explained_not_silently_ignored(self):
        self._finish_pipeline()
        (self.reports / legacy.STAGE_REPORTS["pdf"]).unlink()
        self.main.refresh_pipeline()
        shown = []
        legacy.QMessageBox.information = staticmethod(
            lambda parent, title, text: shown.append(text))
        self.assertFalse(self.main.new_job())
        self.assertEqual(len(shown), 1)
        self.assertIn("only available after the PDF stage", shown[0])

    def test_existing_bundle_raises_a_replace_warning(self):
        self._finish_pipeline()
        published = self.root / "BUNDLE" / "A" / "1. SP" / "CAD" / "OLD.dwg"
        published.parent.mkdir(parents=True, exist_ok=True)
        published.write_bytes(b"DWG")
        self.assertTrue(self.main.new_job())
        self.assertEqual(len(self.dialogs), 1, "exactly one confirmation")
        title, text = self.dialogs[0]
        self.assertIn("already exists", title)
        # Must name the bundle, say files are replaced, and point at the backup.
        self.assertIn(str(self.root / "BUNDLE"), text)
        self.assertIn("REPLACED", text)
        self.assertIn("_GNI_BACKUP", text)
        self.assertIn("change Bundle name or Destination root", text)
        # A file was inside it, so the count must be reported too.
        self.assertIn("1 file(s) inside", text)
        # ...and the reset still happened.
        self.assertEqual(sorted(p.name for p in self.reports.iterdir()), [])
        self.assertIn("Scan source", self.main.primary.text())

    def test_empty_bundle_uses_a_plain_confirmation(self):
        self._finish_pipeline()
        # Point at a bundle that does not exist yet: the operator renamed it, so
        # nothing can be replaced and no replace-warning is warranted.
        self.main.bundle.setText("BUNDLE_NEW")
        fresh = self.root / "BUNDLE_NEW" / "_GNI_REPORTS"
        fresh.mkdir(parents=True)
        for name in legacy.STAGE_REPORTS.values():
            (fresh / name).write_text(json.dumps({"complete": True}), encoding="utf-8")
        (fresh / "title_block_discovery_fields.csv").write_text("a\n", encoding="utf-8")
        self.main.refresh_pipeline()
        self.main.bundle.setText("BUNDLE_NEW")
        self.main.job_finished = lambda: True
        self.main._finished_root = str(fresh)
        (self.root / "BUNDLE_NEW").rename(self.root / "TMP_MOVE")
        self.assertIsNone(self.main._existing_bundle())
        self.assertTrue(self.main.new_job())
        title, text = self.dialogs[0]
        self.assertNotIn("already exists", title)
        self.assertNotIn("REPLACED", text)
        self.assertIn("Start a new job", title)

    def test_bundle_warning_counts_files_inside(self):
        published = self.root / "BUNDLE" / "00" / "1. SP" / "CAD" / "a.dwg"
        published.parent.mkdir(parents=True, exist_ok=True)
        published.write_bytes(b"DWG")
        pdf = self.root / "BUNDLE" / "00" / "1. SP" / "PDF" / "a.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"PDF")
        path, count = self.main._existing_bundle()
        self.assertEqual(path, self.root / "BUNDLE")
        self.assertGreaterEqual(count, 2)

    def test_missing_bundle_has_no_warning_target(self):
        self.main.bundle.setText("NOPE")
        self.assertIsNone(self.main._existing_bundle())
        self.main.bundle.setText("")
        self.assertIsNone(self.main._existing_bundle())

    def test_reset_targets_the_finished_bundle_not_the_new_fields(self):
        # Renaming the bundle before pressing New Job must not lose the reset:
        # the reports that have to go are the ones of the job that finished.
        self._finish_pipeline()
        self.main.bundle.setText("NEXT_BUNDLE")
        self.main.refresh_pipeline()
        self.assertTrue(self.main.job_finished())
        self.assertTrue(self.main.new_job())
        self.assertEqual(sorted(p.name for p in self.reports.iterdir()), [])


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
