"""Guards the de-monkey-patching of the planner.

`revision/adaptive_detection.py` and `revision/recovery_00.py` used to install
their detectors by replacing class methods at import time. They are now called
explicitly. These tests pin the resulting plan so a future refactor cannot
silently change revision detection.
"""

import json
import tempfile
import unittest
from pathlib import Path

from config.settings import Settings
from models import DrawingGroup, SourceFile
from revision.completion_plan import TitleBlockCompletionPlanBuilder
from revision import adaptive_detection
from revision import recovery_00
from revision.write_plan import RevisionWritePlanBuilder

FIELDS = [
    "drawing_identity", "drawing_type", "source_path", "drawing", "entity_type",
    "handle", "layer", "tag", "value", "insertion_point", "owner_handle",
    "owner_type", "block_name", "suggested_field", "confidence",
    "confidence_score", "evidence", "proposed_a", "proposed_00", "warning",
]


def _row(handle, value, x, y, tag=""):
    return {
        "drawing_identity": "2013J310015-GA-001", "drawing_type": "GA",
        "source_path": "src.dwg", "drawing": "Model", "entity_type": "ATTRIB",
        "handle": handle, "layer": "TITLE", "tag": tag, "value": value,
        "insertion_point": "%s,%s" % (x, y), "owner_handle": "OWN",
        "owner_type": "INSERT", "block_name": "TITLEBLOCK",
        "suggested_field": "", "confidence": "HIGH", "confidence_score": 98,
        "evidence": "", "proposed_a": "", "proposed_00": "", "warning": "",
    }


def _rows():
    """A revision table (header + A + 00 rows) plus a bottom-right current rev."""
    rows = [
        _row("H1", "REVISION", 10.0, 100.0),
        _row("H2", "REV DATE", 20.0, 100.0),
        _row("H3", "PURPOSE OF ISSUE", 30.0, 100.0),
        _row("H4", "DRAWN BY", 40.0, 100.0),
        _row("A1", "A", 10.0, 95.0),
        _row("A2", "07.03.2024", 20.0, 95.0),
        _row("A3", "ISSUED FOR COMMENTS", 30.0, 95.0),
        _row("A4", "ABC", 40.0, 95.0),
        _row("B1", "00", 10.0, 90.0),
        _row("B2", "14.03.2024", 20.0, 90.0),
        _row("B3", "APPROVED FOR CONSTRUCTION", 30.0, 90.0),
        _row("B4", "XYZ", 40.0, 90.0),
        # bottom-right current revision label/value
        _row("C1", "REVISION", 200.0, 20.0),
        _row("C2", "00", 202.0, 18.0),
    ]
    return rows


def _group():
    source = SourceFile(
        path="C:/src/2013J310015-GA-001_02.dwg", relative_path="2013J310015-GA-001_02.dwg",
        filename="2013J310015-GA-001_02.dwg", extension="DWG", size_bytes=1,
        modified_iso="2024-01-01T00:00:00+00:00", role="DWG", drawing_type="GA",
        identity="2013J310015-GA-001", revision="02",
    )
    return DrawingGroup(
        identity="2013J310015-GA-001", drawing_type="GA", candidates=[source],
        selected=True, primary_source=source.path,
    )


class PlannerBehaviourTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.csv = Path(cls.tmp.name) / "discovery.csv"
        import csv
        with cls.csv.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            for row in _rows():
                writer.writerow(row)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_spatial_rows_detected_without_monkey_patching(self):
        rows = _rows()
        spatial, warnings = RevisionWritePlanBuilder._detect_spatial_rows(
            "2013J310015-GA-001", "GA", rows
        )
        kinds = sorted(row.row_kind for row in spatial)
        self.assertEqual(kinds, ["00", "A"])
        self.assertTrue(all(row.confidence == "HIGH" for row in spatial), warnings)

    def test_current_revision_resolves_bottom_right_value(self):
        value, label = TitleBlockCompletionPlanBuilder._find_current_revision(_rows())
        self.assertIsNotNone(value)
        self.assertEqual(value["handle"], "C2")
        self.assertEqual(label["handle"], "C1")

    def test_detector_output_is_stable_for_identical_input(self):
        first, _ = adaptive_detection.detect_detailed_rows(
            RevisionWritePlanBuilder, "ID", "GA", _rows())
        second, _ = recovery_00.detect_with_recovery(
            RevisionWritePlanBuilder, "ID", "GA", _rows())
        by_revision = {row["revision"]: row for row in first}
        self.assertEqual(sorted(by_revision), sorted(r["revision"] for r in second))
        for row in second:
            original = by_revision[row["revision"]]
            self.assertEqual(row["row_y"], original["row_y"])
            self.assertEqual(row["confidence"], original["confidence"])

    def test_import_has_no_side_effects_on_planner_classes(self):
        # Importing the detector patches must not replace planner methods any more.
        self.assertNotIn("_gni_2f1_wrapped", vars(TitleBlockCompletionPlanBuilder.build))
        self.assertIs(
            RevisionWritePlanBuilder._detect_spatial_rows.__func__,
            TitleBlockCompletionPlanBuilder._detect_spatial_rows.__func__,
        )

    def test_full_completion_plan_is_produced(self):
        builder = TitleBlockCompletionPlanBuilder(Settings(), migration_date="2024-03-14")
        plans = builder.build([_group()], self.csv)
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        # Order matches the pre-0.4C.7 cleanup detector ([00, A], not sorted by y).
        self.assertEqual([row.row_kind for row in plan.spatial_rows], ["00", "A"])
        self.assertEqual([row.confidence for row in plan.spatial_rows], ["HIGH", "HIGH"])
        # The 00 revision date is forced to the migration date (recovery_00).
        self.assertEqual(plan.revision_00_date, "2024-03-14")
        targets = {(action.output_revision, action.target_field) for action in plan.actions}
        self.assertIn(("00", "REVISION_PURPOSE"), targets)
        self.assertIn(("A", "REVISION_PURPOSE"), targets)
        self.assertEqual(plan.status, "READY_WITH_WARNINGS")


if __name__ == "__main__":
    unittest.main()
