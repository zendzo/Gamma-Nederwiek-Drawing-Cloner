"""Tests for the JSON-backed Reports dashboard.

The aggregation rules are Qt-free on purpose: these tests can prove the
dashboard reads the handoff reports correctly without AutoCAD, a display, or a
running pipeline.  A small offscreen section covers the widget wiring.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from reporting.dashboard_data import (
    STAGE_FILES,
    STAGES,
    build_snapshot,
    status_tone,
)


def _payload_with(results, summary, *, complete=None):
    payload = {
        "schema": "TEST",
        "generated_at": "2026-10-29T10:00:00+07:00",
        "summary": summary,
        "results": results,
    }
    if complete is not None:
        payload["complete"] = complete
    return payload


def _pair(identity, status, outputs=None, errors=None):
    return {
        "identity": identity,
        "drawing_type": "SP",
        "status": status,
        "outputs": outputs or [],
        "errors": errors or [],
    }


def write_complete_reports(reports: Path, *, writer_issue: bool = False) -> None:
    """Write one clean report per stage, plus a deliberately broken writer row."""

    reports.mkdir(parents=True, exist_ok=True)
    folders = [f"F{index}/CAD" for index in range(16)]

    (reports / "destination_manifest.json").write_text(
        json.dumps(
            {
                "schema": "GNI-Destination-Manifest-1.3",
                "generated_at": "2026-10-29T10:00:00+07:00",
                "required_folders": folders,
                "result": {
                    "status": "SUCCESS",
                    "created_folders": folders,
                    "existing_folders": [],
                    "copied_assets": ["logo.png", "tenant.png"],
                    "warnings": [],
                },
            }
        ),
        encoding="utf-8",
    )
    (reports / "supporting_file_clone_report.json").write_text(
        json.dumps(
            {
                "schema": "GNI-Supporting-Clone-1.0",
                "summary": {
                    "total": 2,
                    "success": 2,
                    "skipped": 0,
                    "failed": 0,
                    "part_list": 1,
                    "nc1": 1,
                    "dxf": 0,
                },
                "results": [],
            }
        ),
        encoding="utf-8",
    )
    (reports / "title_block_discovery_report.json").write_text(
        json.dumps(
            {
                "schema": "GNI-TitleBlock-Discovery-0.4A.2",
                "complete": True,
                "summary": {
                    "drawings": 2,
                    "success": 2,
                    "failed": 0,
                    "fields": 12,
                },
                "results": [
                    {
                        "identity": "D1",
                        "drawing_type": "SP",
                        "status": "SUCCESS",
                        "fields": [],
                    },
                    {
                        "identity": "D2",
                        "drawing_type": "AS",
                        "status": "SUCCESS",
                        "fields": [],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (reports / "revision_pilot_write_plan.json").write_text(
        json.dumps(
            {
                "schema": "GNI-Revision-Spatial-Plan",
                "complete": True,
                "summary": {
                    "plans": 2,
                    "ready": 2,
                    "blocked": 0,
                    "actions": 6,
                    "spatial_rows": 4,
                },
                "plans": [
                    {
                        "identity": "D1",
                        "drawing_type": "SP",
                        "status": "READY",
                        "actions": [{}],
                        "warnings": [],
                        "errors": [],
                    },
                    {
                        "identity": "D2",
                        "drawing_type": "AS",
                        "status": "READY",
                        "actions": [{}],
                        "warnings": [],
                        "errors": [],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    writer_results = [
        _pair(
            "D1",
            "LOCAL_PAIR_WRITTEN_NOT_VALIDATED",
            outputs=[{"actions_verified": 3}],
        ),
        _pair(
            "D2",
            "FAILED" if writer_issue else "LOCAL_PAIR_WRITTEN_NOT_VALIDATED",
            outputs=[] if writer_issue else [{"actions_verified": 3}],
            errors=["ACTION_NOT_WRITTEN"] if writer_issue else [],
        ),
    ]
    (reports / "stable_external_writer_report.json").write_text(
        json.dumps(
            {
                "schema": "GNI-Stable-External-Writer",
                "summary": {
                    "plans": 2,
                    "local_pairs": 1 if writer_issue else 2,
                    "failed": 1 if writer_issue else 0,
                },
                "results": writer_results,
            }
        ),
        encoding="utf-8",
    )
    (reports / "reopen_validation_report.json").write_text(
        json.dumps(
            {
                "schema": "GNI-Reopen-Validator",
                "summary": {"pairs": 2, "validated": 2, "failed": 0},
                "results": [
                    _pair("D1", "VALIDATED", outputs=[{"checks_passed": 4}]),
                    _pair("D2", "VALIDATED", outputs=[{"checks_passed": 4}]),
                ],
            }
        ),
        encoding="utf-8",
    )
    (reports / "atomic_publish_dry_run_report.json").write_text(
        json.dumps(
            {
                "schema": "GNI-Atomic-Publisher-Dry-Run",
                "summary": {
                    "pairs": 2,
                    "ready": 2,
                    "blocked": 0,
                    "dwg_overwrites": 0,
                    "dwg_backups_required": 0,
                },
                "results": [
                    _pair("D1", "DRY_RUN_READY_TO_PUBLISH"),
                    _pair("D2", "DRY_RUN_READY_TO_PUBLISH"),
                ],
            }
        ),
        encoding="utf-8",
    )
    (reports / "atomic_publish_report.json").write_text(
        json.dumps(
            {
                "schema": "GNI-Atomic-Publisher",
                "summary": {
                    "transactions": 2,
                    "published": 2,
                    "rolled_back": 0,
                    "failed_unchanged": 0,
                    "critical": 0,
                },
                "transactions": [
                    {
                        "identity": "D1",
                        "drawing_type": "SP",
                        "status": "PUBLISHED",
                        "errors": [],
                    },
                    {
                        "identity": "D2",
                        "drawing_type": "AS",
                        "status": "PUBLISHED",
                        "errors": [],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (reports / "pdf_publish_report.json").write_text(
        json.dumps(
            {
                "schema": "GNI-PDF-Publisher",
                "complete": True,
                "summary": {
                    "pairs": 2,
                    "published": 2,
                    "unchanged": 0,
                    "failed": 0,
                },
                "results": [
                    _pair("D1", "PDF_PAIR_PUBLISHED"),
                    _pair("D2", "PDF_PAIR_PUBLISHED"),
                ],
            }
        ),
        encoding="utf-8",
    )


class DashboardDataTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.reports = self.root / "NEDERWIEK BETA" / "_GNI_REPORTS"

    def tearDown(self):
        self._tmp.cleanup()

    def test_complete_pipeline_reads_every_report(self):
        write_complete_reports(self.reports)
        snapshot = build_snapshot(self.reports)

        self.assertEqual(snapshot.verdict, "COMPLETE")
        self.assertEqual((snapshot.completed, snapshot.total), (10, 10))
        self.assertEqual(snapshot.percent, 100)
        self.assertEqual(snapshot.issue_count, 0)
        self.assertEqual(snapshot.bundle, "NEDERWIEK BETA")
        self.assertEqual(
            [stage.state for stage in snapshot.stages],
            ["COMPLETE"] * 10,
        )

        values = {kpi.label: kpi.value for kpi in snapshot.kpis}
        self.assertEqual(values["Drawings"], "2")
        self.assertEqual(values["Pairs planned"], "2")
        self.assertEqual(values["Pairs written"], "2")
        self.assertEqual(values["Pairs validated"], "2")
        self.assertEqual(values["Dry run ready"], "2")
        self.assertEqual(values["Published DWG"], "2")
        self.assertEqual(values["PDFs"], "2")
        self.assertEqual(values["Issues"], "0")

        self.assertEqual([row.identity for row in snapshot.drawings], ["D1", "D2"])
        first = snapshot.drawings[0]
        self.assertEqual(first.drawing_type, "SP")
        self.assertEqual(first.statuses["discovery"], "OK")
        self.assertEqual(first.statuses["plan"], "READY")
        self.assertEqual(first.statuses["writer"], "WRITTEN")
        self.assertEqual(first.statuses["validator"], "VALIDATED")
        self.assertEqual(first.statuses["dryrun"], "READY")
        self.assertEqual(first.statuses["publish"], "PUBLISHED")
        self.assertEqual(first.statuses["pdf"], "PUBLISHED")

    def test_missing_clone_is_optional_not_pending(self):
        write_complete_reports(self.reports)
        (self.reports / "supporting_file_clone_report.json").unlink()

        snapshot = build_snapshot(self.reports)
        states = {stage.key: stage.state for stage in snapshot.stages}
        self.assertEqual(states["clone"], "OPTIONAL")
        self.assertEqual(states["pdf"], "COMPLETE")
        # The optional stage still counts as done once the job has evidence.
        self.assertEqual(snapshot.completed, 10)
        self.assertEqual(snapshot.verdict, "COMPLETE")

    def test_incomplete_report_is_never_accepted(self):
        write_complete_reports(self.reports)
        plan = json.loads(
            (self.reports / "revision_pilot_write_plan.json").read_text(encoding="utf-8")
        )
        plan["complete"] = False
        plan["cancelled"] = True
        plan["cancel_reason"] = "CANCELLED"
        (self.reports / "revision_pilot_write_plan.json").write_text(
            json.dumps(plan), encoding="utf-8"
        )

        snapshot = build_snapshot(self.reports)
        state = next(stage for stage in snapshot.stages if stage.key == "plan")
        self.assertEqual(state.state, "INCOMPLETE")
        self.assertEqual(snapshot.verdict, "NEEDS ATTENTION")
        self.assertTrue(
            any("marked incomplete" in issue for issue in snapshot.issues),
            snapshot.issues,
        )

    def test_stale_report_is_quarantined_and_flagged(self):
        write_complete_reports(self.reports)
        report = self.reports / "stable_external_writer_report.json"
        stale = Path(str(report) + ".stale")
        report.replace(stale)

        snapshot = build_snapshot(self.reports)
        state = next(stage for stage in snapshot.stages if stage.key == "writer")
        self.assertEqual(state.state, "STALE")
        self.assertEqual(state.path, stale)
        self.assertEqual(snapshot.verdict, "NEEDS ATTENTION")
        self.assertTrue(any("quarantined" in issue for issue in snapshot.issues))

    def test_malformed_report_is_unreadable(self):
        write_complete_reports(self.reports)
        (self.reports / "title_block_discovery_report.json").write_text(
            "{not valid json", encoding="utf-8"
        )

        snapshot = build_snapshot(self.reports)
        state = next(stage for stage in snapshot.stages if stage.key == "discovery")
        self.assertEqual(state.state, "UNREADABLE")
        self.assertGreaterEqual(snapshot.issue_count, 1)
        self.assertEqual(snapshot.verdict, "NEEDS ATTENTION")

    def test_gate_and_failure_files_add_blocking_issues(self):
        write_complete_reports(self.reports)
        (self.reports / "gate_rejected.json").write_text(
            json.dumps(
                {
                    "count": 1,
                    "rejected": [
                        {"identity": "D9", "errors": ["PRIMARY_GEOMETRY_NOT_FOUND"]}
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.reports / "failures.json").write_text(
            json.dumps(
                {
                    "count": 1,
                    "failures": [
                        {
                            "identity": "D8",
                            "stage": "PDF Publisher",
                            "status": "FAILED",
                            "errors": ["PLOT_TIMEOUT"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        snapshot = build_snapshot(self.reports)
        self.assertGreaterEqual(snapshot.issue_count, 2)
        self.assertEqual(snapshot.verdict, "NEEDS ATTENTION")
        joined = "\n".join(snapshot.issues)
        self.assertIn("D9", joined)
        self.assertIn("D8", joined)

    def test_missing_folder_is_not_started(self):
        snapshot = build_snapshot(self.root / "missing" / "_GNI_REPORTS")
        self.assertEqual(snapshot.verdict, "NOT STARTED")
        self.assertEqual(snapshot.completed, 0)
        self.assertEqual(snapshot.percent, 0)
        self.assertEqual(snapshot.evidence_count, 0)
        states = {stage.key: stage.state for stage in snapshot.stages}
        self.assertEqual(states["scan"], "PENDING")
        self.assertEqual(states["clone"], "OPTIONAL")
        self.assertEqual(states["pdf"], "PENDING")

    def test_stage_mapping_matches_the_controller(self):
        import app as legacy

        self.assertEqual(STAGE_FILES, legacy.STAGE_REPORTS)
        self.assertEqual(
            [(key, label) for key, label, _name in STAGES],
            list(legacy.PIPELINE),
        )

    def test_status_tone_classifies_engine_statuses(self):
        self.assertEqual(status_tone("DRY_RUN_BLOCKED"), "bad")
        self.assertEqual(status_tone("PUBLISH_FAILED_ROLLED_BACK"), "bad")
        self.assertEqual(status_tone("VALIDATED"), "ok")
        self.assertEqual(status_tone("LOCAL_PAIR_WRITTEN_NOT_VALIDATED"), "ok")
        self.assertEqual(status_tone("PENDING"), "idle")
        self.assertEqual(status_tone("READY (WARN)"), "warn")


class DashboardWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.reports = Path(self._tmp.name) / "BUNDLE" / "_GNI_REPORTS"

    def tearDown(self):
        self._tmp.cleanup()

    def _dashboard(self, **kwargs):
        from reporting.dashboard import ReportDashboard

        dashboard = ReportDashboard(**kwargs)
        self.addCleanup(dashboard.deleteLater)
        return dashboard

    def test_empty_dashboard_renders_not_started(self):
        dashboard = self._dashboard()
        dashboard.refresh(self.reports)
        self.assertEqual(dashboard.verdict.text(), "NOT STARTED")
        self.assertEqual(dashboard.table.rowCount(), 0)
        self.assertEqual(dashboard.evidence.toggle.text(), "Show evidence files (0)")
        self.assertFalse(dashboard.evidence.body.isVisible())

    def test_complete_dashboard_populates_cards_and_matrix(self):
        write_complete_reports(self.reports)
        dashboard = self._dashboard()
        dashboard.refresh(self.reports)

        self.assertEqual(dashboard.verdict.text(), "COMPLETE")
        self.assertEqual(dashboard.table.rowCount(), 2)
        self.assertIn("2 of 2 drawings", dashboard.count_label.text())
        self.assertEqual(dashboard._chips["pdf"].state.text(), "DONE")
        self.assertEqual(dashboard._cards["pdf"].meter.value(), 100)
        self.assertEqual(dashboard._cards["writer"].headline.text(), "2/2 pairs written")
        self.assertEqual(dashboard.issues_group.title(), "Blocking issues")
        empty = [
            label.text()
            for label in dashboard.issues_host.findChildren(type(dashboard.verdict))
            if label.objectName() == "dashIssuesEmpty"
        ]
        self.assertIn("No blocking issues found.", empty)
        self.assertEqual(dashboard.evidence.toggle.text(), "Show evidence files (9)")

    def test_issues_filter_narrows_the_matrix(self):
        write_complete_reports(self.reports, writer_issue=True)
        dashboard = self._dashboard()
        dashboard.refresh(self.reports)

        self.assertEqual(dashboard.table.rowCount(), 2)
        self.assertEqual(dashboard._snapshot.drawings[1].issues[0][:6], "WRITER")
        dashboard.filter_box.setCurrentIndex(1)
        self.assertEqual(dashboard.table.rowCount(), 1)
        self.assertEqual(dashboard.table.item(0, 0).text(), "D2")

    def test_evidence_section_starts_collapsed_and_toggles(self):
        from PySide6.QtWidgets import QPushButton, QTableWidget

        table = QTableWidget(0, 2)
        button = QPushButton("Refresh")
        dashboard = self._dashboard(
            evidence_table=table,
            evidence_buttons=(button,),
            evidence_note="Evidence note",
        )
        write_complete_reports(self.reports)
        dashboard.refresh(self.reports)

        self.assertFalse(dashboard.evidence.body.isVisible())
        self.assertTrue(dashboard.evidence.body.isHidden())
        dashboard.evidence.toggle.setChecked(True)
        self.assertFalse(dashboard.evidence.body.isHidden())
        self.assertTrue(dashboard.evidence.body.isVisibleTo(dashboard))
        self.assertEqual(
            dashboard.evidence.toggle.text(), "Hide evidence files (9)"
        )
        self.assertIs(table.parent(), dashboard.evidence.body)
        self.assertIs(button.parent(), dashboard.evidence.body)

    def test_search_filters_by_status_text(self):
        write_complete_reports(self.reports)
        dashboard = self._dashboard()
        dashboard.refresh(self.reports)
        dashboard.search.setText("PUBLISHED")
        self.assertEqual(dashboard.table.rowCount(), 2)
        dashboard.search.setText("does-not-exist")
        self.assertEqual(dashboard.table.rowCount(), 0)
        self.assertIn("0 of 2 drawings", dashboard.count_label.text())

    def test_refresh_is_safe_when_folder_disappears(self):
        write_complete_reports(self.reports)
        dashboard = self._dashboard()
        dashboard.refresh(self.reports)
        self.assertEqual(dashboard.table.rowCount(), 2)
        dashboard.refresh(self.reports.parent / "gone")
        self.assertEqual(dashboard.verdict.text(), "NOT STARTED")
        self.assertEqual(dashboard.table.rowCount(), 0)


class MainIntegrationTests(unittest.TestCase):
    """The controller's Reports page must host the dashboard, not bypass it."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_refresh_reports_updates_dashboard_and_evidence_table(self):
        import app as legacy

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reports = root / "BUNDLE" / "_GNI_REPORTS"
            write_complete_reports(reports)
            main = legacy.Main()
            try:
                main.dst.setText(str(root))
                main.bundle.setText("BUNDLE")
                main.refresh_reports()
                self.assertEqual(main.dashboard.verdict.text(), "COMPLETE")
                self.assertEqual(main.dashboard.table.rowCount(), 2)
                self.assertEqual(main.rtbl.rowCount(), 9)
                self.assertEqual(main.dashboard.evidence.toggle.text(),
                                 "Show evidence files (9)")
            finally:
                main.close()
                main.deleteLater()


if __name__ == "__main__":
    unittest.main()
