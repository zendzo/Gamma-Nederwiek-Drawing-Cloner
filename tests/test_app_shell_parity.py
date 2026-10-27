"""Guards the "one controller, one UI" rule.

`app_modern.py` used to re-define `next_stage`, `_stage_complete`,
`refresh_pipeline` and `_advance_pipeline`. The copies drifted from `app.py`
(skipped-stage handling and `refresh_primary()` were lost). The modern shell now
only restyles; these tests fail if the duplication comes back.
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import app as legacy

try:
    import app_modern
except Exception as error:  # pragma: no cover - only when PySide6 is missing
    app_modern = None
    _IMPORT_ERROR = error
else:
    _IMPORT_ERROR = None


class ShellParityTests(unittest.TestCase):
    def test_modern_shell_is_importable(self):
        if app_modern is None:  # pragma: no cover
            self.skipTest("app_modern unavailable: %r" % (_IMPORT_ERROR,))
        self.assertTrue(hasattr(app_modern, "ModernMain"))

    def test_modern_shell_does_not_override_pipeline_logic(self):
        if app_modern is None:  # pragma: no cover
            self.skipTest("app_modern unavailable")
        for name in ("next_stage", "_stage_complete", "_report_state",
                     "refresh_pipeline", "refresh_primary", "_advance_pipeline",
                     "run_stage", "run_next_stage", "closeEvent"):
            with self.subTest(method=name):
                self.assertNotIn(
                    name,
                    vars(app_modern.ModernMain),
                    "ModernMain must inherit %s from app.Main, not re-implement it" % name,
                )

    def test_modern_shell_inherits_the_controller(self):
        if app_modern is None:  # pragma: no cover
            self.skipTest("app_modern unavailable")
        self.assertTrue(issubclass(app_modern.ModernMain, legacy.Main))

    def test_pipeline_contract_is_unchanged(self):
        keys = [key for key, _ in legacy.PIPELINE]
        self.assertEqual(
            keys,
            ["scan", "destination", "clone", "discovery", "plan", "writer",
             "validator", "dryrun", "publish", "pdf"],
        )
        self.assertEqual(legacy.AUTO_STAGES[-1], "dryrun")
        self.assertNotIn("publish", legacy.AUTO_STAGES)
        self.assertNotIn("pdf", legacy.AUTO_STAGES)
        for key, name in legacy.STAGE_REPORTS.items():
            self.assertTrue(name.endswith(".json"), key)


class NavCaptionTests(unittest.TestCase):
    """The sidebar captions must cover the tabs `app.py` actually builds.

    The map once used pre-rename keys ("Pipeline", "Supporting Files",
    "Discovery"), so every real page silently fell back to its full title.
    """

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_every_real_page_has_a_caption(self):
        if app_modern is None:  # pragma: no cover
            self.skipTest("app_modern unavailable")
        main = legacy.Main()
        try:
            tabs = main.centralWidget()
            titles = [tabs.tabText(index) for index in range(tabs.count())]
        finally:
            main.close()
            main.deleteLater()
        self.assertTrue(titles)
        for title in titles:
            with self.subTest(page=title):
                self.assertIn(title, app_modern.NAV_CAPTIONS,
                              "no caption mapped for tab %r" % title)
                caption = app_modern.ModernMain._nav_caption(title)
                self.assertTrue(caption)
                # The icons supply the left padding; stray spaces in the caption
                # would push long labels over the sidebar width and elide them.
                self.assertEqual(caption, caption.strip())
                # The header subtitle had the same stale-key bug.
                self.assertIn(title, app_modern.PAGE_DESCRIPTIONS,
                              "no subtitle mapped for tab %r" % title)


def _mean_rgb(image):
    """Average colour of the visible pixels, or None when fully transparent."""
    total = count = 0
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            if color.alpha() > 40:
                total += color.red() + color.green() + color.blue()
                count += 1
    return (total / count) if count else None


class NavIconTests(unittest.TestCase):
    """The sidebar icons are real SVG assets, not text glyphs or emoji.

    `_nav_icon()` recolours each file by replacing the authored stroke colour,
    so a renamed page or a hand-edited SVG would silently render an empty slot.
    """

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_every_real_page_has_an_icon_file(self):
        if app_modern is None:  # pragma: no cover
            self.skipTest("app_modern unavailable")
        main = legacy.Main()
        try:
            tabs = main.centralWidget()
            titles = [tabs.tabText(index) for index in range(tabs.count())]
        finally:
            main.close()
            main.deleteLater()
        self.assertTrue(titles)
        for title in titles:
            with self.subTest(page=title):
                self.assertIn(title, app_modern.NAV_ICONS,
                              "no icon mapped for tab %r" % title)
                path = app_modern.ICON_DIR / app_modern.NAV_ICONS[title]
                self.assertTrue(path.is_file(), "missing icon asset: %s" % path)
                self.assertEqual(path.suffix, ".svg")

    def test_icon_and_caption_maps_cover_the_same_pages(self):
        if app_modern is None:  # pragma: no cover
            self.skipTest("app_modern unavailable")
        self.assertEqual(set(app_modern.NAV_ICONS), set(app_modern.NAV_CAPTIONS))

    def test_every_icon_is_a_valid_svg(self):
        if app_modern is None:  # pragma: no cover
            self.skipTest("app_modern unavailable")
        from PySide6.QtSvg import QSvgRenderer
        for title, name in app_modern.NAV_ICONS.items():
            with self.subTest(page=title):
                renderer = QSvgRenderer(str(app_modern.ICON_DIR / name))
                self.assertTrue(renderer.isValid(), "%s is not valid SVG" % name)
                self.assertTrue(renderer.defaultSize().isValid())

    def test_every_icon_carries_the_recolour_token(self):
        if app_modern is None:  # pragma: no cover
            self.skipTest("app_modern unavailable")
        for title, name in app_modern.NAV_ICONS.items():
            with self.subTest(page=title):
                source = (app_modern.ICON_DIR / name).read_text(encoding="utf-8")
                self.assertIn(
                    app_modern.ICON_COLOR_IDLE, source,
                    "%s must be authored in ICON_COLOR_IDLE so the checked state "
                    "can be recoloured" % name,
                )
                self.assertNotIn("currentColor", source)

    def test_sidebar_buttons_render_icons(self):
        if app_modern is None:  # pragma: no cover
            self.skipTest("app_modern unavailable")
        main = app_modern.ModernMain()
        try:
            self.assertEqual(len(main.nav_buttons), len(main._page_names))
            for button, title in zip(main.nav_buttons, main._page_names):
                with self.subTest(page=title):
                    self.assertFalse(button.icon().isNull(),
                                     "sidebar button %r has no icon" % title)
                    self.assertNotEqual(button.toolButtonStyle(),
                                        app_modern.Qt.ToolButtonTextOnly)
                    self.assertEqual(button.iconSize().width(), app_modern.ICON_SIZE)
                    self.assertTrue(button.text())
        finally:
            main.close()
            main.deleteLater()

    def test_checked_state_icon_is_brighter(self):
        if app_modern is None:  # pragma: no cover
            self.skipTest("app_modern unavailable")
        icon = app_modern._nav_icon("Setup")
        self.assertFalse(icon.isNull())
        size = app_modern.QSize(app_modern.ICON_SIZE, app_modern.ICON_SIZE)
        idle = _mean_rgb(icon.pixmap(size, app_modern.QIcon.Normal,
                                     app_modern.QIcon.Off).toImage())
        active = _mean_rgb(icon.pixmap(size, app_modern.QIcon.Normal,
                                       app_modern.QIcon.On).toImage())
        self.assertIsNotNone(idle)
        self.assertIsNotNone(active)
        self.assertGreater(active, idle + 20,
                           "the checked icon must be white, not the idle slate")

    def test_missing_icon_degrades_to_null_icon(self):
        if app_modern is None:  # pragma: no cover
            self.skipTest("app_modern unavailable")
        saved = app_modern.ICON_DIR
        app_modern.ICON_DIR = saved / "does-not-exist"
        try:
            self.assertTrue(app_modern._nav_icon("Setup").isNull())
        finally:
            app_modern.ICON_DIR = saved


if __name__ == "__main__":
    unittest.main()
