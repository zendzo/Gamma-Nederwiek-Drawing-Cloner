import json
import tempfile
import unittest
from pathlib import Path

from runtime.background_controller import PipelineWorker


def _plan(identity="ID-1", new_value="APPROVED FOR CONSTRUCTION"):
    return {
        "identity": identity,
        "drawing_type": "GA",
        "primary_geometry": "C:/src/ID-1_02.dwg",
        "actions": [
            {
                "output_revision": "00",
                "handle": "A1",
                "action": "WRITE",
                "old_value": "ISSUED FOR COMMENTS",
                "new_value": new_value,
                "target_field": "REVISION_PURPOSE",
                "reason": "SPATIAL",
            },
            {
                "output_revision": "A",
                "handle": "A2",
                "action": "MOVE_ONLY",
                "old_value": "A",
                "new_value": "A",
                "target_field": "RETAINED_A_ROW_LAYER_MOVE",
                "reason": "MOVE_RETAINED_A_ROW_TO_NEDERWIEK_DATA",
            },
        ],
    }


class PlanHashTests(unittest.TestCase):
    def test_plan_hash_is_stable_for_identical_plans(self):
        self.assertEqual(
            PipelineWorker._plan_sha(_plan()), PipelineWorker._plan_sha(_plan())
        )

    def test_plan_hash_ignores_action_ordering(self):
        plan = _plan()
        reversed_plan = dict(plan, actions=list(reversed(plan["actions"])))
        self.assertEqual(
            PipelineWorker._plan_sha(plan), PipelineWorker._plan_sha(reversed_plan)
        )

    def test_plan_hash_changes_when_a_value_changes(self):
        self.assertNotEqual(
            PipelineWorker._plan_sha(_plan()),
            PipelineWorker._plan_sha(_plan(new_value="ISSUED FOR CONSTRUCTION")),
        )

    def test_plan_hash_changes_when_an_action_is_added(self):
        plan = _plan()
        extra = dict(plan, actions=plan["actions"] + [
            {
                "output_revision": "00",
                "handle": "A3",
                "action": "WRITE",
                "old_value": "X",
                "new_value": "",
                "target_field": "REVISION_ROW_CLEAR",
                "reason": "CLEAR_NON_OUTPUT_REVISION_ROW",
            }
        ])
        self.assertNotEqual(PipelineWorker._plan_sha(plan), PipelineWorker._plan_sha(extra))


class CheckpointReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "ID-1_02.dwg"
        self.source.write_bytes(b"SOURCE")
        self.working = self.root / "ID-1_00.dwg"
        self.working.write_bytes(b"OUTPUT")

    def tearDown(self):
        self.tmp.cleanup()

    def _worker(self):
        return PipelineWorker("writer", {})

    def _record(self, plan_sha):
        worker = self._worker()
        return {
            "identity": "ID-1",
            "source_sha256": worker._sha(self.source),
            "plan_sha256": plan_sha,
            "outputs": [
                {"output_revision": mode, "working_dwg": str(self.working),
                 "sha256": worker._sha(self.working)}
                for mode in ("A", "00")
            ],
        }

    def test_plan_change_invalidates_reusable_checkpoint(self):
        # Same source file, same working DWG, different plan -> must re-run.
        # This is the stale-output bug: an edited plan used to reuse the old DWG.
        worker = self._worker()
        plan = _plan()
        plan["primary_geometry"] = str(self.source)
        old_sha = PipelineWorker._plan_sha(plan)
        edited = dict(plan, actions=[dict(a, new_value="CHANGED") for a in plan["actions"]])
        record = self._record(old_sha)
        self.assertTrue(
            worker._writer_reusable(record, plan, None, PipelineWorker._plan_sha(plan))
        )
        self.assertFalse(
            worker._writer_reusable(record, edited, None, PipelineWorker._plan_sha(edited))
        )

    def test_checkpoint_without_plan_hash_is_never_reused(self):
        worker = self._worker()
        plan = _plan()
        plan["primary_geometry"] = str(self.source)
        record = self._record(None)
        record.pop("plan_sha256")
        self.assertFalse(
            worker._writer_reusable(record, plan, None, PipelineWorker._plan_sha(plan))
        )

    def test_missing_working_dwg_invalidates_checkpoint(self):
        worker = self._worker()
        plan = _plan()
        plan["primary_geometry"] = str(self.source)
        sha = PipelineWorker._plan_sha(plan)
        record = self._record(sha)
        self.working.unlink()
        self.assertFalse(worker._writer_reusable(record, plan, None, sha))


if __name__ == "__main__":
    unittest.main()
