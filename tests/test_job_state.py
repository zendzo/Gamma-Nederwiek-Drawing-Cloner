"""Job persistence: scan_state.json round-trip and report-view reconstruction.

Loading a previous job depends on two things that live in ``runtime/job_state``:
the scan-state file that preserves the otherwise in-memory scan, and the
loaders that rebuild the supporting/discovery/plan tables from their reports.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from models import DrawingGroup, SourceFile

from runtime.job_state import (
    ScanState,
    clone_should_be_skipped,
    load_discovery_results,
    load_revision_plans,
    load_scan_state,
    load_supporting_results,
    resolve_bundle_root,
    write_scan_state,
)


def _file(identity="ID-1", revision="A", role="DWG", selected=True):
    return SourceFile(
        path=f"C:/src/{identity}_{revision}.dwg",
        relative_path=f"{identity}_{revision}.dwg",
        filename=f"{identity}_{revision}.dwg",
        extension="DWG",
        size_bytes=10,
        modified_iso="2026-01-01T00:00:00+00:00",
        role=role,
        drawing_type="SP",
        identity=identity,
        revision=revision,
        selected=selected,
    )


class ScanStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.reports = Path(self._tmp.name) / "_GNI_REPORTS"
        self.reports.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_round_trip_preserves_files_groups_and_mapping(self):
        group = DrawingGroup(
            identity="ID-1",
            drawing_type="SP",
            candidates=[_file("ID-1", "A"), _file("ID-1", "00")],
            primary_source="C:/src/ID-1_00.dwg",
            revision_a_donor="C:/src/ID-1_A.dwg",
            warning="WARN",
        )
        write_scan_state(
            self.reports,
            source_root="C:/src",
            destination_root="C:/dst",
            bundle_name="BUNDLE",
            mapping={"project_from": "FROM", "project_to": "TO"},
            files=[_file("ID-1", "A"), _file("ID-1", "00")],
            groups=[group],
            supporting=[_file("ID-1", "A", role="PART_LIST")],
        )
        state = load_scan_state(self.reports)

        self.assertIsNotNone(state)
        self.assertEqual(state.source_root, "C:/src")
        self.assertEqual(state.destination_root, "C:/dst")
        self.assertEqual(state.bundle_name, "BUNDLE")
        self.assertEqual(state.mapping["project_to"], "TO")
        self.assertEqual(len(state.files), 2)
        self.assertEqual(len(state.groups), 1)
        self.assertEqual(state.groups[0].identity, "ID-1")
        self.assertEqual(len(state.groups[0].candidates), 2)
        self.assertEqual(state.supporting[0].role, "PART_LIST")

    def test_missing_file_returns_none(self):
        self.assertIsNone(load_scan_state(self.reports))

    def test_corrupt_file_returns_none(self):
        (self.reports / "scan_state.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(load_scan_state(self.reports))

    def test_wrong_schema_returns_none(self):
        (self.reports / "scan_state.json").write_text(
            json.dumps({"schema": "OTHER"}), encoding="utf-8"
        )
        self.assertIsNone(load_scan_state(self.reports))

    def test_resolve_bundle_root_accepts_both_forms(self):
        bundle = Path(self._tmp.name) / "BUNDLE"
        reports = bundle / "_GNI_REPORTS"
        reports.mkdir(parents=True)
        self.assertEqual(resolve_bundle_root(bundle), bundle)
        self.assertEqual(resolve_bundle_root(reports), bundle)
        empty = Path(self._tmp.name) / "empty"
        empty.mkdir()
        self.assertIsNone(resolve_bundle_root(empty))

    def test_clone_should_be_skipped(self):
        self.assertTrue(clone_should_be_skipped(self.reports, []))
        self.assertTrue(
            clone_should_be_skipped(self.reports, [_file(selected=False)])
        )
        self.assertFalse(
            clone_should_be_skipped(self.reports, [_file(selected=True)])
        )
        (self.reports / "supporting_file_clone_report.json").write_text("{}", encoding="utf-8")
        self.assertFalse(
            clone_should_be_skipped(self.reports, [_file(selected=False)])
        )


class ReportViewReconstructionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.reports = Path(self._tmp.name) / "_GNI_REPORTS"
        self.reports.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_supporting_results_round_trip(self):
        from dataclasses import asdict
        from supporting.clone_engine import SupportingCloneResult

        result = SupportingCloneResult(
            source_path="C:/src/x.xls", destination_path="C:/dst/x.xls",
            role="PART_LIST", extension="XLS", status="SUCCESS",
            action="COPIED", collision_policy="BACKUP_OVERWRITE",
        )
        (self.reports / "supporting_file_clone_report.json").write_text(
            json.dumps({"results": [asdict(result)]}), encoding="utf-8"
        )
        loaded = load_supporting_results(self.reports)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].status, "SUCCESS")
        self.assertEqual(loaded[0].error, "")

    def test_discovery_results_round_trip(self):
        from dataclasses import asdict
        from titleblock.discovery import DiscoveryField, DiscoveryResult

        field = DiscoveryField(
            drawing_identity="ID-1", drawing_type="SP", source_path="C:/src/ID-1.dwg",
            drawing="ID-1", entity_type="ATTRIB", handle="AB1", layer="0", tag="TITLE",
            value="REV", insertion_point="0,0,0", owner_handle="O", owner_type="INSERT",
            block_name="TB", suggested_field="REV", confidence="HIGH",
            confidence_score=99, evidence="e", proposed_a="A", proposed_00="00",
        )
        result = DiscoveryResult(
            identity="ID-1", drawing_type="SP", source_path="C:/src/ID-1.dwg",
            status="SUCCESS", job_folder="", fields=[field],
        )
        (self.reports / "title_block_discovery_report.json").write_text(
            json.dumps({"results": [asdict(result)]}), encoding="utf-8"
        )
        loaded = load_discovery_results(self.reports)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].identity, "ID-1")
        self.assertEqual(loaded[0].fields[0].tag, "TITLE")

    def test_revision_plans_round_trip(self):
        from dataclasses import asdict
        from revision.write_plan import RevisionPlan

        plan = RevisionPlan(
            identity="ID-1", drawing_type="SP", status="READY",
            primary_geometry="C:/src/ID-1.dwg", revision_a_donor="",
            migration_date="2026-11-02", revision_a_date="2026-10-26",
            revision_00_date="2026-11-03",
            actions=[{"action": "SET", "reason": "r"}],
            spatial_rows=[{"drawing_identity": "ID-1"}],
            warnings=["WARN"], errors=[],
        )
        (self.reports / "revision_pilot_write_plan.json").write_text(
            json.dumps({"plans": [asdict(plan)]}), encoding="utf-8"
        )
        loaded = load_revision_plans(self.reports)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].identity, "ID-1")
        self.assertEqual(loaded[0].status, "READY")
        self.assertEqual(len(loaded[0].actions), 1)
        self.assertEqual(loaded[0].warnings, ["WARN"])


class LoadPreviousJobTests(unittest.TestCase):
    """End-to-end: load a mid-pipeline bundle and resume at the right stage."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import app as legacy

        self.legacy = legacy
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.bundle = self.root / "BUNDLE"
        self.reports = self.bundle / "_GNI_REPORTS"
        self.reports.mkdir(parents=True)
        self._saved_cfg = legacy.CFG
        legacy.CFG = str(self.root / "settings.json")
        # Silence the summary/error dialogs so a failing assert cannot hang.
        self._saved = {}
        for name in ("information", "warning", "critical", "question"):
            self._saved[name] = getattr(legacy.QMessageBox, name)
            setattr(legacy.QMessageBox, name, staticmethod(lambda *a, **k: None))

    def tearDown(self):
        legacy = self.legacy
        for name, value in self._saved.items():
            setattr(legacy.QMessageBox, name, value)
        legacy.CFG = self._saved_cfg
        main = getattr(self, "main", None)
        if main is not None:
            main.close()
            main.deleteLater()
        self._tmp.cleanup()

    def _write(self, key):
        name = self.legacy.STAGE_REPORTS[key]
        (self.reports / name).write_text(
            json.dumps({"complete": True}), encoding="utf-8"
        )

    def test_load_resumes_at_the_validator_checkpoint(self):
        source = self.root / "SRC"
        source.mkdir()
        write_scan_state(
            self.reports,
            source_root=str(source),
            destination_root=str(self.root),
            bundle_name="BUNDLE",
            mapping={"project_from": "FROM", "project_to": "TO"},
            files=[_file("ID-1", "A"), _file("ID-1", "00")],
            groups=[
                DrawingGroup(
                    identity="ID-1", drawing_type="SP",
                    candidates=[_file("ID-1", "A"), _file("ID-1", "00")],
                    primary_source=str(source / "ID-1_00.dwg"),
                    revision_a_donor=str(source / "ID-1_A.dwg"),
                )
            ],
            supporting=[],
        )
        for key in ("destination", "discovery", "plan", "writer"):
            self._write(key)
        (self.reports / "title_block_discovery_fields.csv").write_text("a\n", encoding="utf-8")

        main = self.legacy.Main()
        self.main = main
        main.load_previous_job(str(self.bundle))

        self.assertEqual(main.src.text(), str(source))
        self.assertEqual(main.bundle.text(), "BUNDLE")
        self.assertEqual(main.maped[0][1].text(), "FROM")  # project_from
        self.assertIn("clone", main._skipped)
        self.assertEqual(main.tbl.rowCount(), 1)
        self.assertEqual(main.next_stage(), ("validator", "Reopen validator"))

    def test_load_rejects_a_non_bundle_folder(self):
        main = self.legacy.Main()
        self.main = main
        other = self.root / "not_a_bundle"
        other.mkdir()
        # No reports, no scan state -> load leaves the fields untouched.
        main.load_previous_job(str(other))
        self.assertFalse((other / "_GNI_REPORTS").exists())


if __name__ == "__main__":
    unittest.main()
