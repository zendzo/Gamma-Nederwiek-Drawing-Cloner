from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import csv
import json
import math
import os
import re
from typing import Dict, List

from revision.write_plan import RevisionWritePlanBuilder, PlanAction

CLEANUP_PLAN_VERSION = "0.4B.2.5.2-complete-row-band-cleanup"


class NoDonorRevisionCleanupPlanBuilder(RevisionWritePlanBuilder):
    """Extend the spatial plan without requiring an external *_A.dwg donor.

    Both A and 00 outputs use the same highest-numeric primary geometry.
    The retained output row is normalized. Every detected non-output revision
    row is cleared cell-by-cell while title-block geometry remains untouched.
    """

    REVISION_PATTERN = re.compile(r"^(?:A|\d{1,2})$", re.I)
    DATE_PATTERN = re.compile(
        r"^(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|"
        r"\d{4}[./-]\d{1,2}[./-]\d{1,2})$"
    )

    @staticmethod
    def _read_rows(path):
        with Path(path).open(
            "r", encoding="utf-8-sig", errors="replace", newline=""
        ) as stream:
            return list(csv.DictReader(stream))

    @staticmethod
    def _cluster(items, tolerance=0.35):
        groups = []
        for item in sorted(items, key=lambda value: value["y"]):
            if not groups:
                groups.append([item])
                continue
            mean_y = sum(value["y"] for value in groups[-1]) / len(groups[-1])
            if abs(item["y"] - mean_y) <= tolerance:
                groups[-1].append(item)
            else:
                groups.append([item])
        return groups

    @classmethod
    def _all_revision_rows(cls, identity, drawing_type, rows):
        # The 00-row recovery detector used to be installed here by a monkey
        # patch; call it explicitly so cleanup sees the same rows as the planner.
        from revision.recovery_00 import detect_with_recovery
        return detect_with_recovery(cls, identity, drawing_type, rows)

    @staticmethod
    def _cleanup_action(group, output_revision, row, field, detected_row):
        return PlanAction(
            group.identity,
            group.drawing_type,
            output_revision,
            "PRIMARY_HIGHEST_REVISION",
            group.primary_source or "",
            row.get("handle", ""),
            row.get("entity_type", ""),
            row.get("layer", ""),
            row.get("block_name", ""),
            row.get("tag", ""),
            row.get("value", ""),
            field,
            "",
            detected_row["confidence"],
            detected_row["confidence_score"],
            detected_row["evidence"] + ";NON_OUTPUT_REVISION_ROW",
            "WRITE",
            "CLEAR_NON_OUTPUT_REVISION_ROW",
        )

    def _add_cleanup_actions(self, plan, group, all_rows):
        by_identity = [
            row for row in all_rows if row.get("drawing_identity") == group.identity
        ]
        detected, warnings = self._all_revision_rows(
            group.identity, group.drawing_type, by_identity
        )
        plan.warnings.extend(warning for warning in warnings if warning not in plan.warnings)

        high_rows = [row for row in detected if row["confidence"] == "HIGH"]
        if not any(row["revision"] == "A" for row in high_rows):
            plan.errors.append("HIGH_CONFIDENCE_SPATIAL_ROW_MISSING:A")
        if not any(row["revision"] == "00" for row in high_rows):
            plan.errors.append("HIGH_CONFIDENCE_SPATIAL_ROW_MISSING:00")

        cleanup_fields = {
            "REVISION": "REVISION_ROW_CLEAR",
            "REVISION_DATE": "REVISION_DATE_CLEAR",
            "DRAWN_BY": "DRAWN_BY_CLEAR",
            "REVISION_PURPOSE": "REVISION_PURPOSE_CLEAR",
            "REVISION_CHANGE_LOG": "REVISION_CHANGE_LOG_CLEAR",
            "CHECKED_BY": "CHECKED_BY_CLEAR",
            "APPROVED_BY": "APPROVED_BY_CLEAR",
        }

        existing_keys = {
            (action.output_revision, action.handle, action.target_field)
            for action in plan.actions
        }
        cleanup_count = {"A": 0, "00": 0}

        retained_revisions = {
            "A": {"A"},
            "00": {"A", "00"},
        }

        for output_revision in ("A", "00"):
            for detected_row in high_rows:
                # Revision history policy:
                # A output keeps only A.
                # 00 output keeps both A history and current row 00.
                if detected_row["revision"] in retained_revisions[output_revision]:
                    continue
                seen_handles = set()
                for cell in detected_row.get("all_cells", []):
                    handle = str(cell.get("handle", "")).strip()
                    if not handle or handle in seen_handles:
                        continue
                    seen_handles.add(handle)
                    column = cell.get("column", "")
                    target_field = cleanup_fields.get(
                        column, "REVISION_ROW_AUXILIARY_TEXT_CLEAR"
                    )
                    key = (output_revision, handle, target_field)
                    if key in existing_keys:
                        continue
                    plan.actions.append(
                        self._cleanup_action(
                            group, output_revision, cell, target_field, detected_row
                        )
                    )
                    existing_keys.add(key)
                    cleanup_count[output_revision] += 1

        plan.warnings.append(
            "CLEANUP_ACTIONS:A="
            + str(cleanup_count["A"])
            + ";00="
            + str(cleanup_count["00"])
        )
        return detected

    def build(self, groups, discovery_csv):
        plans = super().build(groups, discovery_csv)
        all_rows = self._read_rows(discovery_csv)
        groups_by_identity = {group.identity: group for group in groups}

        for plan in plans:
            group = groups_by_identity.get(plan.identity)
            if group is None:
                plan.errors.append("GROUP_NOT_FOUND_AFTER_BASE_PLAN")
                plan.status = "BLOCKED"
                continue

            # External *_A donor is intentionally not required.
            plan.errors = [
                error for error in plan.errors if error != "REVISION_A_DONOR_MISSING"
            ]
            plan.revision_a_donor = ""
            plan.warnings.append("A_OUTPUT_SOURCE=PRIMARY_HIGHEST_REVISION")
            plan.warnings.append("00_OUTPUT_SOURCE=PRIMARY_HIGHEST_REVISION")

            detected = self._add_cleanup_actions(plan, group, all_rows)
            setattr(plan, "cleanup_rows", detected)
            setattr(plan, "source_strategy", "PRIMARY_HIGHEST_REVISION_FOR_A_AND_00")

            # Remove duplicate errors produced by base and cleanup passes.
            plan.errors = list(dict.fromkeys(plan.errors))
            plan.warnings = list(dict.fromkeys(plan.warnings))
            plan.status = (
                "BLOCKED"
                if plan.errors
                else "READY_WITH_WARNINGS"
                if plan.warnings
                else "READY"
            )
        return plans

    @staticmethod
    def write_reports(bundle_root, plans):
        json_path, csv_path, spatial_path = RevisionWritePlanBuilder.write_reports(
            bundle_root, plans
        )
        report_root = Path(bundle_root) / "_GNI_REPORTS"
        cleanup_path = report_root / "revision_cleanup_plan.csv"
        fields = [
            "identity",
            "drawing_type",
            "output_revision",
            "handle",
            "target_field",
            "old_value",
            "new_value",
            "action",
            "reason",
            "confidence",
            "evidence",
        ]
        with cleanup_path.open(
            "w", encoding="utf-8-sig", newline=""
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for plan in plans:
                for action in plan.actions:
                    if action.reason != "CLEAR_NON_OUTPUT_REVISION_ROW":
                        continue
                    writer.writerow({field: getattr(action, field) for field in fields})

        payload = json.loads(json_path.read_text(encoding="utf-8"))
        payload["version"] = CLEANUP_PLAN_VERSION
        payload["source_policy"] = "PRIMARY_HIGHEST_REVISION_FOR_A_AND_00"
        payload["a_donor_required"] = False
        payload["cleanup_policy"] = "A_KEEP_A;00_KEEP_A_AND_00;CLEAR_COMPLETE_NON_RETAINED_ROW_BANDS"
        payload["history_retention_policy"] = {
            "A": ["A"],
            "00": ["A", "00"],
        }
        temporary = json_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, json_path)
        return json_path, csv_path, spatial_path, cleanup_path
