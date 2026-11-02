"""Regression guards for the discovery checkpoint and plan-stage bottlenecks.

* The discovery checkpoint is rewritten after every batch. It must not re-hash
  every finished drawing each time (that was O(batches x drawings) full reads
  of the source share).
* The plan stage must parse the discovery CSV once per chunk of drawings, not
  once per drawing, and must produce the same plans in the same order.
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from runtime import production_workers as prod
from runtime.production_workers import DiscoveryWorker, ProductionPlanWorker


class SourceShaCacheTests(unittest.TestCase):
    def setUp(self):
        DiscoveryWorker._sha_cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "A.dwg"
        self.path.write_bytes(b"ORIGINAL")

    def test_unchanged_drawing_is_hashed_once(self):
        with mock.patch.object(prod, "sha256_file", wraps=prod.sha256_file) as hasher:
            first = DiscoveryWorker._source_sha(self.path)
            second = DiscoveryWorker._source_sha(self.path)
        self.assertEqual(first, second)
        self.assertEqual(hasher.call_count, 1)

    def test_changed_drawing_is_hashed_again(self):
        first = DiscoveryWorker._source_sha(self.path)
        self.path.write_bytes(b"RE-SAVED-LONGER")
        second = DiscoveryWorker._source_sha(self.path)
        self.assertNotEqual(first, second)

    def test_checkpoint_hash_matches_the_real_file(self):
        import hashlib
        digest = DiscoveryWorker._source_sha(self.path)
        self.assertEqual(digest, hashlib.sha256(b"ORIGINAL").hexdigest())


class PlanChunkingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _run(self, count, chunk):
        groups = [SimpleNamespace(identity=f"G{i}") for i in range(count)]
        calls = []

        class FakePlanner:
            def __init__(self, settings):
                pass

            def build(self, part, csv):
                calls.append([g.identity for g in part])
                return [SimpleNamespace(identity=g.identity, status="READY") for g in part]

            @staticmethod
            def write_reports(root, plans):
                return ["a.json", "a.csv", "b.csv", "c.csv"]

        worker = ProductionPlanWorker(groups, "fields.csv", "dst", "BUNDLE", "src",
                                      SimpleNamespace(), ".")
        worker.PLAN_CHUNK = chunk
        completed = []
        worker.completed.connect(lambda *args: completed.append(args))
        worker.failed.connect(lambda message: self.fail(message))
        with mock.patch("runtime.production_workers.DestinationBuilder") as dest, \
                mock.patch("runtime.production_workers.TitleBlockCompletionPlanBuilder",
                           FakePlanner), \
                mock.patch.object(ProductionPlanWorker, "_write_fragment"), \
                mock.patch.object(ProductionPlanWorker, "_write_index"), \
                mock.patch.object(ProductionPlanWorker, "_mark_plan_report_complete"):
            dest.return_value.build.return_value = None
            dest.return_value.bundle_root = Path(tempfile.gettempdir())
            worker.run()
        return groups, calls, completed

    def test_one_planner_call_per_chunk_not_per_drawing(self):
        groups, calls, _ = self._run(count=40, chunk=16)
        self.assertEqual([len(c) for c in calls], [16, 16, 8])

    def test_chunked_plans_keep_drawing_order_and_completeness(self):
        groups, calls, completed = self._run(count=40, chunk=16)
        planned = [identity for call in calls for identity in call]
        self.assertEqual(planned, [g.identity for g in groups])
        self.assertTrue(completed, "worker never reported completion")
        plans = completed[0][0]
        self.assertEqual([p.identity for p in plans], [g.identity for g in groups])


class AtomicReplaceRetryTests(unittest.TestCase):
    """A brief antivirus/indexer lock on a fresh temp file must not fail a publish."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name) / "a.tmp"
        self.dst = Path(self.tmp.name) / "a.dwg"
        self.src.write_bytes(b"DATA")

    def test_transient_lock_is_retried_then_succeeds(self):
        from runtime import atomic_io
        real = os.replace
        attempts = []

        def flaky(source, destination):
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError(13, "Access is denied")
            return real(source, destination)

        with mock.patch.object(atomic_io.os, "replace", flaky), \
                mock.patch.object(atomic_io.time, "sleep"):
            atomic_io.atomic_replace(self.src, self.dst)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(self.dst.read_bytes(), b"DATA")

    def test_persistent_lock_still_raises(self):
        from runtime import atomic_io

        def locked(source, destination):
            raise PermissionError(13, "Access is denied")

        with mock.patch.object(atomic_io.os, "replace", locked), \
                mock.patch.object(atomic_io.time, "sleep"):
            with self.assertRaises(PermissionError):
                atomic_io.atomic_replace(self.src, self.dst, retries=3)


class PublishSerialTests(unittest.TestCase):
    """Publish transactions share reference rasters, so they must never overlap."""

    def test_publish_many_never_overlaps_transactions(self):
        import threading
        import time
        from revision.atomic_publisher import AtomicPublisher

        publisher = object.__new__(AtomicPublisher)
        state = {"active": 0, "peak": 0}
        lock = threading.Lock()

        def publish_package(package):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.01)
            with lock:
                state["active"] -= 1
            return SimpleNamespace(identity=package["id"])

        publisher.publish_package = publish_package
        packages = [{"id": str(index)} for index in range(6)]
        results = publisher.publish_many(packages, workers=8, batch_size=4)
        self.assertEqual(state["peak"], 1)
        self.assertEqual([item.identity for item in results],
                         [str(index) for index in range(6)])

    def test_publish_many_cancel_stops_between_transactions(self):
        from revision.atomic_publisher import AtomicPublisher

        publisher = object.__new__(AtomicPublisher)
        publisher.publish_package = lambda package: SimpleNamespace(identity=package["id"])
        seen = []
        results = publisher.publish_many(
            [{"id": str(index)} for index in range(5)],
            on_result=seen.append,
            should_cancel=lambda: len(seen) >= 2,
        )
        self.assertEqual(len(results), 2)


if __name__ == "__main__":
    unittest.main()
