import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from revision.pdf_publisher import PdfPair, PdfPublisher
from revision.reopen_validator import StableReopenValidator
from revision.stable_external_writer import StableExternalActionWriter


def _result(identity, delay=0.0, status="OK"):
    time.sleep(delay)
    return SimpleNamespace(identity=identity, status=status)


class _Tracker:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def enter(self):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)

    def leave(self):
        with self.lock:
            self.active -= 1


class WriterPoolTests(unittest.TestCase):
    def _writer(self, delays, workers=3):
        writer = object.__new__(StableExternalActionWriter)
        writer.max_workers = workers
        tracker = _Tracker()

        def run_plan(plan):
            tracker.enter()
            try:
                return _result(plan["identity"], delays.get(plan["identity"], 0.0))
            finally:
                tracker.leave()

        writer.run_plan = run_plan
        return writer, tracker

    def test_run_many_preserves_plan_order_with_jittered_finish_times(self):
        plans = [{"identity": name} for name in ("a", "b", "c", "d", "e")]
        writer, _ = self._writer({"a": 0.15, "b": 0.0, "c": 0.1, "d": 0.02, "e": 0.05})
        results = writer.run_many(plans, workers=3)
        self.assertEqual([item.identity for item in results], ["a", "b", "c", "d", "e"])

    def test_run_many_never_exceeds_worker_count(self):
        plans = [{"identity": str(index)} for index in range(9)]
        writer, tracker = self._writer({str(index): 0.03 for index in range(9)}, workers=3)
        writer.run_many(plans, workers=3)
        self.assertLessEqual(tracker.peak, 3)
        self.assertGreaterEqual(tracker.peak, 2)

    def test_run_many_single_worker_runs_serially(self):
        plans = [{"identity": str(index)} for index in range(4)]
        writer, tracker = self._writer({str(index): 0.01 for index in range(4)}, workers=1)
        writer.run_many(plans, workers=1)
        self.assertEqual(tracker.peak, 1)

    def test_run_many_cancel_keeps_completed_results_in_order(self):
        plans = [{"identity": str(index)} for index in range(8)]
        writer, _ = self._writer({str(index): 0.02 for index in range(8)}, workers=2)
        seen = []

        def should_cancel():
            return len(seen) >= 2

        results = writer.run_many(
            plans,
            workers=2,
            on_result=lambda result: seen.append(result.identity),
            should_cancel=should_cancel,
        )
        self.assertTrue(0 < len(results) < len(plans))
        self.assertEqual([item.identity for item in results], [str(index) for index in range(len(results))])
        self.assertEqual(sorted(seen, key=int), [item.identity for item in results])


class ValidatorPoolTests(unittest.TestCase):
    def test_validate_many_preserves_pair_order(self):
        validator = object.__new__(StableReopenValidator)
        delays = {"a": 0.12, "b": 0.0, "c": 0.06, "d": 0.02}

        def validate_pair(pair):
            return _result(pair["identity"], delays[pair["identity"]])

        validator.validate_pair = validate_pair
        pairs = [{"identity": name} for name in ("a", "b", "c", "d")]
        results = validator.validate_many(pairs, workers=3)
        self.assertEqual([item.identity for item in results], ["a", "b", "c", "d"])


class _StubPublisher:
    """Publisher double: reuse the real pool logic without Core Console."""

    def __init__(self, delays=None, failures=(), previous_ok=False, signature="sig"):
        self.max_workers = 4
        self.config_signature = signature
        self.previous_signature = signature
        self.previous_ok = previous_ok
        self.previous = {}
        self.delays = delays or {}
        self.failures = set(failures)
        self.calls = []

    def _publish_pair(self, tx):
        identity = tx["identity"]
        self.calls.append(identity)
        time.sleep(self.delays.get(identity, 0.0))
        status = (
            "PDF_PUBLISH_FAILED_ROLLED_BACK"
            if identity in self.failures
            else "PDF_PAIR_PUBLISHED"
        )
        return PdfPair("tx-" + identity, identity, tx["drawing_type"], status)


def _transactions(count):
    return [
        {"identity": "id" + str(index), "drawing_type": "GA", "files": []}
        for index in range(count)
    ]


class PdfPoolTests(unittest.TestCase):
    def test_publish_many_preserves_order_and_reports_every_pair(self):
        stub = _StubPublisher({"id0": 0.12, "id1": 0.0, "id2": 0.06, "id3": 0.02}, previous_ok=True)
        results = PdfPublisher.publish_many(stub, _transactions(4), workers=3)
        self.assertEqual(
            [item.identity for item in results], ["id0", "id1", "id2", "id3"]
        )
        self.assertTrue(all(item.status == "PDF_PAIR_PUBLISHED" for item in results))

    def test_publish_many_continues_after_a_single_pair_failure(self):
        stub = _StubPublisher({}, failures={"id1"}, previous_ok=True)
        results = PdfPublisher.publish_many(stub, _transactions(3), workers=3)
        self.assertEqual(len(results), 3)
        self.assertEqual(results[1].status, "PDF_PUBLISH_FAILED_ROLLED_BACK")
        self.assertEqual(results[0].status, "PDF_PAIR_PUBLISHED")
        self.assertEqual(results[2].status, "PDF_PAIR_PUBLISHED")

    def test_publish_many_preflight_stops_batch_when_plotter_config_is_new(self):
        stub = _StubPublisher({}, failures={"id0"}, previous_ok=False)
        results = PdfPublisher.publish_many(
            stub, _transactions(3), workers=3
        )
        self.assertEqual([item.identity for item in results], ["id0"])
        self.assertIn("BATCH_STOPPED_BY_PDF_PREFLIGHT", results[0].errors)
        self.assertEqual(stub.calls, ["id0"])

    def test_publish_many_cancel_stops_launching_new_pairs(self):
        stub = _StubPublisher({str_name: 0.03 for str_name in ("id0", "id1", "id2", "id3", "id4", "id5")}, previous_ok=True)
        results = PdfPublisher.publish_many(
            stub,
            _transactions(6),
            workers=2,
            should_cancel=lambda: len(stub.calls) >= 2,
        )
        self.assertTrue(0 < len(results) < 6)
        indices = [int(item.identity[2:]) for item in results]
        self.assertEqual(indices, sorted(indices))


class PdfSignatureTests(unittest.TestCase):
    def _publisher(self, reports, settings):
        publisher = object.__new__(PdfPublisher)
        publisher.reports = Path(reports)
        publisher.settings = settings
        return publisher

    def test_signature_changes_when_media_or_plotter_changes(self):
        base = {"plotter_candidates": ["DWG To PDF.pc3"], "preferred_media_names": ["ISO A1 (841.00 x 594.00 MM)"], "plot_style": "monochrome.ctb", "plot_rotation": 0, "minimum_pdf_bytes": 4096}
        changed = dict(base)
        changed["preferred_media_names"] = ["ISO A1 (594.00 x 841.00 MM)"]
        first = PdfPublisher._signature(self._publisher(".", base))
        second = PdfPublisher._signature(self._publisher(".", changed))
        self.assertNotEqual(first, second)

    def test_load_previous_indexes_published_and_unchanged_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "schema": "GNI-PDF-Publisher-0.4C.6",
                "config_signature": "abc",
                "results": [
                    {
                        "identity": "ID-1",
                        "status": "PDF_PAIR_PUBLISHED",
                        "outputs": [
                            {"revision": "A", "status": "PDF_PUBLISHED", "pdf_sha256": "1"},
                            {"revision": "00", "status": "PDF_UNCHANGED", "pdf_sha256": "2"},
                        ],
                    }
                ],
            }
            Path(tmp, "pdf_publish_report.json").write_text(json.dumps(payload), encoding="utf-8")
            previous, signature, published = PdfPublisher._load_previous(self._publisher(tmp, {}))
            self.assertEqual(signature, "abc")
            self.assertTrue(published)
            self.assertEqual(sorted(previous), [("ID-1", "00"), ("ID-1", "A")])

    def test_load_previous_is_empty_without_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous, signature, published = PdfPublisher._load_previous(self._publisher(tmp, {}))
            self.assertEqual((previous, signature, published), ({}, "", False))


if __name__ == "__main__":
    unittest.main()
