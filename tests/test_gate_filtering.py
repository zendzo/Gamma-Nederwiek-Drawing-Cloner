import json
import tempfile
import unittest
from pathlib import Path

from revision.atomic_publish_dry_run import AtomicPublisherDryRun
from revision.reopen_validator import ReopenValidationError, StableReopenValidator
from revision.stable_external_writer import StableExternalActionWriter, StableWriterError


def _outputs(status):
    return [
        {"output_revision": mode, "status": status, "sha256": mode + "-hash"}
        for mode in ("A", "00")
    ]


class WriterGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.primary = self.root / "ID-1_02.dwg"
        self.primary.write_bytes(b"DWG")
        self.plan = self.root / "plan.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write_plans(self, *statuses):
        plans = []
        for index, status in enumerate(statuses):
            plans.append({
                "identity": "ID-%d" % index,
                "drawing_type": "GA",
                "status": status,
                "primary_geometry": str(self.primary),
                "revision_a_donor": "",
                "actions": [
                    {"output_revision": mode, "handle": "H" + mode, "action": "WRITE"}
                    for mode in ("A", "00")
                ],
            })
        self.plan.write_text(json.dumps({"plans": plans}), encoding="utf-8")

    def test_one_blocked_plan_no_longer_blocks_the_batch(self):
        self._write_plans("READY", "BLOCKED", "READY_WITH_WARNINGS")
        ready, rejected = StableExternalActionWriter.load_ready_plans(self.plan, return_rejected=True)
        self.assertEqual([item["identity"] for item in ready], ["ID-0", "ID-2"])
        self.assertEqual([item["identity"] for item in rejected], ["ID-1"])
        # P3: the reason names the status and omits defects a BLOCKED plan
        # cannot satisfy anyway (it has no actions by design).
        self.assertEqual(rejected[0]["errors"], ["PLAN_NOT_READY:BLOCKED"])

    def test_all_blocked_still_raises_with_details(self):
        self._write_plans("BLOCKED", "BLOCKED")
        with self.assertRaises(StableWriterError) as caught:
            StableExternalActionWriter.load_ready_plans(self.plan, return_rejected=True)
        self.assertIn("ID-0=PLAN_NOT_READY", str(caught.exception))

    def test_default_call_still_returns_plans_only(self):
        self._write_plans("READY", "BLOCKED")
        plans = StableExternalActionWriter.load_ready_plans(self.plan)
        self.assertIsInstance(plans, list)
        self.assertEqual(len(plans), 1)


class ValidatorGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.report = Path(self.tmp.name) / "writer.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, *pairs):
        self.report.write_text(json.dumps({"results": list(pairs)}), encoding="utf-8")

    def test_failed_writer_pair_does_not_block_validated_ones(self):
        self._write(
            {"identity": "ID-0", "status": "LOCAL_PAIR_WRITTEN_NOT_VALIDATED", "outputs": _outputs("OK")},
            {"identity": "ID-1", "status": "FAILED", "outputs": _outputs("FAILED")},
            {"identity": "ID-2", "status": "LOCAL_PAIR_WRITTEN_NOT_VALIDATED", "outputs": _outputs("OK")},
        )
        ready, rejected = StableReopenValidator.load_writer_results(self.report, return_rejected=True)
        self.assertEqual([item["identity"] for item in ready], ["ID-0", "ID-2"])
        self.assertEqual([item["identity"] for item in rejected], ["ID-1"])

    def test_incomplete_pair_outputs_are_rejected_not_raised(self):
        self._write(
            {"identity": "ID-0", "status": "LOCAL_PAIR_WRITTEN_NOT_VALIDATED", "outputs": _outputs("OK")},
            {"identity": "ID-1", "status": "LOCAL_PAIR_WRITTEN_NOT_VALIDATED", "outputs": _outputs("OK")[:1]},
        )
        ready, rejected = StableReopenValidator.load_writer_results(self.report, return_rejected=True)
        self.assertEqual(len(ready), 1)
        self.assertIn("PAIR_OUTPUTS_INCOMPLETE", rejected[0]["errors"])

    def test_no_validatable_pairs_raises(self):
        self._write({"identity": "ID-0", "status": "FAILED", "outputs": []})
        with self.assertRaises(ReopenValidationError):
            StableReopenValidator.load_writer_results(self.report)


class DryRunGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bundle = self.root / "BUNDLE"
        self.reports = self.bundle / "_GNI_REPORTS"
        self.reports.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, writer_results, validation_results):
        (self.reports / "stable_external_writer_report.json").write_text(
            json.dumps({"results": writer_results}), encoding="utf-8")
        (self.reports / "reopen_validation_report.json").write_text(
            json.dumps({"results": validation_results}), encoding="utf-8")

    def test_unvalidated_pair_does_not_block_the_batch(self):
        self._write(
            [
                {"identity": "ID-0", "status": "LOCAL_PAIR_WRITTEN_NOT_VALIDATED", "outputs": _outputs("OK")},
                {"identity": "ID-1", "status": "LOCAL_PAIR_WRITTEN_NOT_VALIDATED", "outputs": _outputs("OK")},
            ],
            [
                {"identity": "ID-0", "status": "VALIDATED", "drawing_type": "GA", "outputs": _outputs("VALIDATED")},
                {"identity": "ID-1", "status": "FAILED", "drawing_type": "GA", "outputs": _outputs("FAILED")},
            ],
        )
        ready, rejected = AtomicPublisherDryRun.load_validated_pairs(self.bundle, return_rejected=True)
        self.assertEqual([item["identity"] for item in ready], ["ID-0"])
        self.assertEqual([item["identity"] for item in rejected], ["ID-1"])


if __name__ == "__main__":
    unittest.main()
