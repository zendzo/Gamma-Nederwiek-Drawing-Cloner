# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the portable (no-install) GammaNederwiek build.

Build it with ``packaging/build_portable.ps1``. That script supplies the version
resource, runs the test gate, signs the executable, hashes it and zips it - do
not call ``pyinstaller`` by hand, an unsigned/unversioned build is not shippable.

Layout of the result::

    GammaNederwiek/
        GammaNederwiek.exe        <- the only file that is signed
        _internal/                <- Python 3.12 + Qt 6 + resources/lisp/revision
        config/                   <- created on first run (portable state)
        logs/                     <- created on first run (support artifact)

``runtime/paths.py`` resolves ``app_root()`` to ``_internal`` at runtime, which
is why the data folders listed below are staged there and not next to the exe.
"""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent

# Read-only application data. `runtime/paths.app_root()` is `_MEIPASS`
# (`_internal`) in a frozen build, so these must live inside the bundle.
datas = [
    (str(ROOT / "resources"), "resources"),
    (str(ROOT / "lisp"), "lisp"),
    (str(ROOT / "VERSION"), "."),
]
datas += [
    (str(path), "revision") for path in sorted((ROOT / "revision").glob("*.json"))
]

# Qt ships far more than a widget app needs (the untouched PySide6 tree is
# ~680 MB). Everything below is unused by this UI; excluding it is what brings
# the folder down to roughly 200 MB.
EXCLUDES = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtSpatialAudio",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtPrintSupport",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtSerialPort",
    "PySide6.QtSensors",
    "PySide6.QtWebSockets",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtStateMachine",
    "PySide6.QtTextToSpeech",
    "PySide6.QtUiTools",
    "PySide6.QtHelp",
    "PySide6.QtDesigner",
    # Standard-library and tooling modules no runtime path needs.
    "tkinter",
    "unittest",
    "doctest",
    "pydoc",
    "pydoc_data",
    "test",
    "distutils",
    "setuptools",
    "pkg_resources",
    "pip",
    "PyInstaller",
]

version_file = ROOT / "packaging" / "version_info.txt"

a = Analysis(
    [str(ROOT / "app_modern.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe_options = dict(
    exclude_binaries=True,
    name="GammaNederwiek",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "resources" / "app_icon.ico"),
)
if version_file.is_file():
    exe_options["version"] = str(version_file)

exe = EXE(pyz, a.scripts, [], **exe_options)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="GammaNederwiek",
)
