from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import os
from typing import Dict, List, Set

from revision.controlled_writer import ControlledRevisionWriter

CLEANUP_WRITER_VERSION = "0.4B.2.6.2-complete-row-shutdown-guard"
EXPECTED_SOURCE_POLICY = "PRIMARY_HIGHEST_REVISION_FOR_A_AND_00"
EXPECTED_CLEANUP_POLICY = "A_KEEP_A;00_KEEP_A_AND_00;CLEAR_COMPLETE_NON_RETAINED_ROW_BANDS"
CLEANUP_REASON = "CLEAR_NON_OUTPUT_REVISION_ROW"


class CleanupPlanError(RuntimeError):
    pass


class CorrectedHistoryCleanupWriter(ControlledRevisionWriter):
    """Controlled local A/00 writer with revision-history retention.

    Retention matrix:
        source A  -> keep in A, keep in 00
        source 00 -> clear in A, keep in 00
        source 01+ -> clear in A, clear in 00

    Both local outputs are independently copied from primary_geometry, which is
    the highest numeric source revision selected by the scanner.
    """

    @staticmethod
    def _normal_revision(value: object) -> str:
        text = str(value or "").strip().upper()
        if text == "A":
            return "A"
        if text.isdigit():
            return text.zfill(2)
        return text

    @classmethod
    def _cleanup_actions(cls, plan: Dict, output_revision: str) -> List[Dict]:
        return [
            action
            for action in plan.get("actions", [])
            if action.get("output_revision") == output_revision
            and action.get("reason") == CLEANUP_REASON
            and action.get("action") == "WRITE"
            and action.get("new_value", "") == ""
        ]

    @staticmethod
    def _retained_current_actions(plan: Dict, output_revision: str) -> List[Dict]:
        required = {"REVISION", "REVISION_DATE", "REVISION_PURPOSE"}
        return [
            action
            for action in plan.get("actions", [])
            if action.get("output_revision") == output_revision
            and action.get("target_field") in required
            and action.get("reason") != CLEANUP_REASON
        ]

    @classmethod
    def _cleared_revisions(cls, actions: List[Dict]) -> Set[str]:
        return {
            cls._normal_revision(action.get("old_value"))
            for action in actions
            if action.get("target_field") == "REVISION_ROW_CLEAR"
        }

    @classmethod
    def _detected_revisions(cls, plan: Dict) -> Set[str]:
        revisions = set()
        for row in plan.get("cleanup_rows", []):
            revision = cls._normal_revision(row.get("revision"))
            if revision:
                revisions.add(revision)
        # Fallback for plans whose dataclass serialization does not expose the
        # extended cleanup_rows attribute. spatial_rows always contains A/00.
        for row in plan.get("spatial_rows", []):
            revision = cls._normal_revision(row.get("row_kind"))
            if revision:
                revisions.add(revision)
        return revisions

    @classmethod
    def validate_plan(cls, plan: Dict) -> List[str]:
        errors: List[str] = []
        status = str(plan.get("status", ""))
        if not status.startswith("READY"):
            errors.append("PLAN_NOT_READY:" + status)

        primary = str(plan.get("primary_geometry", "")).strip()
        if not primary:
            errors.append("PRIMARY_GEOMETRY_MISSING")
        elif not Path(primary).is_file():
            errors.append("PRIMARY_GEOMETRY_NOT_FOUND:" + primary)

        strategy = str(plan.get("source_strategy", "") or "")
        if strategy not in ("", EXPECTED_SOURCE_POLICY):
            errors.append("UNEXPECTED_SOURCE_STRATEGY:" + strategy)

        donor = str(plan.get("revision_a_donor", "") or "").strip()
        if donor:
            errors.append("EXTERNAL_A_DONOR_NOT_ALLOWED:" + donor)

        detected = cls._detected_revisions(plan)
        if "A" not in detected:
            errors.append("HISTORY_ROW_A_NOT_DETECTED")
        if "00" not in detected:
            errors.append("CURRENT_ROW_00_NOT_DETECTED")

        # The current output row must still have controlled revision, date, and
        # purpose actions. For output 00, row A is intentionally preserved by
        # doing nothing to its handles.
        for output in ("A", "00"):
            retained = cls._retained_current_actions(plan, output)
            fields = {action.get("target_field") for action in retained}
            for required in ("REVISION", "REVISION_DATE", "REVISION_PURPOSE"):
                if required not in fields:
                    errors.append(f"CURRENT_ROW_FIELD_MISSING:{output}:{required}")

        cleanup_a = cls._cleanup_actions(plan, "A")
        cleanup_00 = cls._cleanup_actions(plan, "00")
        cleared_a = cls._cleared_revisions(cleanup_a)
        cleared_00 = cls._cleared_revisions(cleanup_00)

        # Output A must remove AFC and every later numeric revision.
        if "00" not in cleared_a:
            errors.append("OUTPUT_A_MUST_CLEAR_00")
        if "A" in cleared_a:
            errors.append("OUTPUT_A_MUST_RETAIN_A")

        # Output 00 must retain both its review history and its current row.
        if "A" in cleared_00:
            errors.append("OUTPUT_00_MUST_RETAIN_HISTORY_A")
        if "00" in cleared_00:
            errors.append("OUTPUT_00_MUST_RETAIN_CURRENT_00")

        later_numeric = {
            revision
            for revision in detected
            if revision.isdigit() and int(revision) >= 1
        }
        missing_from_a = later_numeric - cleared_a
        missing_from_00 = later_numeric - cleared_00
        if missing_from_a:
            errors.append(
                "OUTPUT_A_LATER_REVISIONS_NOT_CLEARED:"
                + ",".join(sorted(missing_from_a))
            )
        if missing_from_00:
            errors.append(
                "OUTPUT_00_LATER_REVISIONS_NOT_CLEARED:"
                + ",".join(sorted(missing_from_00))
            )

        # Output 00 may legitimately have zero cleanup actions when the source
        # contains only A and 00. Output A may not, because row 00 must be
        # cleared.
        if not cleanup_a:
            errors.append("OUTPUT_A_CLEANUP_ACTIONS_MISSING")

        for action in cleanup_a + cleanup_00:
            handle = str(action.get("handle", "")).strip()
            if action.get("confidence") != "HIGH":
                errors.append("CLEANUP_NOT_HIGH_CONFIDENCE:" + handle)
            if not handle:
                errors.append("CLEANUP_HANDLE_MISSING")
            if str(action.get("old_value", "")) == "":
                errors.append("CLEANUP_OLD_VALUE_ALREADY_EMPTY:" + handle)
            if str(action.get("new_value", "")) != "":
                errors.append("CLEANUP_TARGET_NOT_EMPTY:" + handle)

        return list(dict.fromkeys(errors))

    @classmethod
    def load_ready_plans(cls, path):
        report_path = Path(path)
        data = json.loads(report_path.read_text(encoding="utf-8"))

        if data.get("a_donor_required") not in (False, None):
            raise CleanupPlanError("WRITE_PLAN_STILL_REQUIRES_A_DONOR")
        if data.get("source_policy") not in (EXPECTED_SOURCE_POLICY, None):
            raise CleanupPlanError("WRITE_PLAN_SOURCE_POLICY_MISMATCH")
        if data.get("cleanup_policy") not in (EXPECTED_CLEANUP_POLICY, None):
            raise CleanupPlanError("WRITE_PLAN_HISTORY_POLICY_MISMATCH")

        accepted = []
        rejected = []
        for plan in data.get("plans", []):
            errors = cls.validate_plan(plan)
            if errors:
                rejected.append(
                    {"identity": plan.get("identity", ""), "errors": errors}
                )
            else:
                accepted.append(plan)

        if rejected:
            details = "; ".join(
                item["identity"] + "=" + ",".join(item["errors"])
                for item in rejected
            )
            raise CleanupPlanError("HISTORY_CLEANUP_PREFLIGHT_FAILED: " + details)
        if not accepted:
            raise CleanupPlanError("NO_READY_HISTORY_CLEANUP_PLANS")
        return accepted

    @staticmethod
    def write_report(bundle_root, results):
        base_report = ControlledRevisionWriter.write_report(bundle_root, results)
        payload = json.loads(base_report.read_text(encoding="utf-8"))
        payload.update(
            {
                "version": CLEANUP_WRITER_VERSION,
                "operation": "LOCAL_A_00_GENERATION_WITH_HISTORY_RETENTION",
                "source_policy": EXPECTED_SOURCE_POLICY,
                "cleanup_policy": EXPECTED_CLEANUP_POLICY,
                "history_retention_policy": {
                    "A": ["A"],
                    "00": ["A", "00"],
                },
                "publishing_enabled": False,
                "validation_required_before_publish": True,
                "generated_at": datetime.now().astimezone().isoformat(),
            }
        )
        output = (
            Path(bundle_root)
            / "_GNI_REPORTS"
            / "revision_history_cleanup_writer_report.json"
        )
        temporary = output.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, output)
        return output
