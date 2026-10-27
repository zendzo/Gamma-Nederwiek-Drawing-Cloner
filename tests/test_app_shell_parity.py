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
                self.assertTrue(app_modern.ModernMain._nav_caption(title).startswith("  "))
                # The header subtitle had the same stale-key bug.
                self.assertIn(title, app_modern.PAGE_DESCRIPTIONS,
                              "no subtitle mapped for tab %r" % title)


if __name__ == "__main__":
    unittest.main()
