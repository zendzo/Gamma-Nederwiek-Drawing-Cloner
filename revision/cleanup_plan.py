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
        header = cls._find_header(rows)
        if not header:
            return [], ["REVISION_HEADER_NOT_FOUND"]

        bounds = cls._column_bounds(header)
        header_y = sum(item["_y"] for item in header) / len(header)
        min_x = min(center for _, _, _, center, _ in bounds)
        max_x = max(center for _, _, _, center, _ in bounds)

        candidates = []
        for row in rows:
            point = cls._xy(row)
            if not point:
                continue
            x, y = point
            if y >= header_y - 0.5 or x < min_x - 2.5 or x > max_x + 2.5:
                continue
            for field, left, right, center, header_row in bounds:
                if left <= x < right:
                    item = dict(row)
                    item.update(
                        {
                            "x": x,
                            "y": y,
                            "column": field,
                            "column_x": center,
                            "header_handle": header_row.get("handle", ""),
                        }
                    )
                    candidates.append(item)
                    break

        detected = []
        warnings = []
        for cluster in cls._cluster(candidates):
            cells = {}
            for item in cluster:
                field = item["column"]
                current = cells.get(field)
                if current is None or abs(item["x"] - item["column_x"]) < abs(
                    current["x"] - current["column_x"]
                ):
                    cells[field] = item

            revision_cell = cells.get("REVISION")
            if not revision_cell:
                continue
            revision_value = cls._norm(revision_cell.get("value"))
            if not cls.REVISION_PATTERN.fullmatch(revision_value):
                continue

            date_cell = cells.get("REVISION_DATE")
            purpose_cell = cells.get("REVISION_PURPOSE")
            change_cell = cells.get("REVISION_CHANGE_LOG")
            evidence = ["HEADER_ANCHORED", "Y_CLUSTERED", "REVISION_CELL_PRESENT"]
            score = 35

            if date_cell and cls.DATE_PATTERN.fullmatch(
                str(date_cell.get("value", "")).strip()
            ):
                score += 20
                evidence.append("DATE_CELL_PRESENT")
            if purpose_cell and str(purpose_cell.get("value", "")).strip():
                score += 20
                evidence.append("PURPOSE_CELL_PRESENT")
            if change_cell and str(change_cell.get("value", "")).strip():
                score += 10
                evidence.append("CHANGE_LOG_CELL_PRESENT")
            if any(
                field in cells for field in ("DRAWN_BY", "CHECKED_BY", "APPROVED_BY")
            ):
                score += 15
                evidence.append("PERSONNEL_CELL_PRESENT")

            confidence = "HIGH" if score >= 75 else "MEDIUM" if score >= 60 else "LOW"
            detected.append(
                {
                    "identity": identity,
                    "drawing_type": drawing_type,
                    "revision": revision_value,
                    "row_y": round(
                        sum(item["y"] for item in cluster) / len(cluster), 6
                    ),
                    "header_y": round(header_y, 6),
                    "confidence": confidence,
                    "confidence_score": score,
                    "evidence": ";".join(evidence),
                    "cells": cells,
                    "all_cells": [dict(item) for item in cluster],
                }
            )

        for revision in ("A", "00"):
            matches = [row for row in detected if row["revision"] == revision]
            if not matches:
                warnings.append("SPATIAL_ROW_NOT_FOUND:" + revision)
            elif len(matches) > 1:
                warnings.append(
                    "SPATIAL_ROW_AMBIGUOUS:" + revision + ":" + str(len(matches))
                )
        return detected, warnings

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
