"""Bundle Plus Jakarta Sans with the app instead of requiring an install.

The modern shell asks Qt for ``"Plus Jakarta Sans"`` in its stylesheet, but a
domain PC rarely has the font installed, so Qt silently fell back to Segoe UI.
Registering the TTFs from ``resources/fonts`` with
``QFontDatabase.addApplicationFont`` makes the family available to the running
process only - no admin rights, no system-wide install, nothing written to
``%WINDIR%\\Fonts``.

Static weights are shipped deliberately. Qt 6 maps a variable font to a single
weight, so the QSS ``font-weight: 600`` / ``700`` rules would collapse; the
static SemiBold and Bold files keep them distinct.

``OFL.txt`` must travel with the font: the SIL Open Font License requires the
licence to be redistributed alongside the font files.

Typography stays where it is defined - the modern QSS targets ``QLabel`` - so
this module registers the family and never calls ``QApplication.setFont``.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["FONT_FAMILY", "FONT_FILES", "font_dir", "install", "installed_families"]

FONT_FAMILY = "Plus Jakarta Sans"

#: Static weights, shipped as files rather than one variable font (see module docstring).
FONT_FILES = (
    "PlusJakartaSans-Regular.ttf",
    "PlusJakartaSans-Italic.ttf",
    "PlusJakartaSans-Medium.ttf",
    "PlusJakartaSans-SemiBold.ttf",
    "PlusJakartaSans-Bold.ttf",
)

_loaded: set[str] = set()
_font_ids: list[int] = []


def font_dir() -> Path:
    from runtime import paths

    return paths.app_root() / "resources" / "fonts"


def install(reset: bool = False) -> list[int]:
    """Register the bundled fonts once; safe to call from both entry points.

    Returns the Qt font ids that were accepted. Missing or malformed files are
    skipped so a broken asset degrades to the previous Segoe UI fallback
    instead of preventing the app from starting.
    """
    if reset:
        _loaded.clear()
        _font_ids.clear()

    from PySide6.QtGui import QFontDatabase

    directory = font_dir()
    for name in FONT_FILES:
        path = directory / name
        key = str(path)
        if key in _loaded:
            continue
        if not path.is_file():
            continue
        font_id = QFontDatabase.addApplicationFont(key)
        _loaded.add(key)
        if font_id >= 0:
            _font_ids.append(int(font_id))
    return list(_font_ids)


def installed_families() -> set[str]:
    """Family names currently registered from the bundle (empty before install)."""
    from PySide6.QtGui import QFontDatabase

    families: set[str] = set()
    for font_id in _font_ids:
        families.update(QFontDatabase.applicationFontFamilies(font_id))
    return families
