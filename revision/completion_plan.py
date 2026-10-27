from __future__ import annotations

from pathlib import Path
import csv
import json
import math
import os
from typing import Dict, List

from revision.cleanup_plan import NoDonorRevisionCleanupPlanBuilder
from revision.write_plan import PlanAction

COMPLETION_PLAN_VERSION = "0.4B.2.7-title-block-completion-plan"
COMPLETION_POLICY = (
    "A_DATE_ON_A_AND_00;CONTRACTOR_2GWG_TO_2GWN;"
    "CURRENT_REVISION_A_OR_00;COMPLETE_ROW_BAND_CLEANUP"
)


class TitleBlockCompletionPlanBuilder(NoDonorRevisionCleanupPlanBuilder):
    """Add missing title-block actions before reopen validation."""

    @staticmethod
    def _rows(path):
        with Path(path).open(
            "r", encoding="utf-8-sig", errors="replace", newline=""
        ) as stream:
            return list(csv.DictReader(stream))

    @staticmethod
    def _point(row):
        try:
            values = str(row.get("insertion_point", "")).split(",")
            return float(values[0]), float(values[1])
        except (ValueError, IndexError):
            return None

    @staticmethod
    def _completion_action(group, output, row, target_field, new_value, reason, evidence):
        old_value = row.get("value", "")
        return PlanAction(
            group.identity,
            group.drawing_type,
            output,
            "PRIMARY_HIGHEST_REVISION",
            group.primary_source or "",
            row.get("handle", ""),
            row.get("entity_type", ""),
            row.get("layer", ""),
            row.get("block_name", ""),
            row.get("tag", ""),
            old_value,
            target_field,
            new_value,
            "HIGH",
            98,
            evidence,
            "WRITE" if new_value != old_value else "VERIFY_ONLY",
            reason,
        )

    @classmethod
    def _find_current_revision(cls, rows):
        labels = []
        values = []
        for row in rows:
            point = cls._point(row)
            if not point:
                continue
            normalized = cls._norm(row.get("value"))
            if normalized == "REVISION":
                labels.append((row, point))
            elif normalized == "00":
                values.append((row, point))

        candidates = []
        for label, (label_x, label_y) in labels:
            for value, (value_x, value_y) in values:
                delta_x = value_x - label_x
                delta_y = value_y - label_y
                distance = math.hypot(delta_x, delta_y)
                # Current drawing revision is close to the lower-right label,
                # normally slightly right and below it. Revision-history rows
                # are much farther away from this exact label.
                if -2.0 <= delta_x <= 12.0 and -8.0 <= delta_y <= 3.0:
                    candidate = dict(value)
                    candidate["_candidate_dx"] = delta_x
                    candidate["_candidate_dy"] = delta_y
                    candidates.append((distance, candidate, label))
        if not candidates:
            return None, None
        candidates.sort(key=lambda item: (abs(item[1]["_candidate_dy"]), abs(item[1]["_candidate_dx"])))
        return candidates[0][1], candidates[0][2]

    @staticmethod
    def _existing_keys(plan):
        return {
            (action.output_revision, action.handle, action.target_field)
            for action in plan.actions
        }

    def _add_a_history_date_for_00(self, plan, group, rows, existing):
        a_row = next(
            (
                row
                for row in plan.spatial_rows
                if getattr(row, "row_kind", "") == "A"
            ),
            None,
        )
        if not a_row or not getattr(a_row, "date_handle", ""):
            plan.errors.append("A_HISTORY_DATE_HANDLE_MISSING")
            return
        source = next(
            (row for row in rows if row.get("handle") == a_row.date_handle), None
        )
        if not source:
            plan.errors.append("A_HISTORY_DATE_SOURCE_ROW_MISSING")
            return
        key = ("00", source.get("handle", ""), "REVISION_A_HISTORY_DATE")
        if key not in existing:
            plan.actions.append(
                TitleBlockCompletionPlanBuilder._completion_action(
                    group,
                    "00",
                    source,
                    "REVISION_A_HISTORY_DATE",
                    self._format_date(source.get("value", ""), self.migration_date + __import__("datetime").timedelta(days=self.a_offset)),
                    "NORMALIZE_A_HISTORY_DATE_IN_00",
                    "SPATIAL_A_ROW;HISTORY_RETAINED_IN_00",
                )
            )
            existing.add(key)

    def _add_contractor_doc_id(self, plan, group, rows, existing):
        source_token = str(getattr(self.settings, "contractor_doc_from", "2GWG") or "").strip()
        target_token = str(getattr(self.settings, "contractor_doc_to", "2GWN") or "").strip()
        if not source_token:
            plan.errors.append("CONTRACTOR_DOC_ID_FROM_EMPTY")
            return
        if not target_token:
            plan.errors.append("CONTRACTOR_DOC_ID_TO_EMPTY")
            return
        matches = [
            row for row in rows
            if source_token.casefold() in str(row.get("value", "")).casefold()
        ]
        if not matches:
            plan.warnings.append("CONTRACTOR_DOC_ID_TOKEN_NOT_FOUND:" + source_token)
            return
        for row in matches:
            old_value = str(row.get("value", ""))
            start = old_value.casefold().find(source_token.casefold())
            new_value = (
                old_value[:start] + target_token + old_value[start + len(source_token):]
            )
            for output in ("A", "00"):
                key = (output, row.get("handle", ""), "CONTRACTOR_DOCUMENT_ID")
                if key in existing:
                    continue
                plan.actions.append(
                    TitleBlockCompletionPlanBuilder._completion_action(
                        group,
                        output,
                        row,
                        "CONTRACTOR_DOCUMENT_ID",
                        new_value,
                        "CONTRACTOR_DOC_ID_MAPPING",
                        "CONFIGURED_SUBSTRING=" + source_token + "_TO_" + target_token,
                    )
                )
                existing.add(key)

    def _add_current_revision(self, plan, group, rows, existing):
        value_row, label_row = self._find_current_revision(rows)
        if not value_row:
            plan.errors.append("CURRENT_DRAWING_REVISION_NOT_FOUND")
            return
        for output, target in (("A", "A"), ("00", "00")):
            key = (output, value_row.get("handle", ""), "CURRENT_DRAWING_REVISION")
            if key in existing:
                continue
            plan.actions.append(
                TitleBlockCompletionPlanBuilder._completion_action(
                    group,
                    output,
                    value_row,
                    "CURRENT_DRAWING_REVISION",
                    target,
                    "LOWER_RIGHT_CURRENT_REVISION",
                    "NEAR_REVISION_LABEL;OUTSIDE_HISTORY_TABLE;LABEL_HANDLE="
                    + str(label_row.get("handle", "")),
                )
            )
            existing.add(key)

    def _add_coordinate_safe_row_band_actions(self, plan, group, rows):
        """Clear and layer revision text by spatial row bands.

        SP reference geometry from the user:
        - lower-right current revision 00 is near Revision at X=265.7541,
          Y=8.3795, not frame number 8 at X=263.0073, Y=5.9234.
        - A retained row occupies the low revision-table band.
        - 00 and later rows are above A and below the revision header.

        Coordinates remain dynamic for AS/GA by deriving X bounds and row Y
        values from the discovered header and spatial revision rows.
        """
        header = self._find_header(rows)
        detected, warnings = self._all_revision_rows(
            group.identity, group.drawing_type, rows
        )
        if not header:
            plan.errors.append("REVISION_HEADER_NOT_FOUND_FOR_AREA_ACTIONS")
            return
        a_row = next(
            (row for row in detected if row["revision"] == "A" and row["confidence"] == "HIGH"),
            None,
        )
        row_00 = next(
            (row for row in detected if row["revision"] == "00" and row["confidence"] == "HIGH"),
            None,
        )
        if not a_row or not row_00:
            plan.errors.append("A_OR_00_ROW_MISSING_FOR_AREA_ACTIONS")
            return

        bounds = self._column_bounds(header)
        min_x = min(center for _, _, _, center, _ in bounds) - 2.5
        max_x = max(center for _, _, _, center, _ in bounds) + 7.5
        header_y = sum(item["_y"] for item in header) / len(header)
        tolerance = 0.40

        existing_handles = {
            output: {
                action.handle
                for action in plan.actions
                if action.output_revision == output
            }
            for output in ("A", "00")
        }

        def add_area_action(output, row, action_mode, target_field, reason):
            handle = str(row.get("handle", "")).strip()
            if not handle:
                return
            old_value = row.get("value", "")
            new_value = "" if action_mode == "WRITE" else old_value
            plan.actions.append(
                PlanAction(
                    group.identity,
                    group.drawing_type,
                    output,
                    "PRIMARY_HIGHEST_REVISION",
                    group.primary_source or "",
                    handle,
                    row.get("entity_type", ""),
                    row.get("layer", ""),
                    row.get("block_name", ""),
                    row.get("tag", ""),
                    old_value,
                    target_field,
                    new_value,
                    "HIGH",
                    99,
                    "DYNAMIC_REVISION_TABLE_RECTANGLE;HEADER_ANCHORED;Y_BAND",
                    action_mode,
                    reason,
                )
            )
            existing_handles[output].add(handle)

        for row in rows:
            point = self._point(row)
            if not point:
                continue
            x, y = point
            if not (min_x <= x <= max_x):
                continue

            # Output A: retain and move all text in row A. Clear every text
            # entity above row A and below the header, including WCR, ZHT,
            # duplicate purpose/change-log, row 00, and revision 01+.
            if abs(y - a_row["row_y"]) <= tolerance:
                add_area_action(
                    "A", row, "MOVE_ONLY", "RETAINED_A_ROW_LAYER_MOVE",
                    "MOVE_RETAINED_A_ROW_TO_NEDERWIEK_DATA",
                )
            elif a_row["row_y"] + tolerance < y < header_y - 0.35:
                add_area_action(
                    "A", row, "WRITE", "REVISION_AREA_TEXT_CLEAR",
                    "CLEAR_OUTPUT_A_NON_RETAINED_REVISION_AREA",
                )

            # Output 00: retain and move both A and 00 rows. Clear only text
            # above row 00 and below the header, corresponding to revision 01+.
            if (
                abs(y - a_row["row_y"]) <= tolerance
                or abs(y - row_00["row_y"]) <= tolerance
            ):
                add_area_action(
                    "00", row, "MOVE_ONLY", "RETAINED_A_00_ROWS_LAYER_MOVE",
                    "MOVE_RETAINED_A_AND_00_ROWS_TO_NEDERWIEK_DATA",
                )
            elif row_00["row_y"] + tolerance < y < header_y - 0.35:
                add_area_action(
                    "00", row, "WRITE", "REVISION_AREA_TEXT_CLEAR",
                    "CLEAR_OUTPUT_00_REVISION_01_PLUS_AREA",
                )

    def build(self, groups, discovery_csv):
        plans = super().build(groups, discovery_csv)
        all_rows = self._rows(discovery_csv)
        grouped: Dict[str, List[Dict]] = {}
        for row in all_rows:
            grouped.setdefault(row.get("drawing_identity", ""), []).append(row)
        groups_by_identity = {group.identity: group for group in groups}

        for plan in plans:
            group = groups_by_identity.get(plan.identity)
            if not group:
                plan.errors.append("GROUP_NOT_FOUND_FOR_COMPLETION_PLAN")
                plan.status = "BLOCKED"
                continue
            rows = grouped.get(plan.identity, [])
            existing = self._existing_keys(plan)
            self._add_a_history_date_for_00(plan, group, rows, existing)
            self._add_contractor_doc_id(plan, group, rows, existing)
            self._add_current_revision(plan, group, rows, existing)
            self._add_coordinate_safe_row_band_actions(plan, group, rows)
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
        outputs = NoDonorRevisionCleanupPlanBuilder.write_reports(bundle_root, plans)
        json_path = outputs[0]
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        payload["version"] = COMPLETION_PLAN_VERSION
        payload["completion_policy"] = COMPLETION_POLICY
        payload["layer_color_default_aci"] = 6
        temporary = json_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, json_path)
        return outputs

# GNI_ADAPTIVE_DETECTION_0_4C_2F
from revision.adaptive_detection import install as _install_adaptive_detectors
from revision.write_plan import RevisionWritePlanBuilder as _BasePlanBuilder, SpatialRevisionRow as _SpatialRevisionRow
_install_adaptive_detectors(_BasePlanBuilder, _SpatialRevisionRow, NoDonorRevisionCleanupPlanBuilder, TitleBlockCompletionPlanBuilder)

# GNI_ROW00_RECOVERY_0_4C_2F_1
from revision.recovery_00 import install as _install_row00_recovery
_install_row00_recovery(_BasePlanBuilder, _SpatialRevisionRow, NoDonorRevisionCleanupPlanBuilder, TitleBlockCompletionPlanBuilder)
