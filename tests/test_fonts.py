"""The bundled Plus Jakarta Sans must exist and actually register with Qt.

The modern QSS asked for ``"Plus Jakarta Sans"`` long before any font file was
shipped, so Qt silently fell back to Segoe UI on every machine that did not have
the font installed. These tests keep the asset, the loader and the stylesheet in
step, and they double as the check the packaged build runs via ``--selfcheck``.
"""

import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from runtime import fonts

REPO_ROOT = Path(__file__).resolve().parents[1]
FONT_DIR = REPO_ROOT / "resources" / "fonts"
SFNT_MAGIC = (b"\x00\x01\x00\x00", b"OTTO", b"true")


class FontAssetTests(unittest.TestCase):
    def test_every_declared_font_file_is_shipped(self):
        missing = [name for name in fonts.FONT_FILES if not (FONT_DIR / name).is_file()]
        self.assertEqual(missing, [], f"missing bundled fonts in {FONT_DIR}")

    def test_font_files_are_valid_sfnt_fonts(self):
        for name in fonts.FONT_FILES:
            with self.subTest(font=name):
                header = (FONT_DIR / name).read_bytes()[:4]
                self.assertIn(header, SFNT_MAGIC, f"{name} is not a TrueType/OpenType file")

    def test_variable_font_is_not_used(self):
        # Qt 6 collapses a variable font to one weight, which would flatten the
        # 600/700 QSS rules; static weights are shipped instead.
        self.assertFalse(
            [name for name in fonts.FONT_FILES if "Variable" in name],
            "ship static weights, not the variable font",
        )

    def test_licence_travels_with_the_font(self):
        licence = FONT_DIR / "OFL.txt"
        self.assertTrue(licence.is_file(), "SIL OFL requires the licence to ship with the font")
        text = licence.read_text(encoding="utf-8")
        self.assertIn("SIL OPEN FONT LICENSE", text.upper())


class FontRegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_registration_exposes_the_family_the_qss_asks_for(self):
        fonts.install(reset=True)
        self.assertIn(fonts.FONT_FAMILY, fonts.installed_families())

    def test_registration_is_idempotent(self):
        fonts.install(reset=True)
        first = fonts.install()
        second = fonts.install()
        self.assertEqual(first, second, "installing twice must not pile up font ids")
        self.assertEqual(len(first), len(fonts.FONT_FILES))


class StyleSheetParityTests(unittest.TestCase):
    def test_stylesheet_and_loader_agree_on_the_family_name(self):
        import app_modern

        self.assertIn(f'"{fonts.FONT_FAMILY}"', app_modern.MODERN_QSS)
        self.assertIn(fonts.FONT_FAMILY, app_modern.MODERN_QSS)

    def test_both_entry_points_register_the_fonts(self):
        for name in ("app.py", "app_modern.py"):
            with self.subTest(entry=name):
                source = (REPO_ROOT / name).read_text(encoding="utf-8")
                self.assertIn("fonts.install(", source)


if __name__ == "__main__":
    unittest.main()
