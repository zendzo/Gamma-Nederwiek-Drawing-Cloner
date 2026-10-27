"""No module may be imported by ``app.py`` without being used.

Four modules were dead imports: ``revision/controlled_writer.py``,
``revision/cleanup_writer.py``, ``revision/responsive_validator.py`` and
``autocad/background.py`` were each referenced exactly once in ``app.py`` — by
their own ``import`` line. Reading the file made the live pipeline look twice
as big as it is, and every import that is never called hides a real dependency
when it *is* called.

The check is import-name based, so it also catches a new dead import.
"""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DECOMMISSIONED = {
    "revision/controlled_writer.py",
    "revision/cleanup_writer.py",
    "revision/responsive_validator.py",
    "autocad/background.py",
    "lisp/background_engine.lsp",
    "lisp/revision_controlled_writer.lsp",
}


class DeadImportTests(unittest.TestCase):
    def _imports(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    names.append((alias.asname or alias.name, node.module or ""))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    names.append(((alias.asname or alias.name).split(".")[0], ""))
        return names

    def _used_names(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                base = node
                while isinstance(base, ast.Attribute):
                    base = base.value
                if isinstance(base, ast.Name):
                    used.add(base.id)
        return used

    def test_app_py_has_no_unused_imports(self):
        path = ROOT / "app.py"
        used = self._used_names(path)
        # `from PySide6.QtWidgets import *` has no trackable name; every *named*
        # import must be referenced in the module body.
        unused = [name for name, _ in self._imports(path)
                  if name != "*" and name not in used]
        self.assertEqual(unused, [], f"app.py imports but never uses: {unused}")

    def test_decommissioned_modules_are_gone(self):
        for relative in sorted(DECOMMISSIONED):
            with self.subTest(file=relative):
                self.assertFalse((ROOT / relative).exists(),
                                 f"{relative} was decommissioned; delete it rather than re-adding it")

    def test_no_module_imports_a_decommissioned_file(self):
        stems = {Path(relative).stem for relative in DECOMMISSIONED}
        for path in ROOT.rglob("*.py"):
            if "backup_before_" in str(path) or ".venv" in str(path) or "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for stem in stems:
                with self.subTest(file=str(path.relative_to(ROOT)), module=stem):
                    self.assertNotIn(f"import {stem}", text)
                    self.assertNotIn(f"from .{stem}", text)

    def test_live_engines_are_still_importable(self):
        # The cleanup must not have taken a live engine with it.
        for module, attribute in (
            ("revision.stable_external_writer", "StableExternalActionWriter"),
            ("revision.reopen_validator", "StableReopenValidator"),
            ("revision.atomic_publish_dry_run", "AtomicPublisherDryRun"),
            ("revision.atomic_publisher", "AtomicPublisher"),
            ("revision.pdf_publisher", "PdfPublisher"),
            ("revision.completion_plan", "TitleBlockCompletionPlanBuilder"),
            ("runtime.production_workers", "DiscoveryWorker"),
        ):
            with self.subTest(module=module):
                imported = __import__(module, fromlist=[attribute])
                self.assertTrue(hasattr(imported, attribute))


if __name__ == "__main__":
    unittest.main()
