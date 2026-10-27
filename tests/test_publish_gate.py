import json
import tempfile
import unittest
from pathlib import Path

from revision.atomic_publisher import AtomicPublishError, AtomicPublisher


def _outputs(mode_hash):
    return [
        {"output_revision": mode, "working_dwg": "C:/x.dwg", "sha256": mode_hash + mode}
        for mode in ("A", "00")
    ]


class PublishGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bundle = Path(self.tmp.name) / "BUNDLE"
        self.reports = self.bundle / "_GNI_REPORTS"
        self.reports.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, dry_results, validation, writer):
        (self.reports / "atomic_publish_dry_run_report.json").write_text(
            json.dumps({"mode": "DRY_RUN", "destination_modified": False,
                        "results": dry_results}), encoding="utf-8")
        (self.reports / "reopen_validation_report.json").write_text(
            json.dumps({"results": validation}), encoding="utf-8")
        (self.reports / "stable_external_writer_report.json").write_text(
            json.dumps({"results": writer}), encoding="utf-8")

    def _ready(self, identity, hashes="h"):
        return {
            "dry_run": {"identity": identity, "status": "DRY_RUN_READY_TO_PUBLISH",
                        "drawing_type": "GA", "files": []},
            "validation": {"identity": identity, "status": "VALIDATED",
                           "outputs": _outputs(hashes)},
            "writer": {"identity": identity,
                       "status": "LOCAL_PAIR_WRITTEN_NOT_VALIDATED",
                       "outputs": _outputs(hashes)},
        }

    def _blocked(self, identity):
        return {
            "dry_run": {"identity": identity, "status": "DRY_RUN_BLOCKED",
                        "drawing_type": "GA", "files": []},
            "validation": {"identity": identity, "status": "VALIDATED",
                           "outputs": _outputs("h")},
            "writer": {"identity": identity,
                       "status": "LOCAL_PAIR_WRITTEN_NOT_VALIDATED",
                       "outputs": _outputs("h")},
        }

    def test_one_blocked_package_no_longer_blocks_the_batch(self):
        ready_a = self._ready("ID-0")
        blocked = self._blocked("ID-1")
        ready_b = self._ready("ID-2")
        self._write(
            [ready_a["dry_run"], blocked["dry_run"], ready_b["dry_run"]],
            [ready_a["validation"], blocked["validation"], ready_b["validation"]],
            [ready_a["writer"], blocked["writer"], ready_b["writer"]],
        )
        accepted, rejected = AtomicPublisher.load_ready_transactions(
            self.bundle, return_rejected=True)
        self.assertEqual([item["dry_run"]["identity"] for item in accepted], ["ID-0", "ID-2"])
        self.assertEqual([item["identity"] for item in rejected], ["ID-1"])
        self.assertIn("DRY_RUN_NOT_READY", rejected[0]["errors"])

    def test_blocked_package_is_never_published(self):
        # Filtering must only skip, never downgrade a rejection into a publish.
        ready = self._ready("ID-0")
        blocked = self._blocked("ID-1")
        self._write(
            [ready["dry_run"], blocked["dry_run"]],
            [ready["validation"], blocked["validation"]],
            [ready["writer"], blocked["writer"]],
        )
        accepted, rejected = AtomicPublisher.load_ready_transactions(
            self.bundle, return_rejected=True)
        published = {item["dry_run"]["identity"] for item in accepted}
        self.assertEqual(published, {"ID-0"})
        self.assertNotIn("ID-1", published)

    def test_missing_validation_is_rejected(self):
        ready = self._ready("ID-0")
        self._write([ready["dry_run"]], [], [ready["writer"]])
        with self.assertRaises(AtomicPublishError) as caught:
            AtomicPublisher.load_ready_transactions(self.bundle, return_rejected=True)
        self.assertIn("VALIDATION_NOT_READY", str(caught.exception))

    def test_all_rejected_still_raises_with_details(self):
        blocked = self._blocked("ID-0")
        self._write([blocked["dry_run"]], [blocked["validation"]], [blocked["writer"]])
        with self.assertRaises(AtomicPublishError) as caught:
            AtomicPublisher.load_ready_transactions(self.bundle, return_rejected=True)
        self.assertIn("ID-0=DRY_RUN_NOT_READY", str(caught.exception))

    def test_default_call_returns_list_only(self):
        ready = self._ready("ID-0")
        self._write([ready["dry_run"]], [ready["validation"]], [ready["writer"]])
        packages = AtomicPublisher.load_ready_transactions(self.bundle)
        self.assertIsInstance(packages, list)
        self.assertEqual(len(packages), 1)

    def test_report_that_modified_the_destination_is_refused(self):
        ready = self._ready("ID-0")
        (self.reports / "atomic_publish_dry_run_report.json").write_text(
            json.dumps({"mode": "DRY_RUN", "destination_modified": True, "results": []}),
            encoding="utf-8")
        (self.reports / "reopen_validation_report.json").write_text(
            json.dumps({"results": []}), encoding="utf-8")
        (self.reports / "stable_external_writer_report.json").write_text(
            json.dumps({"results": []}), encoding="utf-8")
        with self.assertRaises(AtomicPublishError) as caught:
            AtomicPublisher.load_ready_transactions(self.bundle)
        self.assertIn("INVALID_DRY_RUN_REPORT", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
