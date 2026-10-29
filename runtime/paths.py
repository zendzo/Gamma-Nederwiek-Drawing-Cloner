"""Filesystem roots that keep a frozen build portable.

A source checkout and a PyInstaller build resolve their files differently, and a
frozen build must never assume the folder holding the executable is writable
(``Program Files``, a read-only share, or a locked-down profile).

``app_root``
    Read-only application data: ``resources/``, ``lisp/``, ``revision/*.json``,
    ``VERSION``. In a frozen build this is the PyInstaller runtime folder
    (``_internal``); in a checkout it is the repository root.

``data_root``
    Writable state: ``config/settings.json`` and ``logs/``. A portable copy
    prefers the folder next to the executable so the settings travel with it,
    and falls back to ``%LOCALAPPDATA%\\GammaNederwiek`` when that folder cannot
    be written. ``GNI_DATA_DIR`` overrides both.

Nothing here imports Qt, so it stays importable from the data layer and tests.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "APP_DIR_NAME",
    "app_root",
    "data_root",
    "fallback_settings_path",
    "is_frozen",
    "log_dir",
    "reset_cache",
    "settings_path",
    "version",
]

APP_DIR_NAME = "GammaNederwiek"
DEFAULT_VERSION = "0.4C.17"
_PROBE_NAME = ".gni_write_test"

_cache: dict[str, Path] = {}


def reset_cache() -> None:
    """Drop memoised roots; tests and ``GNI_DATA_DIR`` changes need this."""
    _cache.clear()


def is_frozen() -> bool:
    """True inside a PyInstaller bundle (``onedir`` or ``onefile``)."""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """Read-only application root (``resources/``, ``lisp/``, ``VERSION``)."""
    cached = _cache.get("app_root")
    if cached is not None:
        return cached
    if is_frozen():
        bundled = getattr(sys, "_MEIPASS", None)
        root = Path(bundled) if bundled else Path(sys.executable).resolve().parent
    else:
        root = Path(__file__).resolve().parent.parent
    root = root.resolve()
    _cache["app_root"] = root
    return root


def _writable(candidate: Path) -> bool:
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / _PROBE_NAME
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def data_root() -> Path:
    """Writable state root; never a read-only application folder."""
    cached = _cache.get("data_root")
    if cached is not None:
        return cached

    override = (os.environ.get("GNI_DATA_DIR") or "").strip()
    if override:
        candidate = Path(override).expanduser()
        if _writable(candidate):
            _cache["data_root"] = candidate.resolve()
            return _cache["data_root"]

    portable = (
        Path(sys.executable).resolve().parent if is_frozen() else app_root()
    )
    if _writable(portable):
        _cache["data_root"] = portable
        return portable

    fallback = _local_appdata() / APP_DIR_NAME
    fallback.mkdir(parents=True, exist_ok=True)
    _cache["data_root"] = fallback.resolve()
    return _cache["data_root"]


def _local_appdata() -> Path:
    raw = (os.environ.get("LOCALAPPDATA") or "").strip()
    if raw:
        return Path(raw)
    return Path.home() / "AppData" / "Local"


def settings_path() -> Path:
    """Where the live settings file lives (or should be written).

    A stale ``%LOCALAPPDATA%`` copy is honoured when the portable folder has no
    settings yet, so an app moved to a read-only location keeps its state.
    """
    primary = data_root() / "config" / "settings.json"
    if primary.is_file():
        return primary
    fallback = fallback_settings_path()
    return fallback if fallback.is_file() else primary


def fallback_settings_path() -> Path:
    """Last-resort settings location when the app folder is read-only."""
    return _local_appdata() / APP_DIR_NAME / "config" / "settings.json"


def log_dir() -> Path:
    return data_root() / "logs"


def version() -> str:
    """Build label from the bundled ``VERSION`` file, else the code default."""
    cached = _cache.get("version")
    if cached is not None:
        return str(cached)
    candidates = [app_root() / "VERSION"]
    if not is_frozen():
        candidates.append(app_root().parent / "VERSION")
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            _cache["version"] = text
            return text
    _cache["version"] = DEFAULT_VERSION
    return DEFAULT_VERSION
