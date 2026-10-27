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


if __name__ == "__main__":
    unittest.main()
