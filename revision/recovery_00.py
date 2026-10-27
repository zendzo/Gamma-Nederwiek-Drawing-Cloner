from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import csv
import json
import os
import re

from revision import adaptive_detection as adaptive

PATCH_VERSION = "0.4C.2F.1-row00-recovery-manual-report"


def _norm(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().upper()


def _numeric_revision(value):
    text = _norm(value)
    return text if re.fullmatch(r"\d{2}", text) else ""


def _parse_date(value):
    text = str(value or "").strip()
    for pattern in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    return None


def _all_spatial_clusters(owner, rows):
    header, bounds, min_x, max_x, header_y, tolerance = adaptive._geometry(owner, rows)
    if not header:
        return [], ["REVISION_HEADER_NOT_FOUND"]
    candidates = []
    for row in rows:
        point = adaptive._point(owner, row)
        if not point:
            continue
        x, y = point
        if y >= header_y - max(0.35, tolerance * 0.6) or not (min_x <= x <= max_x):
            continue
        nearest = min(bounds, key=lambda item: abs(x - item[3]))
        field, left, right, center, header_row = nearest
        if field != bounds[0][0] and x < left:
            continue
        if field != bounds[-1][0] and x >= right:
            continue
        item = dict(row)
        item.update({"x": x, "y": y, "column": field, "column_x": center,
                     "header_handle": header_row.get("handle", "")})
        candidates.append(item)
    cluster_method = getattr(owner, "_cluster", None) or getattr(owner, "_cluster_y")
    clusters = cluster_method(candidates, tolerance)
    output = []
    for cluster in clusters:
        cells = {}
        for item in cluster:
            field = item["column"]
            current = cells.get(field)
            if current is None or abs(item["x"] - item["column_x"]) < abs(current["x"] - current["column_x"]):
                cells[field] = item
        rev_cell = cells.get("REVISION")
        revision = _numeric_revision(rev_cell.get("value")) if rev_cell else ""
        if not revision:
            continue
        output.append({
            "revision": revision,
            "row_y": round(sum(item["y"] for item in cluster) / len(cluster), 6),
            "header_y": round(header_y, 6),
            "cells": cells,
            "all_cells": [dict(item) for item in cluster],
        })
    return output, []


def _reconstruct_00(owner, identity, drawing_type, rows, detected):
    if any(item["revision"] == "00" for item in detected):
        return None, ""
    row_a = next((item for item in detected if item["revision"] == "A" and item["confidence"] == "HIGH"), None)
    if not row_a:
        return None, "ROW00_RECOVERY_REJECTED:NO_HIGH_CONFIDENCE_A"
    numeric, _ = _all_spatial_clusters(owner, rows)
    eligible = []
    for item in numeric:
        if item["revision"] == "00":
            continue
        cells = item["cells"]
        date_cell = cells.get("REVISION_DATE")
        purpose_cell = cells.get("REVISION_PURPOSE")
        change_cell = cells.get("REVISION_CHANGE_LOG")
        purpose = _norm(purpose_cell.get("value")) if purpose_cell else ""
        change = _norm(change_cell.get("value")) if change_cell else ""
        personnel = [cells.get(name) for name in ("DRAWN_BY", "CHECKED_BY", "APPROVED_BY")]
        evidence = []
        date = _parse_date(date_cell.get("value")) if date_cell else None
        if date:
            evidence.append("VALID_DATE")
        if "APPROVED FOR CONSTRUCTION" in purpose:
            evidence.append("AFC_PURPOSE")
        if "APPROVED FOR CONSTRUCTION" in change:
            evidence.append("AFC_CHANGE_LOG")
        if sum(bool(cell and str(cell.get("value", "")).strip()) for cell in personnel) >= 2:
            evidence.append("PERSONNEL_EVIDENCE")
        if item["row_y"] > row_a["row_y"] + 0.5:
            evidence.append("BELOW_A_IN_HISTORY_SEQUENCE")
        required = {"VALID_DATE", "AFC_PURPOSE", "PERSONNEL_EVIDENCE", "BELOW_A_IN_HISTORY_SEQUENCE"}
        if required.issubset(evidence):
            eligible.append((date, int(item["revision"]), item, evidence))
    if not eligible:
        return None, "ROW00_RECOVERY_REJECTED:INSUFFICIENT_AFC_EVIDENCE"
    eligible.sort(key=lambda entry: (entry[0], entry[1], entry[2]["row_y"]))
    first = eligible[0]
    if len(eligible) > 1 and eligible[1][0] == first[0] and eligible[1][1] == first[1]:
        return None, "ROW00_RECOVERY_REJECTED:AMBIGUOUS_EARLIEST_AFC_ROW"
    source = first[2]
    cells = source["cells"]
    revision_cell = cells["REVISION"]
    recovered = {
        "identity": identity,
        "drawing_type": drawing_type,
        "revision": "00",
        "row_y": source["row_y"],
        "header_y": source["header_y"],
        "confidence": "HIGH",
        "confidence_score": 90,
        "evidence": ";".join([
            "RECONSTRUCTED_ROW_00",
            "SOURCE_NUMERIC_REVISION=" + source["revision"],
            "EARLIEST_AFC_NUMERIC_ROW",
            "HIGH_CONFIDENCE_A_ANCHOR",
            *first[3],
        ]),
        "cells": cells,
        "all_cells": source["all_cells"],
    }
    # Preserve the actual handle and old value. The writer will change this
    # numeric AFC row to 00 through the normal spatial action pipeline.
    recovered["cells"]["REVISION"] = dict(revision_cell)
    return recovered, "ROW00_RECONSTRUCTED_FROM_NUMERIC_AFC:" + source["revision"]


def detect_with_recovery(owner, identity, drawing_type, rows):
    detected, warnings = adaptive.detect_detailed_rows(owner, identity, drawing_type, rows)
    recovered, diagnostic = _reconstruct_00(owner, identity, drawing_type, rows, detected)
    if recovered:
        detected.append(recovered)
        warnings = [item for item in warnings if item != "SPATIAL_ROW_NOT_FOUND:00"]
        warnings.append(diagnostic)
    elif diagnostic:
        warnings.append(diagnostic)
    return detected, warnings


def _display_migration_date(value):
    text = str(value or "").strip()
    for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, pattern).strftime("%d.%m.%Y")
        except ValueError:
            pass
    return text


def correct_plan_dates(plans):
    """Force the 00 revision date to the migration date on every plan.

    Called by ``TitleBlockCompletionPlanBuilder.build``; previously this was
    applied by wrapping that method at import time.
    """
    for plan in plans:
        migration_iso = str(getattr(plan, "migration_date", ""))
        if hasattr(plan, "revision_00_date"):
            plan.revision_00_date = migration_iso
        display = _display_migration_date(migration_iso)
        row00 = next((row for row in getattr(plan, "spatial_rows", []) if getattr(row, "row_kind", "") == "00"), None)
        date_handle = getattr(row00, "date_handle", "") if row00 else ""
        if not date_handle:
            continue
        for action in getattr(plan, "actions", []):
            if (getattr(action, "output_revision", "") == "00" and
                    getattr(action, "handle", "") == date_handle and
                    getattr(action, "target_field", "") == "REVISION_DATE"):
                action.new_value = display
                action.reason = "REVISION_00_DATE_EQUALS_MIGRATION_DATE"
    return plans


def install(RevisionWritePlanBuilder, SpatialRevisionRow,
            NoDonorRevisionCleanupPlanBuilder, TitleBlockCompletionPlanBuilder):
    """Deprecated no-op.

    The patch body now lives in real code paths: ``RevisionWritePlanBuilder.
    _detect_spatial_rows`` calls :func:`detect_with_recovery`, and
    ``TitleBlockCompletionPlanBuilder.build`` calls :func:`correct_plan_dates`.
    Kept so older entrypoints that import this name keep working.
    """
    return None


def write_manual_check_report(report_root, plans, cancelled=False):
    """Write manual_check_required.json/csv for every BLOCKED plan.

    Previously installed by monkey-patching ``ProductionPlanWorker._write_index``
    at import time; it is now called explicitly from that method.
    """
    manual = [plan for plan in plans if str(getattr(plan, "status", "")) == "BLOCKED"]
    report_root = Path(report_root)
    report_root.mkdir(parents=True, exist_ok=True)
    records = []
    for plan in manual:
        records.append({
            "identity": plan.identity,
            "drawing_type": plan.drawing_type,
            "status": plan.status,
            "eligible_for_automatic_writer": False,
            "errors": list(plan.errors),
            "warnings": list(plan.warnings),
            "recommended_action": "CHECK_REVISION_HISTORY_AND_CURRENT_REVISION_MANUALLY",
        })
    payload = {
        "schema": "GNI-Manual-Check-Required-0.4C.7",
        "version": PATCH_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(),
        "summary": {
            "total_planned": len(plans),
            "automatic_processing_ready": len(plans) - len(manual),
            "manual_check_required": len(manual),
            "automatic_completion_percent": round((len(plans) - len(manual)) * 100.0 / len(plans), 1) if plans else 0.0,
            "cancelled": bool(cancelled),
        },
        "drawings": records,
    }
    json_path = report_root / "manual_check_required.json"
    temporary = json_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, json_path)
    csv_path = report_root / "manual_check_required.csv"
    temp_csv = csv_path.with_suffix(".csv.tmp")
    with temp_csv.open("w", newline="", encoding="utf-8-sig") as stream:
        fields = ["identity", "drawing_type", "status", "eligible_for_automatic_writer", "errors", "warnings", "recommended_action"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in records:
            copied = dict(row)
            copied["errors"] = ";".join(copied["errors"])
            copied["warnings"] = ";".join(copied["warnings"])
            writer.writerow(copied)
    os.replace(temp_csv, csv_path)


def install_worker_reports(ProductionPlanWorker):
    """Deprecated no-op kept so older entrypoints keep importing cleanly."""
    return ProductionPlanWorker
