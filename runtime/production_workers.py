from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import json
import os
import time
import traceback

from PySide6.QtCore import QThread, Signal

from destination.builder import DestinationBuilder
from titleblock.discovery import TitleBlockDiscoveryEngine
from revision.completion_plan import TitleBlockCompletionPlanBuilder

RUNTIME_VERSION = "0.4C.2D-2E-responsive-production-scale"


class DiscoveryWorker(QThread):
    progress = Signal(int, int, str, str)
    activity = Signal(str, str)
    completed = Signal(list, str, str, bool)
    failed = Signal(str)

    def __init__(self, groups, core_console, destination_root, bundle_name,
                 source_root, settings, application_root):
        super().__init__()
        self.groups = list(groups)
        self.core_console = core_console
        self.destination_root = destination_root
        self.bundle_name = bundle_name
        self.source_root = source_root
        self.settings = settings
        self.application_root = Path(application_root)
        self._cancel_requested = False

    def cancel(self):
        self._cancel_requested = True

    def run(self):
        try:
            builder = DestinationBuilder(
                self.destination_root, self.bundle_name, self.application_root
            )
            builder.build(self.source_root, copy_assets_to_all_cad=True)
            engine = TitleBlockDiscoveryEngine(
                self.core_console,
                self.application_root / "lisp" / "title_block_discovery.lsp",
                self.settings,
                self.application_root,
            )
            results = []
            total = len(self.groups)
            for index, group in enumerate(self.groups, 1):
                if self._cancel_requested:
                    break
                identity = str(getattr(group, "identity", ""))
                self.progress.emit(index, total, identity, "Opening drawing")
                self.activity.emit(identity, "Discovering title-block entities")
                current = engine.discover_many([group])
                results.extend(current)
                self.activity.emit(identity, "Saving per-drawing checkpoint")
                self._write_checkpoint(builder.bundle_root, results, total,
                                       self._cancel_requested)
                self.progress.emit(index, total, identity, "Drawing completed")
            json_path, csv_path = engine.write_report(builder.bundle_root, results)
            cancelled = self._cancel_requested and len(results) < total
            self._write_checkpoint(builder.bundle_root, results, total, cancelled)
            self.completed.emit(
                results, str(json_path), str(csv_path), cancelled
            )
        except Exception:
            self.failed.emit(traceback.format_exc())

    @staticmethod
    def _write_checkpoint(bundle_root, results, total, cancelled):
        report_root = Path(bundle_root) / "_GNI_REPORTS"
        report_root.mkdir(parents=True, exist_ok=True)
        path = report_root / "title_block_discovery_checkpoint.json"
        payload = {
            "schema": "GNI-Discovery-Checkpoint-0.4C.2D",
            "version": RUNTIME_VERSION,
            "updated_at": datetime.now().astimezone().isoformat(),
            "total_drawings": total,
            "completed_drawings": len(results),
            "cancelled": bool(cancelled),
            "results": [asdict(item) for item in results],
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, path)


class ProductionPlanWorker(QThread):
    progress = Signal(int, int, str, str)
    completed = Signal(list, str, str, str, str, bool)
    failed = Signal(str)

    def __init__(self, groups, discovery_csv, destination_root, bundle_name,
                 source_root, settings, application_root):
        super().__init__()
        self.groups = list(groups)
        self.discovery_csv = Path(discovery_csv)
        self.destination_root = destination_root
        self.bundle_name = bundle_name
        self.source_root = source_root
        self.settings = settings
        self.application_root = Path(application_root)
        self._cancel_requested = False

    def cancel(self):
        self._cancel_requested = True

    def run(self):
        try:
            builder = DestinationBuilder(
                self.destination_root, self.bundle_name, self.application_root
            )
            builder.build(self.source_root, copy_assets_to_all_cad=True)
            planner = TitleBlockCompletionPlanBuilder(self.settings)
            plans = []
            total = len(self.groups)
            fragments = builder.bundle_root / "_GNI_REPORTS" / "plans"
            fragments.mkdir(parents=True, exist_ok=True)
            for index, group in enumerate(self.groups, 1):
                if self._cancel_requested:
                    break
                identity = str(getattr(group, "identity", ""))
                self.progress.emit(index, total, identity, "Building spatial plan")
                one = planner.build([group], self.discovery_csv)
                plans.extend(one)
                for plan in one:
                    self._write_fragment(fragments, plan)
                self._write_index(fragments, plans, total, self._cancel_requested)
                self.progress.emit(index, total, identity, "Plan checkpoint saved")
            outputs = planner.write_reports(builder.bundle_root, plans)
            cancelled = self._cancel_requested and len(plans) < total
            self._write_index(fragments, plans, total, cancelled)
            self.completed.emit(
                plans, *(str(path) for path in outputs), cancelled
            )
        except Exception:
            self.failed.emit(traceback.format_exc())

    @staticmethod
    def _safe_name(value):
        return "".join(
            char if char.isalnum() or char in "-_." else "_"
            for char in str(value)
        )

    @classmethod
    def _write_fragment(cls, root, plan):
        path = root / (cls._safe_name(plan.identity) + ".json")
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(asdict(plan), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    @staticmethod
    def _write_index(root, plans, total, cancelled):
        path = root / "index.json"
        payload = {
            "schema": "GNI-Production-Plan-Index-0.4C.2E",
            "version": RUNTIME_VERSION,
            "updated_at": datetime.now().astimezone().isoformat(),
            "total_drawings": total,
            "completed_drawings": len(plans),
            "cancelled": bool(cancelled),
            "ready": sum(str(item.status).startswith("READY") for item in plans),
            "blocked": sum(item.status == "BLOCKED" for item in plans),
            "plans": [
                {
                    "identity": item.identity,
                    "drawing_type": item.drawing_type,
                    "status": item.status,
                    "actions": len(item.actions),
                    "warnings": list(item.warnings),
                    "errors": list(item.errors),
                }
                for item in plans
            ],
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, path)

# GNI_MANUAL_CHECK_REPORT_0_4C_2F_1
from revision.recovery_00 import install_worker_reports as _install_manual_reports
_install_manual_reports(ProductionPlanWorker)
