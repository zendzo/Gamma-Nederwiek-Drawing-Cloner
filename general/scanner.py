"""Recursive, filename-agnostic DWG discovery for the General Cloner.

This module deliberately knows nothing about SP/AS/GA prefixes, revision
suffixes, drawing numbers, disciplines, or title blocks.  A file is a drawing
when its extension is ``.dwg`` (case-insensitive) and it lives under the chosen
source root.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List

DWG_EXTENSION = ".dwg"


@dataclass
class GeneralDrawing:
    """One discovered DWG and the destination path derived from its location."""

    source_path: str
    relative_path: str
    filename: str
    size_bytes: int
    modified_iso: str
    destination_path: str = ""
    # Set when two source files normalise to the same destination path (for
    # example a case-only difference on Windows).  The first one wins; the
    # duplicate is reported as SKIPPED instead of silently overwriting.
    duplicate_of: str = ""


def is_dwg(path) -> bool:
    """True for a DWG file, matching the extension case-insensitively."""
    return Path(path).suffix.lower() == DWG_EXTENSION


def scan_general_dwgs(source_root, bundle_root=None) -> List[GeneralDrawing]:
    """Return every DWG under *source_root*, in stable relative-path order.

    ``bundle_root`` is optional.  When supplied, each drawing's destination is
    ``bundle_root / relative_path`` so nested folders are preserved exactly.
    """
    root = Path(source_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Invalid source folder: " + str(source_root))

    bundle = Path(bundle_root).expanduser().resolve() if bundle_root else None
    drawings: List[GeneralDrawing] = []
    # Sorting the resolved paths keeps duplicate detection and report rows
    # deterministic even when the filesystem returns entries in another order.
    candidates = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix().casefold())
    for path in candidates:
        try:
            if not path.is_file() or not is_dwg(path):
                continue
            relative = path.relative_to(root)
        except (OSError, ValueError):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        drawing = GeneralDrawing(
            source_path=str(path),
            relative_path=relative.as_posix(),
            filename=path.name,
            size_bytes=int(stat.st_size),
            modified_iso=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        )
        if bundle is not None:
            drawing.destination_path = str(bundle / relative)
        drawings.append(drawing)

    # Windows and most network shares are case-insensitive.  Two source files
    # that differ only by case would otherwise map to one destination file and
    # one of them would be lost without a report entry.
    seen = {}
    for drawing in drawings:
        key = drawing.relative_path.casefold()
        if key in seen:
            drawing.duplicate_of = seen[key]
        else:
            seen[key] = drawing.relative_path
    return drawings
