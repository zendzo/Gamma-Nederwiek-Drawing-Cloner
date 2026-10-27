"""Checkpoint and report contracts of the discovery/plan QThread workers.

These workers are the only production code that had no test at all, even
though they own the resume records the whole pipeline trusts:

- ``title_block_discovery_checkpoint.json`` (discovery resume)
- ``_GNI_REPORTS/plans/index.json`` + ``plans/*.json`` (plan resume)
- the ``complete``/``cancelled`` stamps that ``_report_state()`` reads

The threads themselves are not started here (that needs Core Console); the
static/class helpers they call are tested directly, plus the signal contract
``app.py`` connects to.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from revision.write_plan import PlanAction, RevisionPlan
from runtime.production_workers import DiscoveryWorker, ProductionPlanWorker


def _action(identity):
    return PlanAction(
        identity=identity,
        drawing_type="GA",
        output_revision="A",
        source_role="primary",
        source_path="C:/x.dwg",
        handle="1A",
        entity_type="TEXT",
        layer="NEDERWIEK_DATA",
        block_name="",
        tag="REV",
        old_value="0",
        target_field="Revision",
        new_value="A",
        confidence="HIGH",
        confidence_score=1.0,
        evidence="test",
        action="WRITE",
        reason="TEST",
    )


def _plan(identity, status="READY", actions=1, warnings=(), errors=()):
    return RevisionPlan(
        identity=identity,
        drawing_type="GA",
        status=status,
        primary_geometry="C:/x.dwg",
        revision_a_donor="",
        migration_date="2026-10-01",
        revision_a_date="2026-09-24",
        revision_00_date="2026-10-02",
        actions=[_action(identity) for _ in range(actions)],
        warnings=list(warnings),
        errors=list(errors),
    )


class DiscoveryCheckpointTests(unittest.TestCase):
    def test_checkpoint_records_progress_and_cancel_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            DiscoveryWorker._write_checkpoint(bundle, [], 12, False)
            path = bundle / "_GNI_REPORTS" / "title_block_discovery_checkpoint.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["total_drawings"], 12)
            self.assertEqual(payload["completed_drawings"], 0)
            self.assertIs(payload["cancelled"], False)
            self.assertEqual(payload["results"], [])
            self.assertIn("updated_at", payload)

    def test_checkpoint_is_written_atomically(self):
        # A torn checkpoint would make the next run resume from garbage.
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            DiscoveryWorker._write_checkpoint(bundle, [], 3, False)
            leftovers = [p.name for p in (bundle / "_GNI_REPORTS").iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [])

    def test_second_write_replaces_the_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            DiscoveryWorker._write_checkpoint(bundle, [], 5, False)
            DiscoveryWorker._write_checkpoint(bundle, [], 5, True)
            payload = json.loads(
                (bundle / "_GNI_REPORTS" / "title_block_discovery_checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertIs(payload["cancelled"], True)


class PlanIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bundle = Path(self.tmp.name) / "BUNDLE"
        self.root = self.bundle / "_GNI_REPORTS" / "plans"
        self.root.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _index(self):
        return json.loads((self.root / "index.json").read_text(encoding="utf-8"))

    def test_index_counts_ready_and_blocked(self):
        plans = [_plan("A"), _plan("B", status="BLOCKED", actions=0), _plan("C", status="READY_WITH_WARNINGS")]
        ProductionPlanWorker._write_index(self.root, plans, 3, False)
        payload = self._index()
        self.assertEqual(payload["total_drawings"], 3)
        self.assertEqual(payload["completed_drawings"], 3)
        self.assertEqual(payload["ready"], 2)
        self.assertEqual(payload["blocked"], 1)
        self.assertIs(payload["complete"], True)
        self.assertIs(payload["cancelled"], False)

    def test_cancelled_index_is_not_complete(self):
        ProductionPlanWorker._write_index(self.root, [_plan("A")], 10, True)
        payload = self._index()
        self.assertIs(payload["cancelled"], True)
        self.assertIs(payload["complete"], False)
        self.assertEqual((payload["completed_drawings"], payload["total_drawings"]), (1, 10))

    def test_index_keeps_per_plan_counts_and_messages(self):
        plans = [_plan("A", warnings=["W1", "W2"], actions=4)]
        ProductionPlanWorker._write_index(self.root, plans, 1, False)
        entry = self._index()["plans"][0]
        self.assertEqual(entry["actions"], 4)
        self.assertEqual(entry["warnings"], ["W1", "W2"])

    def test_blocked_plans_reach_the_manual_check_report(self):
        # write_manual_check_report is called from here (it used to be a
        # monkey-patch), so a BLOCKED plan must leave a trace on disk.
        ProductionPlanWorker._write_index(self.root, [_plan("BLK", status="BLOCKED", actions=0)], 1, False)
        produced = sorted(p.name for p in self.bundle.rglob("manual_check_required*"))
        self.assertTrue(produced, "no manual_check_required report written for a BLOCKED plan")

    def test_fragment_is_named_safely_and_replaces_atomically(self):
        ProductionPlanWorker._write_fragment(self.root, _plan("3. GA/ID:1"))
        names = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(names, ["3._GA_ID_1.json"])
        ProductionPlanWorker._write_fragment(self.root, _plan("3. GA/ID:1", actions=7))
        payload = json.loads((self.root / "3._GA_ID_1.json").read_text(encoding="utf-8"))
        self.assertEqual(len(payload["actions"]), 7)

    def test_safe_name_keeps_common_identity_characters(self):
        self.assertEqual(ProductionPlanWorker._safe_name("I301-J30019_A"), "I301-J30019_A")
        self.assertEqual(ProductionPlanWorker._safe_name("a/b\\c:d"), "a_b_c_d")


class PlanReportStampTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bundle = Path(self.tmp.name) / "BUNDLE"
        self.report = self.bundle / "_GNI_REPORTS" / "revision_pilot_write_plan.json"
        self.report.parent.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _stamp(self, **kwargs):
        ProductionPlanWorker._mark_plan_report_complete(self.bundle, **kwargs)
        return json.loads(self.report.read_text(encoding="utf-8"))

    def test_stamp_adds_complete_and_keeps_content(self):
        self.report.write_text(json.dumps({"plans": [{"identity": "A"}]}), encoding="utf-8")
        payload = self._stamp()
        self.assertIs(payload["complete"], True)
        self.assertIs(payload["cancelled"], False)
        self.assertEqual(payload["plans"], [{"identity": "A"}])

    def test_cancelled_stamp_marks_incomplete(self):
        self.report.write_text(json.dumps({"plans": []}), encoding="utf-8")
        payload = self._stamp(complete=False, cancelled=True)
        self.assertIs(payload["complete"], False)
        self.assertIs(payload["cancelled"], True)

    def test_missing_report_is_ignored(self):
        ProductionPlanWorker._mark_plan_report_complete(self.bundle)
        self.assertFalse(self.report.exists())

    def test_corrupt_report_is_left_alone(self):
        self.report.write_text("{not json", encoding="utf-8")
        ProductionPlanWorker._mark_plan_report_complete(self.bundle)
        self.assertEqual(self.report.read_text(encoding="utf-8"), "{not json")

    def test_stamp_is_what_makes_the_pipeline_stop(self):
        """Integration: the worker's stamp is the flag `_report_state()` reads.

        Without it a cancelled plan sweep looked complete and the writer stage
        ran on half a bundle. This is the contract between the worker and the UI.
        """
        import app as legacy

        QApplication.instance() or QApplication([])
        main = legacy.Main()
        try:
            main.dst.setText(str(self.bundle.parent))
            main.bundle.setText(self.bundle.name)
            self.report.write_text(json.dumps({"plans": []}), encoding="utf-8")
            self.assertEqual(main._report_state("plan"), "complete")
            ProductionPlanWorker._mark_plan_report_complete(self.bundle, complete=False, cancelled=True)
            self.assertEqual(main._report_state("plan"), "incomplete")
        finally:
            main.close()
            main.deleteLater()


class WorkerSignalContractTests(unittest.TestCase):
    """`app.py` connects to these signals positionally, so their arity matters."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _signature(cls, name):
        instance = cls.__new__(cls)
        text = str(getattr(cls, name).__get__(instance))
        inside = text[text.find("(") + 1:text.rfind(")")]
        return [part.strip() for part in inside.split(",") if part.strip()]

    def test_completed_signal_ends_with_cancelled(self):
        # Both workers emit `cancelled` last; app.py unpacks it positionally.
        for cls in (DiscoveryWorker, ProductionPlanWorker):
            with self.subTest(worker=cls.__name__):
                args = self._signature(cls, "completed")
                self.assertTrue(args[-1].startswith("bool"), args)
                self.assertGreaterEqual(len(args), 4)

    def test_progress_signal_is_index_total_identity_stage(self):
        for cls in (DiscoveryWorker, ProductionPlanWorker):
            with self.subTest(worker=cls.__name__):
                self.assertEqual(
                    self._signature(cls, "progress"),
                    ["int", "int", "QString", "QString"],
                )

    def test_failed_signal_carries_a_traceback_string(self):
        for cls in (DiscoveryWorker, ProductionPlanWorker):
            with self.subTest(worker=cls.__name__):
                self.assertEqual(self._signature(cls, "failed"), ["QString"])

    def test_cancel_is_cooperative_and_idempotent(self):
        from config.settings import Settings

        worker = ProductionPlanWorker([], Path("d.csv"), "/tmp", "B", "/tmp", Settings(), "/tmp")
        self.assertFalse(worker._cancel_requested)
        worker.cancel()
        worker.cancel()
        self.assertTrue(worker._cancel_requested)


if __name__ == "__main__":
    unittest.main()
