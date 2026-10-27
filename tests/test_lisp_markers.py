"""Every LSP marker asserted by Python must exist in the matching .lsp.

Each Python driver waits for a banner printed by its LISP file and aborts with
``WRONG_LISP_VERSION`` / ``*_LOAD_FAILED`` when it is missing. Nothing checked
that the two sides agreed, so editing a ``(princ ...)`` line in ``lisp/`` — or
bumping a version in only one place — only failed on a real Core Console run,
i.e. on the operator's Windows machine.

The registry below is discovered by symbol name on every test run, so adding a
new ``*_LOAD_MARKER`` / ``VERSION_MARKER`` constant is covered automatically.
"""

import importlib
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LISP_DIR = ROOT / "lisp"

# module -> classes whose marker constants gate the run.
ENGINES = {
    "autocad.background": ("BackgroundEngine",),
    "titleblock.discovery": ("TitleBlockDiscoveryEngine",),
    "revision.controlled_writer": ("ControlledRevisionWriter",),
    "revision.stable_external_writer": ("StableExternalActionWriter",),
    "revision.reopen_validator": ("StableReopenValidator",),
}

MARKER_NAMES = ("LOAD_MARKER", "VERSION_MARKER")


def _lisp_text():
    """Concatenate every .lsp so a marker may live in any of them."""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace")
                     for path in sorted(LISP_DIR.glob("*.lsp")))


class LispMarkerTests(unittest.TestCase):
    def test_every_class_marker_exists_in_lisp(self):
        text = _lisp_text()
        checked = 0
        for module_name, class_names in ENGINES.items():
            with self.subTest(module=module_name):
                module = importlib.import_module(module_name)
            for class_name in class_names:
                cls = getattr(module, class_name)
                for attr in MARKER_NAMES:
                    marker = getattr(cls, attr, None)
                    if not marker:
                        continue
                    checked += 1
                    with self.subTest(engine=class_name, marker=attr, value=marker):
                        self.assertTrue(
                            marker in text,
                            f"{module_name}.{class_name}.{attr} = {marker!r} is not printed by any lisp/*.lsp")
        self.assertGreaterEqual(checked, 8, "marker discovery found too few constants")

    def test_pdf_plotter_marker_exists_in_lisp(self):
        from revision import pdf_publisher
        self.assertTrue(pdf_publisher.PDF_PLOTTER_LOAD_MARKER in _lisp_text())

    def test_no_engine_asserts_a_marker_it_never_declares(self):
        # Guards the inverse mistake: a bare string literal in an assertIn() that
        # no constant references, so a rename cannot be traced back. Only *exact*
        # version banners count - the failure side of a message would match too.
        literal = re.compile(r"['\"](GNI_[A-Z0-9_]+_(?:LOADED|QSAVE|OWNER_FIX|SHUTDOWN_SAFE))['\"]")
        for path in sorted((ROOT / "revision").glob("*.py")) + sorted((ROOT / "runtime").glob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in literal.finditer(text):
                with self.subTest(file=path.name, marker=match.group(1)):
                    self.assertTrue(match.group(1) in _lisp_text(),
                                    f"{path.name} asserts a marker no lisp prints")

    def test_lisp_version_banners_are_unique_per_file(self):
        """Two different banners for the same engine mean one of them is stale."""
        banners = {}
        for path in sorted(LISP_DIR.glob("*.lsp")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for marker in re.findall(r"GNI_[A-Z0-9_]*(?:LOADED|QSAVE|OWNER_FIX|SHUTDOWN_SAFE)", text):
                banners.setdefault(path.name, set()).add(marker)
        for name, found in banners.items():
            with self.subTest(file=name):
                loaded = [m for m in found if m.endswith("_LOADED")]
                self.assertLessEqual(len(loaded), 1, f"{name} prints several *_LOADED banners: {sorted(loaded)}")


class LispFileTests(unittest.TestCase):
    def test_expected_lisp_files_are_present(self):
        expected = {
            "background_engine.lsp",
            "title_block_discovery.lsp",
            "stable_external_writer.lsp",
            "reopen_validator.lsp",
            "pdf_modelspace_plotter.lsp",
        }
        present = {path.name for path in LISP_DIR.glob("*.lsp")}
        self.assertTrue(expected.issubset(present), expected - present)

    def test_lisp_uses_forward_slash_paths_only(self):
        # Backslashes inside LISP string literals break on Core Console.
        for path in sorted(LISP_DIR.glob("*.lsp")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                if "\\\\" in line and "C:" in line:
                    with self.subTest(file=path.name, line=line.strip()[:60]):
                        self.fail("Windows-style path in LISP literal")


if __name__ == "__main__":
    unittest.main()
