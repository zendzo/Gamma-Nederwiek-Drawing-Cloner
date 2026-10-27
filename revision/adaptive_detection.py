from __future__ import annotations

import math
import re
from statistics import median

PATCH_VERSION = "0.4C.2F-adaptive-ga-as-detection"


def _norm_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().upper()


def _point(owner, row):
    method = getattr(owner, "_xy", None) or getattr(owner, "_point", None)
    return method(row) if method else None


def _revision_value(value):
    text = _norm_text(value)
    if text == "A":
        return "A"
    if re.fullmatch(r"0+", text):
        return "00"
    if re.fullmatch(r"\d{2}", text):
        return text
    return ""


def _date_valid(value):
    return bool(re.fullmatch(
        r"(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}[./-]\d{1,2}[./-]\d{1,2})",
        str(value or "").strip(),
    ))


def _purpose_matches(revision, value):
    text = _norm_text(value)
    if revision == "A":
        return "ISSUED FOR COMMENT" in text
    if revision == "00":
        return "APPROVED FOR CONSTRUCTION" in text
    return bool(text)


def _geometry(owner, rows):
    header = owner._find_header(rows)
    if not header:
        return [], [], 0.0, 0.0, 0.0, 0.55
    bounds = owner._column_bounds(header)
    xs = sorted(center for _, _, _, center, _ in bounds)
    span = max(xs) - min(xs) if len(xs) > 1 else 70.0
    gaps = [b - a for a, b in zip(xs, xs[1:]) if b > a]
    typical_gap = median(gaps) if gaps else max(span / 6.0, 4.0)
    margin = max(7.5, typical_gap * 0.75, span * 0.08)
    min_x = min(xs) - margin
    max_x = max(xs) + margin
    header_y = sum(item["_y"] for item in header) / len(header)
    tolerance = max(0.45, min(1.25, typical_gap * 0.055))
    return header, bounds, min_x, max_x, header_y, tolerance


def detect_detailed_rows(owner, identity, drawing_type, rows):
    header, bounds, min_x, max_x, header_y, tolerance = _geometry(owner, rows)
    if not header:
        return [], ["REVISION_HEADER_NOT_FOUND"]

    candidates = []
    for row in rows:
        point = _point(owner, row)
        if not point:
            continue
        x, y = point
        if y >= header_y - max(0.35, tolerance * 0.6):
            continue
        if not (min_x <= x <= max_x):
            continue
        nearest = min(bounds, key=lambda item: abs(x - item[3]))
        field, left, right, center, header_row = nearest
        # Outer columns are allowed to extend to the adaptive table edge.
        if field != bounds[0][0] and x < left:
            continue
        if field != bounds[-1][0] and x >= right:
            continue
        item = dict(row)
        item.update({
            "x": x, "y": y, "column": field, "column_x": center,
            "header_handle": header_row.get("handle", ""),
        })
        candidates.append(item)

    clusters = owner._cluster(candidates, tolerance) if hasattr(owner, "_cluster") else owner._cluster_y(candidates, tolerance)
    detected = []
    for cluster in clusters:
        cells = {}
        for item in cluster:
            field = item["column"]
            current = cells.get(field)
            if current is None or abs(item["x"] - item["column_x"]) < abs(current["x"] - current["column_x"]):
                cells[field] = item
        revision_cell = cells.get("REVISION")
        revision = _revision_value(revision_cell.get("value")) if revision_cell else ""
        if revision not in ("A", "00"):
            continue
        date_cell = cells.get("REVISION_DATE")
        purpose_cell = cells.get("REVISION_PURPOSE")
        change_cell = cells.get("REVISION_CHANGE_LOG")
        personnel = any(field in cells for field in ("DRAWN_BY", "CHECKED_BY", "APPROVED_BY"))
        score = 40
        evidence = ["ADAPTIVE_HEADER_ANCHORED", "ADAPTIVE_Y_CLUSTER", "REVISION_VALUE_MATCH"]
        if date_cell and _date_valid(date_cell.get("value")):
            score += 20; evidence.append("DATE_VALUE_MATCH")
        purpose_ok = bool(purpose_cell and _purpose_matches(revision, purpose_cell.get("value")))
        change_ok = bool(change_cell and _purpose_matches(revision, change_cell.get("value")))
        if purpose_ok:
            score += 25; evidence.append("PURPOSE_VALUE_MATCH")
        elif purpose_cell and str(purpose_cell.get("value", "")).strip():
            score += 10; evidence.append("PURPOSE_CELL_PRESENT")
        if change_ok:
            score += 10; evidence.append("CHANGE_LOG_VALUE_MATCH")
        if personnel:
            score += 10; evidence.append("PERSONNEL_CELL_PRESENT")
        confidence = "HIGH" if score >= 75 else "MEDIUM" if score >= 60 else "LOW"
        detected.append({
            "identity": identity,
            "drawing_type": drawing_type,
            "revision": revision,
            "row_y": round(sum(item["y"] for item in cluster) / len(cluster), 6),
            "header_y": round(header_y, 6),
            "confidence": confidence,
            "confidence_score": score,
            "evidence": ";".join(evidence),
            "cells": cells,
            "all_cells": [dict(item) for item in cluster],
        })

    warnings = []
    for expected in ("A", "00"):
        matches = [row for row in detected if row["revision"] == expected]
        if not matches:
            warnings.append("SPATIAL_ROW_NOT_FOUND:" + expected)
        elif len(matches) > 1:
            warnings.append("SPATIAL_ROW_AMBIGUOUS:" + expected + ":" + str(len(matches)))
    return detected, warnings


def adaptive_current_revision(owner, rows):
    header = owner._find_header(rows)
    header_handles = {str(row.get("handle", "")) for row in header}
    detailed, _ = detect_detailed_rows(owner, "", "", rows)
    table_x = []
    table_y = []
    for detected in detailed:
        for cell in detected.get("all_cells", []):
            table_x.append(cell["x"]); table_y.append(cell["y"])
    if header:
        table_x.extend(item["_x"] for item in header)
        table_y.extend(item["_y"] for item in header)
    table_box = None
    if table_x and table_y:
        table_box = (min(table_x) - 3.0, max(table_x) + 8.0, min(table_y) - 2.0, max(table_y) + 2.0)

    label_aliases = {
        "REVISION", "REVISION NO", "REVISION NO.", "REV NO", "REV NO.",
        "CURRENT REVISION", "CURRENT REV", "REV.", "REV",
    }
    labels = []
    values = []
    for row in rows:
        point = _point(owner, row)
        if not point:
            continue
        handle = str(row.get("handle", ""))
        value = _norm_text(row.get("value"))
        tag = _norm_text(row.get("tag"))
        if handle not in header_handles and (value in label_aliases or tag in {"REVISION_LABEL", "REV_LABEL"}):
            labels.append((row, point))
        candidate = _revision_value(value)
        semantic_tag = bool(re.search(r"(?:^|_)(?:CURRENT_?)?REV(?:ISION)?(?:_?NO)?(?:$|_)", tag))
        if candidate and (candidate == "00" or len(value) == 2 or semantic_tag):
            values.append((row, point, candidate, semantic_tag))

    scored = []
    for label, (lx, ly) in labels:
        for value, (vx, vy), candidate, semantic_tag in values:
            if value is label:
                continue
            if table_box and table_box[0] <= vx <= table_box[1] and table_box[2] <= vy <= table_box[3]:
                continue
            dx, dy = vx - lx, vy - ly
            distance = math.hypot(dx, dy)
            # Broad enough for GA/AS blocks, but only two-digit or semantically
            # tagged revision values are candidates. Frame number 8 is excluded.
            if not (-30.0 <= dx <= 45.0 and -25.0 <= dy <= 20.0):
                continue
            score = 0.0
            score += 100.0 if candidate == "00" else 70.0
            score += 70.0 if semantic_tag else 0.0
            score += 30.0 if str(value.get("block_name", "")) == str(label.get("block_name", "")) else 0.0
            score += max(0.0, 35.0 - distance)
            score += max(0.0, 20.0 - abs(dy) * 3.0)
            if dx >= -3.0:
                score += 8.0
            copied = dict(value)
            copied["_candidate_dx"] = dx
            copied["_candidate_dy"] = dy
            copied["_candidate_score"] = score
            copied["_candidate_revision"] = candidate
            scored.append((score, copied, label))

    # Attribute-tag-only fallback for title blocks without a visible label.
    if not scored:
        for value, point, candidate, semantic_tag in values:
            if not semantic_tag:
                continue
            vx, vy = point
            if table_box and table_box[0] <= vx <= table_box[1] and table_box[2] <= vy <= table_box[3]:
                continue
            copied = dict(value)
            copied.update({"_candidate_dx": 0.0, "_candidate_dy": 0.0, "_candidate_score": 120.0, "_candidate_revision": candidate})
            scored.append((120.0, copied, {"handle": "TAG_ANCHOR"}))

    if not scored:
        return None, None
    scored.sort(key=lambda item: (-item[0], abs(item[1]["_candidate_dy"]), abs(item[1]["_candidate_dx"])))
    winner = scored[0]
    # Refuse ambiguous matches rather than silently editing a frame value.
    if len(scored) > 1 and winner[0] - scored[1][0] < 5.0 and winner[1].get("handle") != scored[1][1].get("handle"):
        return None, None
    return winner[1], winner[2]


def install(RevisionWritePlanBuilder, SpatialRevisionRow, NoDonorRevisionCleanupPlanBuilder, TitleBlockCompletionPlanBuilder):
    def base_rows(cls, identity, drawing_type, rows):
        detailed, warnings = detect_detailed_rows(cls, identity, drawing_type, rows)
        output = []
        for row in detailed:
            cells = row["cells"]
            def pair(field):
                item = cells.get(field, {})
                return item.get("handle", ""), item.get("value", "")
            rh, rv = pair("REVISION"); dh, dv = pair("REVISION_DATE")
            ph, pv = pair("REVISION_PURPOSE"); ch, cv = pair("REVISION_CHANGE_LOG")
            drh, drv = pair("DRAWN_BY"); ckh, ckv = pair("CHECKED_BY"); aph, apv = pair("APPROVED_BY")
            output.append(SpatialRevisionRow(
                identity, drawing_type, row["revision"], row["row_y"], row["header_y"],
                rh, rv, dh, dv, ph, pv, ch, cv, drh, drv, ckh, ckv, aph, apv,
                row["confidence"], row["confidence_score"], row["evidence"],
                "" if row["confidence"] == "HIGH" else "MANUAL_REVIEW_REQUIRED",
            ))
        return output, warnings

    RevisionWritePlanBuilder._detect_spatial_rows = classmethod(base_rows)
    NoDonorRevisionCleanupPlanBuilder._all_revision_rows = classmethod(detect_detailed_rows)
    TitleBlockCompletionPlanBuilder._find_current_revision = classmethod(adaptive_current_revision)
