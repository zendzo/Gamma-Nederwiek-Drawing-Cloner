"""Locate ``accoreconsole.exe`` so a new machine needs no manual browsing.

A portable copy is dropped on a machine where nothing has been configured yet.
Rather than making the operator hunt through ``C:\\Program Files\\Autodesk``,
read the AutoCAD install locations that Autodesk already records in the
registry. Every lookup is best effort: an empty string simply leaves the field
empty and the operator can still pick the file by hand.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["detect"]

_EXE_NAME = "accoreconsole.exe"


def detect() -> str:
    """Return a path to ``accoreconsole.exe`` or ``''`` when unknown."""
    if os.name != "nt":
        return ""
    for candidate in _from_registry():
        if candidate.is_file():
            return str(candidate)
    for candidate in _from_program_files():
        if candidate.is_file():
            return str(candidate)
    return ""


def _from_registry() -> list[Path]:
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows
        return []

    found: list[tuple[str, Path]] = []
    access = winreg.KEY_READ
    access |= getattr(winreg, "KEY_WOW64_64KEY", 0)
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Autodesk\AutoCAD", 0, access)
    except OSError:
        return []

    with root:
        for release in _subkeys(root):
            try:
                release_key = winreg.OpenKey(root, release, 0, access)
            except OSError:
                continue
            with release_key:
                for product in _subkeys(release_key):
                    try:
                        product_key = winreg.OpenKey(release_key, product, 0, access)
                    except OSError:
                        continue
                    with product_key:
                        try:
                            location, _ = winreg.QueryValueEx(product_key, "AcadLocation")
                        except OSError:
                            continue
                    exe = Path(str(location)) / _EXE_NAME
                    if exe.is_file():
                        found.append((release, exe))
    # Newest release first; the tuple order keeps ties stable.
    found.sort(key=lambda item: item[0], reverse=True)
    return [exe for _, exe in found]


def _subkeys(key) -> list[str]:
    import winreg

    names: list[str] = []
    index = 0
    while True:
        try:
            names.append(winreg.EnumKey(key, index))
        except OSError:
            break
        index += 1
    return names


def _from_program_files() -> list[Path]:
    """Fallback sweep for installs that skipped the registry write."""
    roots = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ]
    candidates: list[Path] = []
    for raw in roots:
        if not raw:
            continue
        autodesk = Path(raw) / "Autodesk"
        if not autodesk.is_dir():
            continue
        try:
            versions = sorted(
                (item for item in autodesk.iterdir() if item.is_dir()),
                key=lambda item: item.name,
                reverse=True,
            )
        except OSError:
            continue
        candidates.extend(version / _EXE_NAME for version in versions)
    return candidates
