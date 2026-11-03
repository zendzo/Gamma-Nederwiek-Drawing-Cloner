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
import os

from runtime.winpath import long_path

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


def _iter_files(root):
    """Yield ``(relative_path, io_path)`` for every file under *root*.

    ``Path.rglob`` stops at the legacy MAX_PATH boundary on Windows. The
    General Cloner must read the production Gamma tree, whose paths are far
    longer than 260 characters, so the walk is done with ``os.scandir`` on the
    extended-length root. ``relative_path`` stays a normal path for reports and
    destination construction; ``io_path`` is only used for filesystem calls.
    """
    stack = [(long_path(root), Path())]
    while stack:
        directory, relative_dir = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    relative = relative_dir / entry.name
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append((Path(entry.path), relative))
                        elif entry.is_file(follow_symlinks=False):
                            yield relative, Path(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue


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
    for relative, io_path in sorted(
        _iter_files(root), key=lambda item: item[0].as_posix().casefold()
    ):
        if not is_dwg(relative):
            continue
        try:
            stat = long_path(io_path).stat()
        except OSError:
            continue
        source_path = root / relative
        drawing = GeneralDrawing(
            source_path=str(source_path),
            relative_path=relative.as_posix(),
            filename=relative.name,
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
