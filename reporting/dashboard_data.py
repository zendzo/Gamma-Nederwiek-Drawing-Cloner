"""Read-only view model for the Reports dashboard.

The JSON handoff reports in ``<_GNI_REPORTS>`` are the single source of truth.
This module never writes, never imports Qt, and never decides whether the
pipeline may advance -- it only derives display values from the same files the
controller already trusts.  Keeping it Qt-free means the aggregation rules can
be unit-tested without starting a GUI or AutoCAD.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import json
from typing import Any, Iterable


# Keep this order in sync with app.PIPELINE.  tests/test_report_dashboard.py
# asserts the labels and file names match the controller, so a rename cannot
# silently leave the dashboard pointing at the wrong report.
STAGES: tuple[tuple[str, str, str | None], ...] = (
    ("scan", "Scan source", None),
    ("destination", "Build destination", "destination_manifest.json"),
    ("clone", "Clone supporting files", "supporting_file_clone_report.json"),
    ("discovery", "Discover title blocks", "title_block_discovery_report.json"),
    ("plan", "Build revision plan", "revision_pilot_write_plan.json"),
    ("writer", "Stable external writer", "stable_external_writer_report.json"),
    ("validator", "Reopen validator", "reopen_validation_report.json"),
    ("dryrun", "Atomic publish dry run", "atomic_publish_dry_run_report.json"),
    ("publish", "Atomic publisher", "atomic_publish_report.json"),
    ("pdf", "Publish PDFs", "pdf_publish_report.json"),
)

STAGE_FILES: dict[str, str] = {
    key: name for key, _label, name in STAGES if name
}

# Clone is genuinely optional: a bundle with no XLS/NC1/DXF files produces no
# report and the controller marks the stage skipped.  It must not look broken.
OPTIONAL_STAGES = frozenset({"clone"})

# Per-drawing collections inside each report.  The publisher names its list
# ``transactions`` while every other engine uses ``results``/``plans``.
DRAWING_SOURCES: tuple[tuple[str, str], ...] = (
    ("discovery", "results"),
    ("plan", "plans"),
    ("writer", "results"),
    ("validator", "results"),
    ("dryrun", "results"),
    ("publish", "transactions"),
    ("pdf", "results"),
)

# Compact labels for the status matrix.  Raw engine statuses are deliberately
# verbose (LOCAL_PAIR_WRITTEN_NOT_VALIDATED) and would force horizontal scroll.
FRIENDLY_STATUS: dict[str, str] = {
    "SUCCESS": "OK",
    "FAILED": "FAILED",
    "READY": "READY",
    "READY_WITH_WARNINGS": "READY (WARN)",
    "BLOCKED": "BLOCKED",
    "LOCAL_PAIR_WRITTEN_NOT_VALIDATED": "WRITTEN",
    "VALIDATED": "VALIDATED",
    "DRY_RUN_READY_TO_PUBLISH": "READY",
    "DRY_RUN_BLOCKED": "BLOCKED",
    "PUBLISHED": "PUBLISHED",
    "PUBLISH_FAILED_ROLLED_BACK": "ROLLED BACK",
    "PUBLISH_FAILED_DESTINATION_UNCHANGED": "FAILED",
    "CRITICAL_ROLLBACK_FAILED": "CRITICAL",
    "PDF_PAIR_PUBLISHED": "PUBLISHED",
}

STATE_TONES: dict[str, str] = {
    "COMPLETE": "ok",
    "OPTIONAL": "idle",
    "PENDING": "idle",
    "INCOMPLETE": "warn",
    "STALE": "warn",
    "UNREADABLE": "bad",
}

MAX_STAGE_ISSUES = 24
MAX_SNAPSHOT_ISSUES = 80


@dataclass(frozen=True)
class Kpi:
    """One headline number on the dashboard."""

    label: str
    value: str
    caption: str = ""
    tone: str = "info"


@dataclass(frozen=True)
class StageInfo:
    """Everything the dashboard needs to draw one pipeline stage."""

    key: str
    label: str
    filename: str = ""
    state: str = "PENDING"
    tone: str = "idle"
    headline: str = ""
    detail: str = ""
    percent: int = 0
    generated_at: str = ""
    path: Path | None = None
    issues: tuple[str, ...] = ()
    issue_count: int = 0


@dataclass(frozen=True)
class DrawingStatus:
    """One drawing's status across the stages that produce per-pair results."""

    identity: str
    drawing_type: str = ""
    statuses: dict[str, str] = field(default_factory=dict)
    issues: tuple[str, ...] = ()

    def matches(self, query: str) -> bool:
        query = query.strip().lower()
        if not query:
            return True
        haystack = " ".join(
            [self.identity, self.drawing_type, *self.statuses.values(), *self.issues]
        ).lower()
        return query in haystack


@dataclass(frozen=True)
class Snapshot:
    """A complete, consistent picture of the reports folder at one moment."""

    root: Path
    bundle: str = ""
    verdict: str = "NOT STARTED"
    verdict_tone: str = "idle"
    verdict_detail: str = ""
    completed: int = 0
    total: int = 0
    percent: int = 0
    last_updated: str = ""
    kpis: tuple[Kpi, ...] = ()
    stages: tuple[StageInfo, ...] = ()
    drawings: tuple[DrawingStatus, ...] = ()
    issues: tuple[str, ...] = ()
    issue_count: int = 0
    evidence_count: int = 0


@dataclass(frozen=True)
class _Metrics:
    headline: str
    detail: str = ""
    percent: int | None = None
    issues: tuple[str, ...] = ()


@dataclass
class _Loaded:
    payload: dict[str, Any] | None = None
    path: Path | None = None
    stale: bool = False
    unreadable: bool = False


# A tiny mtime/size cache: switching pages must not re-parse nine JSON files
# whose bytes have not changed.  The dashboard is single-threaded; no lock.
_JSON_CACHE: dict[str, tuple[int, int, dict[str, Any]]] = {}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    key = str(path)
    cached = _JSON_CACHE.get(key)
    if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if len(_JSON_CACHE) > 64:
        _JSON_CACHE.clear()
    _JSON_CACHE[key] = (stat.st_mtime_ns, stat.st_size, payload)
    return payload


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _percent(done: int, total: int) -> int | None:
    if total <= 0:
        return None
    return max(0, min(100, int(round(done * 100.0 / total))))


def _summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    return _as_dict(_as_dict(payload).get("summary"))


def _limit(values: Iterable[str], limit: int = MAX_STAGE_ISSUES) -> tuple[str, ...]:
    items = [str(value).strip() for value in values if str(value).strip()]
    return tuple(items[:limit])


def _label(value: str) -> str:
    value = _text(value)
    if not value:
        return ""
    return FRIENDLY_STATUS.get(value, value.replace("_", " "))


def status_tone(text: str) -> str:
    """Map a raw engine status to ok/warn/bad/idle for the matrix."""

    upper = text.upper()
    if any(
        token in upper
        for token in ("FAILED", "BLOCKED", "CRITICAL", "ROLLED", "UNREADABLE",
                      "ERROR", "MISSING", "REJECT")
    ):
        return "bad"
    if any(token in upper for token in ("WARN", "STALE", "INCOMPLETE", "CANCEL")):
        return "warn"
    if any(
        token in upper
        for token in ("READY", "VALIDATED", "PUBLISHED", "WRITTEN", "SUCCESS",
                      "OK", "COMPLETE", "UNCHANGED")
    ):
        return "ok"
    return "idle"


def _format_timestamp(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text


def _natural_key(value: str) -> tuple[Any, ...]:
    """Sort DRAW-2 before DRAW-10 without pulling in a dependency."""

    parts: list[Any] = []
    number = ""
    for char in value:
        if char.isdigit():
            number += char
        else:
            if number:
                parts.append((0, int(number)))
                number = ""
            parts.append((1, char.lower()))
    if number:
        parts.append((0, int(number)))
    return tuple(parts)


# ---------------------------------------------------------------------------
# Per-stage metrics
# ---------------------------------------------------------------------------


def _destination_metrics(payload: dict[str, Any]) -> _Metrics:
    result = _as_dict(payload.get("result"))
    required = [str(item) for item in _as_list(payload.get("required_folders"))]
    created = [str(item) for item in _as_list(result.get("created_folders"))]
    existing = [str(item) for item in _as_list(result.get("existing_folders"))]
    assets = _as_list(result.get("copied_assets"))
    warnings = [str(item) for item in _as_list(result.get("warnings"))]
    status = _text(result.get("status"))

    folders = len(set(created) | set(existing))
    total = len(required) or folders
    headline = f"{folders}/{total} folders ready" if total else "Destination ready"
    detail = f"{len(created)} created · {len(existing)} already present"
    if assets:
        detail += f" · {len(assets)} reference assets placed"
    if status and status != "SUCCESS":
        warnings.insert(0, status)
    return _Metrics(
        headline=headline,
        detail=detail,
        percent=_percent(folders, total),
        issues=_limit(warnings),
    )


def _clone_metrics(payload: dict[str, Any]) -> _Metrics:
    summary = _summary(payload)
    total = _as_int(summary.get("total"))
    success = _as_int(summary.get("success"))
    skipped = _as_int(summary.get("skipped"))
    failed = _as_int(summary.get("failed"))
    part_list = _as_int(summary.get("part_list"))
    nc1 = _as_int(summary.get("nc1"))
    dxf = _as_int(summary.get("dxf"))

    issues = [
        f"{_text(item.get('destination_path') or item.get('source_path')) or 'file'}: "
        f"{_text(item.get('error')) or _text(item.get('status'))}"
        for item in _as_list(payload.get("results"))
        if _text(item.get("status")) == "FAILED"
    ]
    if failed and not issues:
        issues.append(f"{failed} file(s) failed to clone")

    if not total:
        return _Metrics(
            headline="No supporting files",
            detail="Nothing selected for cloning in this job",
            percent=None,
            issues=_limit(issues),
        )
    return _Metrics(
        headline=f"{success}/{total} files cloned",
        detail=f"{skipped} skipped · {failed} failed · "
               f"{part_list} part list · {nc1} NC1 · {dxf} DXF",
        percent=_percent(success, total),
        issues=_limit(issues),
    )


def _discovery_metrics(payload: dict[str, Any]) -> _Metrics:
    summary = _summary(payload)
    drawings = _as_int(summary.get("drawings"))
    success = _as_int(summary.get("success"))
    failed = _as_int(summary.get("failed"))
    fields = _as_int(summary.get("fields"))

    issues = [
        f"{_text(item.get('identity')) or 'drawing'}: "
        f"{_text(item.get('error')) or _text(item.get('status'))}"
        for item in _as_list(payload.get("results"))
        if _text(item.get("status")) == "FAILED"
    ]
    if failed and not issues:
        issues.append(f"{failed} drawing(s) could not be read")
    return _Metrics(
        headline=f"{success}/{drawings} drawings discovered" if drawings
                 else "No drawings discovered",
        detail=f"{fields} title-block fields captured · {failed} failed",
        percent=_percent(success, drawings),
        issues=_limit(issues),
    )


def _plan_metrics(payload: dict[str, Any]) -> _Metrics:
    summary = _summary(payload)
    plans = _as_int(summary.get("plans"))
    ready = _as_int(summary.get("ready"))
    blocked = _as_int(summary.get("blocked"))
    actions = _as_int(summary.get("actions"))
    rows = _as_int(summary.get("spatial_rows"))

    issues = []
    for plan in _as_list(payload.get("plans")):
        status = _text(plan.get("status"))
        if status.startswith("READY"):
            continue
        errors = [_text(item) for item in _as_list(plan.get("errors")) if _text(item)]
        issues.append(
            f"{_text(plan.get('identity')) or 'plan'}: "
            f"{status or 'BLOCKED'}"
            + (f" — {', '.join(errors[:2])}" if errors else "")
        )
    if blocked and not issues:
        issues.append(f"{blocked} plan(s) blocked")
    return _Metrics(
        headline=f"{ready}/{plans} plans ready" if plans else "No plans built",
        detail=f"{actions} actions · {rows} spatial rows · {blocked} blocked",
        percent=_percent(ready, plans),
        issues=_limit(issues),
    )


def _writer_metrics(payload: dict[str, Any]) -> _Metrics:
    summary = _summary(payload)
    plans = _as_int(summary.get("plans"))
    pairs = _as_int(summary.get("local_pairs"))
    failed = _as_int(summary.get("failed"))

    verified = 0
    issues = []
    for result in _as_list(payload.get("results")):
        for output in _as_list(result.get("outputs")):
            verified += _as_int(output.get("actions_verified"))
        if _text(result.get("status")) != "FAILED":
            continue
        errors = [_text(item) for item in _as_list(result.get("errors")) if _text(item)]
        issues.append(
            f"{_text(result.get('identity')) or 'pair'}: "
            + (", ".join(errors[:2]) if errors else "writer failed")
        )
    if failed and not issues:
        issues.append(f"{failed} pair(s) failed to write")
    return _Metrics(
        headline=f"{pairs}/{plans} pairs written" if plans else "No pairs written",
        detail=f"{failed} failed · {verified} actions verified",
        percent=_percent(pairs, plans),
        issues=_limit(issues),
    )


def _validator_metrics(payload: dict[str, Any]) -> _Metrics:
    summary = _summary(payload)
    pairs = _as_int(summary.get("pairs"))
    validated = _as_int(summary.get("validated"))
    failed = _as_int(summary.get("failed"))

    checks = 0
    issues = []
    for result in _as_list(payload.get("results")):
        for output in _as_list(result.get("outputs")):
            checks += _as_int(output.get("checks_passed"))
        if _text(result.get("status")) == "VALIDATED":
            continue
        errors = [_text(item) for item in _as_list(result.get("errors")) if _text(item)]
        issues.append(
            f"{_text(result.get('identity')) or 'pair'}: "
            + (", ".join(errors[:2]) if errors else _text(result.get("status")) or "failed")
        )
    if failed and not issues:
        issues.append(f"{failed} pair(s) failed validation")
    return _Metrics(
        headline=f"{validated}/{pairs} pairs validated" if pairs else "No pairs validated",
        detail=f"{failed} failed · {checks} reopen checks passed",
        percent=_percent(validated, pairs),
        issues=_limit(issues),
    )


def _dry_run_metrics(payload: dict[str, Any]) -> _Metrics:
    summary = _summary(payload)
    pairs = _as_int(summary.get("pairs"))
    ready = _as_int(summary.get("ready"))
    blocked = _as_int(summary.get("blocked"))
    overwrites = _as_int(summary.get("dwg_overwrites"))
    backups = _as_int(summary.get("dwg_backups_required"))

    issues = []
    for result in _as_list(payload.get("results")):
        if _text(result.get("status")) == "DRY_RUN_READY_TO_PUBLISH":
            continue
        errors = [_text(item) for item in _as_list(result.get("errors")) if _text(item)]
        issues.append(
            f"{_text(result.get('identity')) or 'pair'}: "
            + (", ".join(errors[:2]) if errors else _text(result.get("status")) or "blocked")
        )
    if blocked and not issues:
        issues.append(f"{blocked} pair(s) blocked by the dry run")
    return _Metrics(
        headline=f"{ready}/{pairs} pairs ready to publish" if pairs
                 else "No publishable pairs",
        detail=f"{blocked} blocked · {overwrites} DWG overwrites · "
               f"{backups} backups required",
        percent=_percent(ready, pairs),
        issues=_limit(issues),
    )


def _publish_metrics(payload: dict[str, Any]) -> _Metrics:
    summary = _summary(payload)
    transactions = _as_int(summary.get("transactions"))
    published = _as_int(summary.get("published"))
    rolled_back = _as_int(summary.get("rolled_back"))
    unchanged_failures = _as_int(summary.get("failed_unchanged"))
    critical = _as_int(summary.get("critical"))

    issues = []
    for transaction in _as_list(payload.get("transactions")):
        if _text(transaction.get("status")) == "PUBLISHED":
            continue
        errors = [_text(item) for item in _as_list(transaction.get("errors")) if _text(item)]
        issues.append(
            f"{_text(transaction.get('identity')) or 'transaction'}: "
            + (", ".join(errors[:2]) if errors else _text(transaction.get("status")) or "failed")
        )
    if (rolled_back or unchanged_failures or critical) and not issues:
        issues.append(f"{rolled_back + unchanged_failures + critical} publish failure(s)")
    return _Metrics(
        headline=f"{published}/{transactions} transactions published" if transactions
                 else "Nothing published yet",
        detail=f"{rolled_back} rolled back · {unchanged_failures} unchanged failures · "
               f"{critical} critical",
        percent=_percent(published, transactions),
        issues=_limit(issues),
    )


def _pdf_metrics(payload: dict[str, Any]) -> _Metrics:
    summary = _summary(payload)
    pairs = _as_int(summary.get("pairs"))
    published = _as_int(summary.get("published"))
    unchanged = _as_int(summary.get("unchanged"))
    failed = _as_int(summary.get("failed"))

    issues = []
    for result in _as_list(payload.get("results")):
        if _text(result.get("status")) == "PDF_PAIR_PUBLISHED":
            continue
        errors = [_text(item) for item in _as_list(result.get("errors")) if _text(item)]
        issues.append(
            f"{_text(result.get('identity')) or 'pair'}: "
            + (", ".join(errors[:2]) if errors else _text(result.get("status")) or "failed")
        )
    if failed and not issues:
        issues.append(f"{failed} pair(s) failed to plot")
    return _Metrics(
        headline=f"{published}/{pairs} pairs plotted" if pairs else "No PDFs plotted",
        detail=f"{unchanged} unchanged · {failed} failed",
        percent=_percent(published, pairs),
        issues=_limit(issues),
    )


_METRIC_BUILDERS = {
    "destination": _destination_metrics,
    "clone": _clone_metrics,
    "discovery": _discovery_metrics,
    "plan": _plan_metrics,
    "writer": _writer_metrics,
    "validator": _validator_metrics,
    "dryrun": _dry_run_metrics,
    "publish": _publish_metrics,
    "pdf": _pdf_metrics,
}


# ---------------------------------------------------------------------------
# Snapshot assembly
# ---------------------------------------------------------------------------


def _load_stage_files(root: Path) -> dict[str, _Loaded]:
    loaded: dict[str, _Loaded] = {}
    if not root.is_dir():
        return loaded
    for key, _label, name in STAGES:
        if not name:
            continue
        path = root / name
        if path.is_file():
            payload = _read_json(path)
            loaded[key] = _Loaded(
                payload=payload,
                path=path,
                unreadable=payload is None,
            )
            continue
        stale_path = Path(str(path) + ".stale")
        if stale_path.is_file():
            payload = _read_json(stale_path)
            loaded[key] = _Loaded(
                payload=payload,
                path=stale_path,
                stale=True,
                unreadable=payload is None,
            )
    return loaded


def _stage_state(key: str, loaded: _Loaded | None) -> str:
    if loaded is None:
        return "OPTIONAL" if key in OPTIONAL_STAGES else "PENDING"
    if loaded.unreadable:
        return "UNREADABLE"
    if loaded.stale:
        return "STALE"
    if loaded.payload is not None and loaded.payload.get("complete") is False:
        return "INCOMPLETE"
    return "COMPLETE"


def _stage_info(
    key: str,
    label: str,
    filename: str | None,
    loaded: _Loaded | None,
    any_evidence: bool,
    discovery_drawings: int,
) -> StageInfo:
    if key == "scan":
        state = "COMPLETE" if any_evidence else "PENDING"
        return StageInfo(
            key=key,
            label=label,
            state=state,
            tone=STATE_TONES.get(state, "idle"),
            headline=f"{discovery_drawings} drawings" if discovery_drawings
                     else ("Source scanned" if any_evidence else "Awaiting scan"),
            detail="Recorded by the first stage report" if any_evidence
                   else "Run Scan source to begin",
            percent=100 if any_evidence else 0,
        )

    state = _stage_state(key, loaded)
    tone = STATE_TONES.get(state, "idle")
    issues: tuple[str, ...] = ()
    issue_count = 0
    metrics = _Metrics(headline="Not run yet", detail="No report in _GNI_REPORTS", percent=0)
    if loaded is None and key in OPTIONAL_STAGES:
        metrics = _Metrics(
            headline="No supporting files",
            detail="Optional stage — no clone report for this job",
            percent=0,
        )

    if loaded is not None and not loaded.unreadable and loaded.payload is not None:
        builder = _METRIC_BUILDERS[key]
        metrics = builder(loaded.payload)
        issues = metrics.issues
        issue_count = len(issues)
        if state == "INCOMPLETE":
            reason = _text(loaded.payload.get("cancel_reason")) or "cancelled before completion"
            issues = _limit((f"Report marked incomplete: {reason}", *issues))
            issue_count = max(issue_count, len(issues))
        if state == "STALE":
            issues = _limit(("Previous report was quarantined; run this stage again", *issues))
            issue_count = max(issue_count, len(issues))
    elif state == "UNREADABLE":
        issues = _limit((f"{filename or 'report'} is not valid JSON",))
        issue_count = len(issues)
        metrics = _Metrics(
            headline="Report unreadable",
            detail="The file exists but could not be parsed",
            percent=0,
        )

    percent = metrics.percent
    if percent is None:
        percent = 100 if state == "COMPLETE" else 0
    return StageInfo(
        key=key,
        label=label,
        filename=filename or "",
        state=state,
        tone=tone,
        headline=metrics.headline,
        detail=metrics.detail,
        percent=max(0, min(100, int(percent))),
        generated_at=_format_timestamp(
            loaded.payload.get("generated_at") if loaded and loaded.payload else ""
        ),
        path=loaded.path if loaded else None,
        issues=issues,
        issue_count=issue_count,
    )


def _build_drawings(
    loaded: dict[str, _Loaded],
) -> tuple[DrawingStatus, ...]:
    rows: dict[str, dict[str, Any]] = {}

    def row_for(identity: str) -> dict[str, Any]:
        return rows.setdefault(
            identity,
            {"drawing_type": "", "statuses": {}, "issues": []},
        )

    for stage_key, collection in DRAWING_SOURCES:
        item = loaded.get(stage_key)
        if not item or item.unreadable or not item.payload:
            continue
        for record in _as_list(item.payload.get(collection)):
            if not isinstance(record, dict):
                continue
            identity = _text(record.get("identity"))
            if not identity:
                continue
            row = row_for(identity)
            if not row["drawing_type"]:
                row["drawing_type"] = _text(record.get("drawing_type"))
            status = _text(record.get("status"))
            row["statuses"][stage_key] = _label(status) if status else "—"
            errors = [_text(value) for value in _as_list(record.get("errors")) if _text(value)]
            warnings = [_text(value) for value in _as_list(record.get("warnings")) if _text(value)]
            combined = errors or warnings
            if combined:
                prefix = stage_key.upper()
                row["issues"].extend(f"{prefix}: {value}" for value in combined[:2])

    drawings = [
        DrawingStatus(
            identity=identity,
            drawing_type=row["drawing_type"],
            statuses=dict(row["statuses"]),
            issues=_limit(row["issues"], 12),
        )
        for identity, row in rows.items()
    ]
    drawings.sort(key=lambda item: _natural_key(item.identity))
    return tuple(drawings)


def _extra_issues(root: Path) -> tuple[list[str], int]:
    issues: list[str] = []
    count = 0

    rejected = _read_json(root / "gate_rejected.json")
    if rejected:
        items = [item for item in _as_list(rejected.get("rejected")) if isinstance(item, dict)]
        count += _as_int(rejected.get("count"), len(items))
        for item in items[:12]:
            errors = [_text(error) for error in _as_list(item.get("errors")) if _text(error)]
            issues.append(
                f"Gate · {_text(item.get('identity')) or '?'}: "
                + (", ".join(errors[:3]) if errors else "rejected")
            )

    failures = _read_json(root / "failures.json")
    if failures:
        items = [item for item in _as_list(failures.get("failures")) if isinstance(item, dict)]
        count += _as_int(failures.get("count"), len(items))
        for item in items[:12]:
            errors = [_text(error) for error in _as_list(item.get("errors")) if _text(error)]
            issues.append(
                f"Failure · {_text(item.get('identity')) or '?'} "
                f"({_text(item.get('stage')) or 'pipeline'}): "
                + (", ".join(errors[:3]) if errors else _text(item.get("status")) or "failed")
            )

    return issues, count


def _kpis(
    loaded: dict[str, _Loaded],
    drawings: tuple[DrawingStatus, ...],
    issue_count: int,
) -> tuple[Kpi, ...]:
    discovery = _summary(loaded["discovery"].payload) if "discovery" in loaded else {}
    plan = _summary(loaded["plan"].payload) if "plan" in loaded else {}
    writer = _summary(loaded["writer"].payload) if "writer" in loaded else {}
    validator = _summary(loaded["validator"].payload) if "validator" in loaded else {}
    publish = _summary(loaded["publish"].payload) if "publish" in loaded else {}
    dryrun = _summary(loaded["dryrun"].payload) if "dryrun" in loaded else {}
    pdf = _summary(loaded["pdf"].payload) if "pdf" in loaded else {}

    drawings_count = _as_int(discovery.get("drawings")) or len(drawings)
    plans = _as_int(plan.get("plans"))
    ready = _as_int(plan.get("ready"))
    blocked = _as_int(plan.get("blocked"))
    pairs = _as_int(writer.get("local_pairs"))
    write_failed = _as_int(writer.get("failed"))
    validated = _as_int(validator.get("validated"))
    validation_failed = _as_int(validator.get("failed"))
    published = _as_int(publish.get("published"))
    rolled = _as_int(publish.get("rolled_back"))
    unchanged_failures = _as_int(publish.get("failed_unchanged"))
    critical = _as_int(publish.get("critical"))
    pdf_published = _as_int(pdf.get("published"))
    pdf_unchanged = _as_int(pdf.get("unchanged"))
    pdf_failed = _as_int(pdf.get("failed"))
    dry_ready = _as_int(dryrun.get("ready"))
    dry_blocked = _as_int(dryrun.get("blocked"))
    dry_backups = _as_int(dryrun.get("dwg_backups_required"))

    return (
        Kpi("Drawings", str(drawings_count),
            f"{_as_int(discovery.get('failed'))} failed discovery",
            "info" if drawings_count else "idle"),
        Kpi("Pairs planned", str(plans),
            f"{ready} ready · {blocked} blocked",
            "bad" if blocked else ("ok" if plans else "idle")),
        Kpi("Pairs written", str(pairs),
            f"{write_failed} failed",
            "bad" if write_failed else ("ok" if pairs else "idle")),
        Kpi("Pairs validated", str(validated),
            f"{validation_failed} failed",
            "bad" if validation_failed else ("ok" if validated else "idle")),
        Kpi("Dry run ready", str(dry_ready),
            f"{dry_blocked} blocked · {dry_backups} backups",
            "bad" if dry_blocked else ("ok" if dry_ready else "idle")),
        Kpi("Published DWG", str(published),
            f"{rolled} rolled back · {critical} critical",
            "bad" if (rolled or unchanged_failures or critical)
            else ("ok" if published else "idle")),
        Kpi("PDFs", str(pdf_published),
            f"{pdf_unchanged} unchanged · {pdf_failed} failed",
            "bad" if pdf_failed else ("ok" if pdf_published else "idle")),
        Kpi("Issues", str(issue_count),
            "blocking items across reports",
            "bad" if issue_count else "ok"),
    )


def _last_updated(root: Path) -> str:
    if not root.is_dir():
        return ""
    newest = 0.0
    try:
        for path in root.iterdir():
            if not path.is_file():
                continue
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        return ""
    if not newest:
        return ""
    return datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M")


def build_snapshot(reports_root: str | Path) -> Snapshot:
    """Read ``reports_root`` and return a display snapshot. Never raises."""

    root = Path(reports_root)
    loaded = _load_stage_files(root)
    any_evidence = any(item.payload is not None for item in loaded.values())
    discovery_summary = _summary(loaded["discovery"].payload) if "discovery" in loaded else {}
    discovery_drawings = _as_int(discovery_summary.get("drawings"))

    stages = tuple(
        _stage_info(
            key,
            label,
            filename,
            loaded.get(key),
            any_evidence,
            discovery_drawings,
        )
        for key, label, filename in STAGES
    )

    drawings = _build_drawings(loaded)
    extra_issues, extra_count = _extra_issues(root)
    issue_count = sum(stage.issue_count for stage in stages) + extra_count

    issue_list: list[str] = []
    for stage in stages:
        for issue in stage.issues:
            issue_list.append(f"{stage.label} · {issue}")
    issue_list.extend(extra_issues)
    issues = tuple(issue_list[:MAX_SNAPSHOT_ISSUES])

    total = len(stages)
    completed = sum(
        1
        for stage in stages
        if stage.state == "COMPLETE"
        or (stage.state == "OPTIONAL" and any_evidence)
    )
    percent = _percent(completed, total) or 0

    required = [stage for stage in stages if stage.key not in OPTIONAL_STAGES]
    required_complete = all(stage.state == "COMPLETE" for stage in required)
    pdf_complete = next(
        (stage.state == "COMPLETE" for stage in stages if stage.key == "pdf"), False
    )

    if not any_evidence and not issue_count:
        verdict = "NOT STARTED"
        verdict_tone = "idle"
        verdict_detail = "Run Scan source to create the first report"
    elif required_complete and pdf_complete and issue_count == 0:
        verdict = "COMPLETE"
        verdict_tone = "ok"
        verdict_detail = "All stages complete · no blocking issues"
    elif issue_count or any(
        stage.state in ("INCOMPLETE", "STALE", "UNREADABLE") for stage in stages
    ):
        verdict = "NEEDS ATTENTION"
        verdict_tone = "bad"
        verdict_detail = f"{completed}/{total} stages complete · {issue_count} issue(s) to review"
    else:
        verdict = "IN PROGRESS"
        verdict_tone = "info"
        pending = next(
            (
                stage.label
                for stage in stages
                if stage.state not in ("COMPLETE", "OPTIONAL")
            ),
            "Finish the pipeline",
        )
        verdict_detail = f"{completed}/{total} stages complete · next: {pending}"

    try:
        evidence_count = sum(1 for path in root.iterdir() if path.is_file())
    except OSError:
        evidence_count = 0

    bundle = root.parent.name if root.name == "_GNI_REPORTS" else root.name

    return Snapshot(
        root=root,
        bundle=bundle or "No bundle selected",
        verdict=verdict,
        verdict_tone=verdict_tone,
        verdict_detail=verdict_detail,
        completed=completed,
        total=total,
        percent=percent,
        last_updated=_last_updated(root),
        kpis=_kpis(loaded, drawings, issue_count),
        stages=stages,
        drawings=drawings,
        issues=issues,
        issue_count=issue_count,
        evidence_count=evidence_count,
    )
