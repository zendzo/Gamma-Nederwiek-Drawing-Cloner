"""A drawing whose revision table has a single 00 row must still plan.

Most SP title blocks carry one revision row (the drawing is at 00, approved for
construction) and no A row. That used to block the whole drawing with
HIGH_CONFIDENCE_SPATIAL_ROW_MISSING:A. Both deliverables are built from the same
highest-revision geometry, so the A copy writes A/issued-for-comments into that
one row and the 00 copy keeps 00/approved-for-construction. The reuse must be
declared, never silent.
"""

import csv
import tempfile
import unittest
from pathlib import Path

from config.settings import Settings
from models import DrawingGroup, SourceFile
from revision import recovery_00
from revision.completion_plan import TitleBlockCompletionPlanBuilder
from revision.stable_external_writer import StableExternalActionWriter
from revision.write_plan import RevisionWritePlanBuilder

FIELDS = [
    "drawing_identity", "drawing_type", "source_path", "drawing", "entity_type",
    "handle", "layer", "tag", "value", "insertion_point", "owner_handle",
    "owner_type", "block_name", "suggested_field", "confidence",
    "confidence_score", "evidence", "proposed_a", "proposed_00", "warning",
]

IDENTITY = "SP-4110-0001-HP-0001"


def _row(handle, value, x, y):
    return {
        "drawing_identity": IDENTITY, "drawing_type": "SP",
        "source_path": "src.dwg", "drawing": "Model", "entity_type": "TEXT",
        "handle": handle, "layer": "TITLE", "tag": "", "value": value,
        "insertion_point": "%s,%s" % (x, y), "owner_handle": "OWN",
        "owner_type": "BLOCK_RECORD", "block_name": "*Model_Space",
        "suggested_field": "", "confidence": "HIGH", "confidence_score": 98,
        "evidence": "", "proposed_a": "", "proposed_00": "", "warning": "",
    }


def _single_row_table():
    """Header, one data row (00), and the bottom-right current-revision pair."""
    return [
        _row("H1", "REVISION", 10.0, 100.0),
        _row("H2", "REV DATE", 20.0, 100.0),
        _row("H3", "PURPOSE OF ISSUE", 30.0, 100.0),
        _row("H4", "DRAWN BY", 40.0, 100.0),
        _row("B1", "00", 10.0, 95.0),
        _row("B2", "15.07.2026", 20.0, 95.0),
        _row("B3", "APPROVED FOR CONSTRUCTION", 30.0, 95.0),
        _row("B4", "DNU", 40.0, 95.0),
        # Bottom-right current-revision label/value, clear of the table box.
        _row("C1", "REVISION", 260.0, 10.0),
        _row("C2", "00", 265.0, 8.0),
    ]


class ReconstructRowATests(unittest.TestCase):
    def _detect(self, rows):
        return recovery_00.detect_with_recovery(
            RevisionWritePlanBuilder, IDENTITY, "SP", rows)

    def test_missing_a_row_is_rebuilt_from_the_00_row(self):
        detected, warnings = self._detect(_single_row_table())
        kinds = {item["revision"]: item for item in detected}
        self.assertIn("A", kinds)
        self.assertIn("00", kinds)
        rebuilt = kinds["A"]
        self.assertEqual(rebuilt["confidence"], "HIGH")
        self.assertIn("RECONSTRUCTED_ROW_A", rebuilt["evidence"])
        self.assertIn("SHARED_HANDLE_WITH_00", rebuilt["evidence"])
        self.assertEqual(rebuilt["row_y"], kinds["00"]["row_y"])
        self.assertNotIn("SPATIAL_ROW_NOT_FOUND:A", warnings)
        self.assertIn("A_ROW_RECONSTRUCTED_FROM_00", warnings)

    def test_rebuild_shares_the_00_cells(self):
        detected, _ = self._detect(_single_row_table())
        by_revision = {item["revision"]: item for item in detected}
        self.assertEqual(by_revision["A"]["cells"]["REVISION"]["handle"], "B1")
        self.assertEqual(by_revision["A"]["cells"]["REVISION_DATE"]["handle"], "B2")

    def test_no_rebuild_when_a_row_already_exists(self):
        rows = _single_row_table() + [
            _row("A1", "A", 10.0, 92.0),
            _row("A2", "07.03.2024", 20.0, 92.0),
            _row("A3", "ISSUED FOR COMMENTS", 30.0, 92.0),
        ]
        detected, warnings = self._detect(rows)
        self.assertEqual(
            sum(1 for item in detected if item["revision"] == "A"), 1)
        self.assertNotIn("A_ROW_RECONSTRUCTED_FROM_00", warnings)

    def test_no_rebuild_without_a_high_confidence_00_row(self):
        recovered, diagnostic = recovery_00._reconstruct_a(IDENTITY, "SP", [])
        self.assertIsNone(recovered)
        self.assertEqual(diagnostic, "A_ROW_RECOVERY_REJECTED:NO_HIGH_CONFIDENCE_00")

    def test_no_rebuild_without_writable_handles(self):
        detected, _ = self._detect(_single_row_table())
        row_00 = next(item for item in detected if item["revision"] == "00")
        row_00["cells"]["REVISION"]["handle"] = ""
        recovered, diagnostic = recovery_00._reconstruct_a(IDENTITY, "SP", [row_00])
        self.assertIsNone(recovered)
        self.assertEqual(diagnostic, "A_ROW_RECOVERY_REJECTED:NO_REVISION_HANDLE")


class SingleRowPlanTests(unittest.TestCase):
    """The real planner + writer gate must accept the rebuilt row."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.source = root / "SP-4110-0001-HP-0001.dwg"
        cls.source.write_bytes(b"DWG")
        cls.csv = root / "discovery.csv"
        with cls.csv.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            for row in _single_row_table():
                writer.writerow(row)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _group(self):
        source = SourceFile(
            path=str(self.source), relative_path=self.source.name,
            filename=self.source.name, extension="DWG", size_bytes=3,
            modified_iso="2026-01-01T00:00:00+00:00", role="DWG",
            drawing_type="SP", identity=IDENTITY, revision="00",
        )
        return DrawingGroup(
            identity=IDENTITY, drawing_type="SP", candidates=[source],
            selected=True, primary_source=source.path,
        )

    def _plan(self):
        builder = TitleBlockCompletionPlanBuilder(
            Settings(), migration_date="2026-11-02")
        return builder.build([self._group()], self.csv)[0]

    def test_plan_is_ready_instead_of_blocked(self):
        plan = self._plan()
        self.assertFalse(
            [error for error in plan.errors
             if error.startswith("HIGH_CONFIDENCE_SPATIAL_ROW_MISSING")],
            plan.errors)
        self.assertEqual(plan.status, "READY_WITH_WARNINGS")
        self.assertIn("A_ROW_RECONSTRUCTED_FROM_00", plan.warnings)
        self.assertEqual(sorted(row.row_kind for row in plan.spatial_rows), ["00", "A"])

    def test_both_outputs_get_their_own_revision_values(self):
        plan = self._plan()
        targets = {(action.output_revision, action.target_field): action
                   for action in plan.actions}
        self.assertEqual(targets[("A", "REVISION")].new_value, "A")
        self.assertEqual(targets[("00", "REVISION")].new_value, "00")
        self.assertIn("ISSUED FOR COMMENT",
                      targets[("A", "REVISION_PURPOSE")].new_value.upper())
        self.assertIn("APPROVED FOR CONSTRUCTION",
                      targets[("00", "REVISION_PURPOSE")].new_value.upper())

    def test_no_two_writes_target_the_same_cell_with_different_values(self):
        plan = self._plan()
        seen = {}
        for action in plan.actions:
            if action.action not in ("WRITE", "MOVE_ONLY", "VERIFY_ONLY"):
                continue
            key = (action.output_revision, action.handle, action.target_field)
            seen.setdefault(key, []).append(action.new_value)
        clashes = {key: values for key, values in seen.items()
                   if len(set(values)) > 1 and key[2] != "REVISION_ROW_CLEAR"}
        self.assertEqual(clashes, {}, clashes)

    def test_a_output_never_clears_a_cell_it_also_writes(self):
        """The writer ranks an empty write above a value write, per handle.

        Cleanup used to clear the 00 row for the A output; with a single-row
        table that is the same row the A row writes, so the A deliverable came
        out with a blank revision row (measured on SP-4110-0001-HP-0001).
        """
        plan = self._plan()
        written, cleared = set(), set()
        for action in plan.actions:
            if action.output_revision != "A" or not action.handle:
                continue
            if action.action == "WRITE" and not str(action.new_value).strip():
                cleared.add(action.handle)
            elif action.action != "WRITE" or str(action.new_value).strip():
                written.add(action.handle)
        self.assertEqual(written & cleared, set())

    def test_writer_gate_accepts_the_plan(self):
        import json
        plan = self._plan()
        payload = {"plans": [
            {
                "identity": plan.identity,
                "drawing_type": plan.drawing_type,
                "status": plan.status,
                "primary_geometry": plan.primary_geometry,
                "actions": [
                    {
                        "output_revision": action.output_revision,
                        "handle": action.handle,
                        "action": action.action,
                        "old_value": action.old_value,
                        "new_value": action.new_value,
                        "target_field": action.target_field,
                        "reason": action.reason,
                        "layer": action.layer,
                    }
                    for action in plan.actions
                ],
            }
        ]}
        path = Path(self.tmp.name) / "plan.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        accepted, rejected = StableExternalActionWriter.load_ready_plans(
            path, return_rejected=True)
        self.assertEqual(len(accepted), 1, rejected)


class SafeTextContractTests(unittest.TestCase):
    def test_lisp_strips_every_control_character(self):
        lsp = (Path(__file__).resolve().parents[1] / "lisp"
               / "title_block_discovery.lsp").read_text(encoding="utf-8")
        start = lsp.index("(defun GNI:SAFE")
        body = lsp[start:lsp.index("(defun GNI:ROW", start)]
        code = "\n".join(line.split(";")[0] for line in body.splitlines())
        # `vl-string-subst` replaces only the first match, which left a second
        # line break inside the TSV field and merged the following rows.
        self.assertNotIn("vl-string-subst", code)
        self.assertIn('vl-string-translate "\\t\\r\\n" "   "', code)


if __name__ == "__main__":
    unittest.main()
