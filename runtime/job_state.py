"""Persist and reload the one piece of pipeline state that never touches disk.

Every stage from "Build destination" onward writes a handoff report into
``_GNI_REPORTS`` and resumes from it.  The very first stage - the source scan -
only ever existed in memory, so a reopened session could not be pointed back at
a half-finished bundle.  ``scan_state.json`` closes that gap: it records the
source folder, the replacement mapping and the scanned files/groups/supporting
files.  The loaders below rebuild the remaining in-memory views (supporting
results, discovery fields, revision plans) from their existing reports so every
tab shows the loaded job.

This module is Qt-free so the persistence rules can be unit-tested.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
import json
from typing import Any, Optional

from models import DrawingGroup, SourceFile
from runtime.atomic_io import atomic_write_json


SCAN_STATE_SCHEMA = "GNI-Scan-State-1.0"
SCAN_STATE_FILE = "scan_state.json"


@dataclass
class ScanState:
    """Everything ``Main`` needs to reconstruct a session from disk."""

    source_root: str
    destination_root: str
    bundle_name: str
    mapping: dict[str, str] = field(default_factory=dict)
    files: list[SourceFile] = field(default_factory=list)
    groups: list[DrawingGroup] = field(default_factory=list)
    supporting: list[SourceFile] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Scan-state persistence
# ---------------------------------------------------------------------------


def _file_from_dict(data: dict[str, Any]) -> Optional[SourceFile]:
    if not isinstance(data, dict):
        return None
    keys = SourceFile.__dataclass_fields__
    return SourceFile(**{k: data.get(k) for k in keys})


def _group_from_dict(data: dict[str, Any]) -> Optional[DrawingGroup]:
    if not isinstance(data, dict):
        return None
    keys = DrawingGroup.__dataclass_fields__
    kwargs = {k: data.get(k) for k in keys}
    kwargs["candidates"] = [
        candidate
        for candidate in (_file_from_dict(item) for item in data.get("candidates", []))
        if candidate is not None
    ]
    return DrawingGroup(**kwargs)


def write_scan_state(
    reports_root,
    *,
    source_root,
    destination_root,
    bundle_name,
    mapping,
    files,
    groups,
    supporting,
):
    """Atomically persist the scan result into ``_GNI_REPORTS/scan_state.json``."""
    reports = Path(reports_root)
    reports.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": SCAN_STATE_SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(),
        "source_root": str(source_root),
        "destination_root": str(destination_root),
        "bundle_name": str(bundle_name),
        "mapping": dict(mapping or {}),
        "files": [asdict(item) for item in files],
        "groups": [asdict(group) for group in groups],
        "supporting": [asdict(item) for item in supporting],
    }
    return atomic_write_json(reports / SCAN_STATE_FILE, payload, indent=2)


def load_scan_state(reports_root) -> Optional[ScanState]:
    """Read ``scan_state.json`` back, or ``None`` when absent/corrupt/mismatched."""
    path = Path(reports_root) / SCAN_STATE_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != SCAN_STATE_SCHEMA:
        return None
    files = [
        item
        for item in (_file_from_dict(record) for record in payload.get("files", []))
        if item is not None
    ]
    groups = [
        item
        for item in (_group_from_dict(record) for record in payload.get("groups", []))
        if item is not None
    ]
    supporting = [
        item
        for item in (_file_from_dict(record) for record in payload.get("supporting", []))
        if item is not None
    ]
    mapping = payload.get("mapping")
    if not isinstance(mapping, dict):
        mapping = {}
    return ScanState(
        source_root=str(payload.get("source_root", "")),
        destination_root=str(payload.get("destination_root", "")),
        bundle_name=str(payload.get("bundle_name", "")),
        mapping=dict(mapping),
        files=files,
        groups=groups,
        supporting=supporting,
    )


def resolve_bundle_root(selected) -> Optional[Path]:
    """The bundle folder for a picked path, whether it is the bundle or its report dir."""
    path = Path(selected)
    if not path.is_dir():
        return None
    if path.name == "_GNI_REPORTS":
        return path.parent
    if (path / "_GNI_REPORTS").is_dir():
        return path
    return None


def clone_should_be_skipped(reports_root, supporting) -> bool:
    """True when clone left no report because there was nothing to clone.

    ``clone`` is genuinely optional: a bundle with no XLS/NC1/DXF files produces
    no ``supporting_file_clone_report.json``, and the live pipeline records that
    in the runtime ``_skipped`` set.  A reloaded session has to derive the same
    skip from disk so ``next_stage()`` does not stall on clone.
    """
    if (Path(reports_root) / "supporting_file_clone_report.json").is_file():
        return False
    return not any(getattr(item, "selected", False) for item in (supporting or []))


# ---------------------------------------------------------------------------
# In-memory view reconstruction from the existing handoff reports
# ---------------------------------------------------------------------------


def _read_json(path) -> Optional[dict]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def load_supporting_results(reports_root):
    from supporting.clone_engine import SupportingCloneResult

    payload = _read_json(Path(reports_root) / "supporting_file_clone_report.json")
    if not payload:
        return []
    keys = SupportingCloneResult.__dataclass_fields__
    return [
        SupportingCloneResult(**{k: item.get(k) for k in keys})
        for item in payload.get("results", [])
        if isinstance(item, dict)
    ]


def load_discovery_results(reports_root):
    from titleblock.discovery import DiscoveryField, DiscoveryResult

    payload = _read_json(Path(reports_root) / "title_block_discovery_report.json")
    if not payload:
        return []
    field_keys = DiscoveryField.__dataclass_fields__
    out = []
    for item in payload.get("results", []):
        if not isinstance(item, dict):
            continue
        fields = [
            DiscoveryField(**{k: record.get(k) for k in field_keys})
            for record in item.get("fields", [])
            if isinstance(record, dict)
        ]
        out.append(
            DiscoveryResult(
                identity=str(item.get("identity", "")),
                drawing_type=str(item.get("drawing_type", "")),
                source_path=str(item.get("source_path", "")),
                status=str(item.get("status", "")),
                job_folder=str(item.get("job_folder", "")),
                fields=fields,
            )
        )
    return out


def load_revision_plans(reports_root):
    from revision.write_plan import RevisionPlan

    payload = _read_json(Path(reports_root) / "revision_pilot_write_plan.json")
    if not payload:
        return []
    keys = RevisionPlan.__dataclass_fields__
    plans = []
    for item in payload.get("plans", []):
        if not isinstance(item, dict):
            continue
        kwargs = {k: item.get(k) for k in keys}
        for list_field in ("actions", "spatial_rows", "warnings", "errors"):
            if not isinstance(kwargs.get(list_field), list):
                kwargs[list_field] = []
        try:
            plans.append(RevisionPlan(**kwargs))
        except TypeError:
            continue
    return plans
