"""General Cloner tests.

These tests use a fake Core Console runner: the real LISP cannot execute on a
machine without AutoCAD, but the Python engine's discovery, staging, conflict
policy, reporting, and failure isolation are all exercised for real.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from config.settings import Settings
from general.cloner import (
    GENERAL_LOG_NAME,
    GENERAL_REPORT_NAME,
    GeneralClonerEngine,
    mapping_pairs,
    scan_general_dwgs,
    validate_general_job,
)
from general.constants import GENERAL_MODE, STRUCTURAL_MODE
from general.scanner import GeneralDrawing
from models import SourceFile
from scanner.universal import Scanner
from runtime.winpath import long_path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

MAPPING = {
    "project_from": "IJMUIDEN VER GAMMA",
    "project_to": "NEDERWIEK BETA",
    "drawing_from": "2013J310015",
    "drawing_to": "I301-J30019",
    "long_from": "OEPA230146",
    "long_to": "OEPA240101",
    "short_from": "230146",
    "short_to": "240101",
    "contractor_doc_from": "2GWG",
    "contractor_doc_to": "2GWN",
}


def _replace_bytes(data: bytes, pairs):
    total = 0
    for old, new in pairs:
        pattern = re.compile(re.escape(old.encode("cp1252")), re.I)
        data, count = pattern.subn(new.encode("cp1252"), data)
        total += count
    return data, total


def _read_pairs(config_path):
    lines = Path(config_path).read_text(encoding="cp1252").splitlines()
    pairs = []
    for line in lines[1:]:
        if "\t" in line:
            old, new = line.split("\t", 1)
            if old:
                pairs.append((old, new))
    return pairs


def fake_runner(fail_name=None, force_failure=False, marker=True):
    """Return a process runner that rewrites the staged DWG and writes a TSV."""

    def run(*, job, working_dwg, config_path, result_path):
        if fail_name and Path(fail_name).stem in str(job):
            raise RuntimeError("simulated per-file failure")
        pairs = _read_pairs(config_path)
        data, count = _replace_bytes(Path(working_dwg).read_bytes(), pairs)
        Path(working_dwg).write_bytes(data)
        status = "FAILED" if force_failure else "OK"
        header = "Handle\tEntity Type\tOld Value\tNew Value\tReplacements\tStatus\n"
        row = f"\t\t\t\t{count}\t{status}\n"
        Path(result_path).write_text(header + row, encoding="utf-8")
        stdout = ""
        if marker:
            stdout = (
                f"{GeneralClonerEngine.LOAD_MARKER}\n"
                f"{GeneralClonerEngine.SUCCESS_MARKER}REPLACEMENTS={count}"
            )
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    return run


class GeneralClonerEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.destination = self.root / "destination"
        self.bundle = "BUNDLE"

    def tearDown(self):
        try:
            self.tmp.cleanup()
        except OSError:
            # The long-path regression test intentionally creates a tree beyond
            # MAX_PATH; tempfile's plain rmtree cannot remove it.
            shutil.rmtree(long_path(self.tmp.name), ignore_errors=True)

    def engine(self, *, policy="BACKUP_OVERWRITE", runner=None, mapping=None):
        return GeneralClonerEngine(
            self.source,
            self.destination,
            self.bundle,
            self.root,
            mapping if mapping is not None else MAPPING,
            policy=policy,
            workers=1,
            batch_size=1,
            process_runner=runner or fake_runner(),
        )

    def write(self, relative, data=b"Project IJMUIDEN VER GAMMA Drawing 2013J310015"):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_scan_includes_root_and_multiple_nested_levels(self):
        self.write("root.dwg")
        self.write("one/child.DWG")
        self.write("one/two/grand.Dwg")
        drawings = self.engine().scan()
        self.assertEqual(
            [item.relative_path for item in drawings],
            ["one/child.DWG", "one/two/grand.Dwg", "root.dwg"],
        )

    def test_scan_matches_dwg_case_insensitively(self):
        self.write("a.dwg")
        self.write("b.DWG")
        self.write("c.Dwg")
        self.write("d.dWg")
        drawings = self.engine().scan()
        self.assertEqual(len(drawings), 4)

    def test_scan_ignores_dxf_xls_xlsx_nc1_and_part_lists(self):
        self.write("only/drawing.dwg")
        for name in ("part.xls", "part.xlsx", "part.dxf", "part.nc1", "Part List.xlsx", "readme.txt"):
            self.write("mixed/" + name)
        drawings = self.engine().scan()
        self.assertEqual([item.relative_path for item in drawings], ["only/drawing.dwg"])

    def test_arbitrary_filenames_need_no_ga_as_sp_or_revision_tokens(self):
        self.write("alpha.dwg")
        self.write("a name with spaces.dwg")
        self.write("no-revision-or-discipline.dwg")
        drawings = self.engine().scan()
        self.assertEqual(len(drawings), 3)
        # GeneralDrawing deliberately has no identity/revision/drawing-type
        # fields; those are Structural-only concepts.
        self.assertFalse(hasattr(drawings[0], "identity"))
        self.assertFalse(hasattr(drawings[0], "revision"))

    def test_run_preserves_relative_subfolders_and_never_changes_the_source(self):
        source_root = self.write("root.dwg")
        source_nested = self.write("nested/child.DWG")
        source_deep = self.write("nested/deep/grand.Dwg")
        original = {path: path.read_bytes() for path in (source_root, source_nested, source_deep)}
        engine = self.engine()
        results, report, cancelled, skipped = engine.run()
        self.assertFalse(cancelled)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(item.status == "SUCCESS" for item in results), results)
        bundle = self.destination / self.bundle
        for relative in ("root.dwg", "nested/child.DWG", "nested/deep/grand.Dwg"):
            destination = bundle / relative
            self.assertTrue(destination.is_file(), destination)
            self.assertIn(b"NEDERWIEK BETA", destination.read_bytes())
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data, "source must never be modified")
        self.assertTrue(Path(report).is_file())

    def test_duplicate_filenames_in_separate_subfolders_are_both_kept(self):
        self.write("a/plan.dwg")
        self.write("b/plan.dwg")
        results, _, _, _ = self.engine().run()
        self.assertEqual([item.status for item in results], ["SUCCESS", "SUCCESS"])
        bundle = self.destination / self.bundle
        self.assertTrue((bundle / "a/plan.dwg").is_file())
        self.assertTrue((bundle / "b/plan.dwg").is_file())

    def test_duplicate_destination_path_is_reported_as_skipped(self):
        # Windows cannot hold two files that differ only by case, so simulate the
        # scanner's duplicate marker directly.  The second drawing must not
        # overwrite the first.
        first = GeneralDrawing("a.dwg", "Same.dwg", "Same.dwg", 1, "")
        second = GeneralDrawing("b.dwg", "same.dwg", "same.dwg", 1, "", duplicate_of="Same.dwg")
        second.destination_path = str(self.destination / self.bundle / "same.dwg")
        first.destination_path = str(self.destination / self.bundle / "Same.dwg")
        engine = self.engine()
        result = engine.clone_one(second)
        self.assertEqual(result.status, "SKIPPED")
        self.assertEqual(result.action, "SKIP_DUPLICATE")

    def test_empty_source_is_a_clean_zero_result(self):
        engine = self.engine()
        results, report, cancelled, skipped = engine.run()
        self.assertEqual(results, [])
        self.assertFalse(cancelled)
        payload = json.loads(Path(report).read_text(encoding="utf-8"))
        self.assertEqual(payload["summary"]["discovered"], 0)
        self.assertEqual(payload["summary"]["processed"], 0)
        self.assertTrue(payload["complete"])
        log = (self.destination / self.bundle / "_GNI_REPORTS" / GENERAL_LOG_NAME).read_text(encoding="utf-8")
        self.assertIn("No DWG files", log)

    def test_no_dwg_folder_is_a_clean_zero_result(self):
        self.write("notes.txt")
        self.write("parts.xlsx")
        results, report, _, _ = self.engine().run()
        self.assertEqual(results, [])
        payload = json.loads(Path(report).read_text(encoding="utf-8"))
        self.assertEqual(payload["summary"]["discovered"], 0)

    def test_skip_different_policy_leaves_the_existing_destination_alone(self):
        self.write("plan.dwg", b"Project IJMUIDEN VER GAMMA")
        self.engine().run()
        destination = self.destination / self.bundle / "plan.dwg"
        before = destination.read_bytes()
        # Change the source so the processed output differs from the destination.
        self.write("plan.dwg", b"Project IJMUIDEN VER GAMMA changed")
        results, _, _, _ = self.engine(policy="SKIP_DIFFERENT").run()
        self.assertEqual(results[0].status, "SKIPPED")
        self.assertEqual(results[0].action, "SKIP_DIFFERENT")
        self.assertEqual(destination.read_bytes(), before)

    def test_backup_overwrite_policy_backs_up_before_replacing(self):
        self.write("plan.dwg", b"Project IJMUIDEN VER GAMMA")
        self.engine().run()
        destination = self.destination / self.bundle / "plan.dwg"
        before = destination.read_bytes()
        self.write("plan.dwg", b"Project IJMUIDEN VER GAMMA changed")
        results, _, _, _ = self.engine(policy="BACKUP_OVERWRITE").run()
        result = results[0]
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.action, "BACKUP_OVERWRITE")
        self.assertTrue(Path(result.backup_path).is_file())
        self.assertEqual(Path(result.backup_path).read_bytes(), before)
        self.assertNotEqual(destination.read_bytes(), before)

    def test_keep_both_policy_writes_a_copy_and_keeps_the_original(self):
        self.write("plan.dwg", b"Project IJMUIDEN VER GAMMA")
        self.engine().run()
        destination = self.destination / self.bundle / "plan.dwg"
        before = destination.read_bytes()
        self.write("plan.dwg", b"Project IJMUIDEN VER GAMMA changed")
        results, _, _, _ = self.engine(policy="KEEP_BOTH").run()
        result = results[0]
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.action, "KEEP_BOTH")
        self.assertEqual(destination.read_bytes(), before)
        copy = self.destination / self.bundle / "plan_COPY_01.dwg"
        self.assertTrue(copy.is_file())
        self.assertIn(b"changed", copy.read_bytes())

    def test_per_file_failure_does_not_stop_the_batch(self):
        self.write("good.dwg")
        self.write("bad.dwg")
        # The fake runner fails only when the staged working file is bad.dwg.
        engine = self.engine(runner=fake_runner(fail_name="bad.dwg"))
        results, report, _, _ = engine.run()
        by_name = {Path(item.source_path).name: item for item in results}
        self.assertEqual(by_name["good.dwg"].status, "SUCCESS")
        self.assertEqual(by_name["bad.dwg"].status, "FAILED")
        self.assertIn("simulated per-file failure", by_name["bad.dwg"].error)
        self.assertTrue((self.destination / self.bundle / "good.dwg").is_file())
        self.assertFalse((self.destination / self.bundle / "bad.dwg").exists())
        payload = json.loads(Path(report).read_text(encoding="utf-8"))
        self.assertEqual(payload["summary"]["success"], 1)
        self.assertEqual(payload["summary"]["failed"], 1)

    def test_report_records_source_destination_status_replacements_and_failures(self):
        self.write("good.dwg")
        self.write("bad.dwg")
        engine = self.engine(runner=fake_runner(fail_name="bad.dwg"))
        results, report, _, _ = engine.run()
        payload = json.loads(Path(report).read_text(encoding="utf-8"))
        self.assertEqual(payload["mode"], GENERAL_MODE)
        self.assertEqual(payload["collision_policy"], "BACKUP_OVERWRITE")
        self.assertEqual(payload["summary"]["discovered"], 2)
        rows = {Path(item["source_path"]).name: item for item in payload["results"]}
        self.assertEqual(rows["good.dwg"]["status"], "SUCCESS")
        self.assertGreater(rows["good.dwg"]["replacements"], 0)
        self.assertEqual(rows["bad.dwg"]["status"], "FAILED")
        self.assertIn("simulated", rows["bad.dwg"]["error"])
        for row in payload["results"]:
            for key in ("source_path", "destination_path", "status", "replacements", "skip_reason", "error"):
                self.assertIn(key, row)

    def test_lisp_write_failure_is_reported(self):
        self.write("plan.dwg")
        engine = self.engine(runner=fake_runner(force_failure=True))
        results, _, _, _ = engine.run()
        self.assertEqual(results[0].status, "FAILED")
        self.assertIn("GENERAL_WRITE_FAILED", results[0].error)

    def test_wrong_drawing_status_is_a_failure(self):
        self.write("plan.dwg")

        def wrong_drawing_runner(*, job, working_dwg, config_path, result_path):
            Path(result_path).write_text(
                "Handle\tEntity Type\tOld Value\tNew Value\tReplacements\tStatus\n"
                "\t\t\t\t0\tWRONG_DRAWING\n",
                encoding="utf-8",
            )
            return SimpleNamespace(
                returncode=0,
                stdout=GeneralClonerEngine.LOAD_MARKER,
                stderr="",
            )

        results, _, _, _ = self.engine(runner=wrong_drawing_runner).run()
        self.assertEqual(results[0].status, "FAILED")
        self.assertIn("WRONG_DRAWING", results[0].error)

    def test_source_validation_rejects_a_missing_folder(self):
        engine = self.engine()
        missing = self.root / "missing"
        with self.assertRaises(ValueError):
            GeneralClonerEngine(
                missing, self.destination, self.bundle, self.root, MAPPING,
                process_runner=fake_runner(),
            )

    def test_destination_inside_source_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_general_job(self.source, self.source / "inside", self.bundle, self.root)

    def test_mapping_pairs_skip_empty_and_duplicate_from_tokens(self):
        pairs = mapping_pairs({"project_from": "A", "project_to": "B", "drawing_from": "", "drawing_to": "C"})
        self.assertEqual(pairs, [("A", "B")])
        pairs = mapping_pairs([("A", "B"), ("a", "C"), ("", "D")])
        self.assertEqual(pairs, [("A", "B")])

    def test_report_and_log_are_written_under_the_bundle_reports_folder(self):
        self.write("plan.dwg")
        results, report, _, _ = self.engine().run()
        self.assertTrue(Path(report).is_file())
        self.assertEqual(Path(report).name, GENERAL_REPORT_NAME)
        log = Path(report).parent / GENERAL_LOG_NAME
        self.assertTrue(log.is_file())
        text = log.read_text(encoding="utf-8")
        self.assertIn("MODE=GENERAL_CLONER", text)
        self.assertIn("DISCOVERED=1", text)
        self.assertIn("SUMMARY", text)

    def test_call_line_passes_the_expected_working_filename(self):
        engine = self.engine()
        line = engine._call_line(Path("c.tsv"), Path("r.tsv"), Path("work_abc123.dwg"))
        self.assertIn('"work_abc123.dwg"', line)
        self.assertIn("GNI:RUN-GENERAL-CLONE", line)

    def test_long_source_and_destination_paths_are_processed(self):
        if os.name != "nt":
            self.skipTest("Windows MAX_PATH regression")
        deep = self.source
        for index in range(8):
            deep = deep / ("very_long_folder_name_%02d" % index)
        long_path(deep).mkdir(parents=True, exist_ok=True)
        name = ("A" * 120) + ".dwg"
        source = deep / name
        long_path(source).write_bytes(b"Project IJMUIDEN VER GAMMA")
        self.assertGreater(len(str(source)), 260)

        captured = {}

        def runner(**kwargs):
            captured["job"] = str(kwargs["job"])
            return fake_runner()(**kwargs)

        results, report, _, _ = self.engine(runner=runner).run()
        self.assertEqual(results[0].status, "SUCCESS", results[0].error)
        self.assertLess(len(captured["job"]), 200)
        destination = self.destination / self.bundle / Path(source).relative_to(self.source)
        self.assertTrue(long_path(destination).is_file())
        self.assertEqual(long_path(source).read_bytes(), b"Project IJMUIDEN VER GAMMA")


class GeneralWorkerTests(unittest.TestCase):
    """The QThread adapter must forward every payload key to the engine."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_run_general_forwards_payload_and_returns_engine_result(self):
        import general.cloner as gc
        import runtime.background_controller as bc

        captured = {}

        class FakeEngine:
            def __init__(self, source, destination, bundle, application, mapping, **kwargs):
                captured["args"] = (source, destination, bundle, application, mapping)
                captured["kwargs"] = kwargs

            def run(self, **kwargs):
                captured["run"] = kwargs
                return (["result"], "report.json", False, 0)

        saved = gc.GeneralClonerEngine
        gc.GeneralClonerEngine = FakeEngine
        try:
            worker = bc.PipelineWorker(
                "general",
                {
                    "source": "source",
                    "destination": "destination",
                    "bundle": "bundle",
                    "app": "app",
                    "mapping": {"project_from": "A", "project_to": "B"},
                    "core": "core.exe",
                    "lisp": "general.lsp",
                    "policy": "KEEP_BOTH",
                    "workers": 3,
                    "batch_size": 2,
                    "timeout": 99,
                },
            )
            result = worker._run_general()
        finally:
            gc.GeneralClonerEngine = saved

        self.assertEqual(result, (["result"], "report.json", False, 0))
        self.assertEqual(captured["args"][0], "source")
        self.assertEqual(captured["args"][4], {"project_from": "A", "project_to": "B"})
        self.assertEqual(captured["kwargs"]["policy"], "KEEP_BOTH")
        self.assertEqual(captured["kwargs"]["workers"], 3)
        self.assertEqual(captured["kwargs"]["batch_size"], 2)
        self.assertEqual(captured["kwargs"]["timeout"], 99)
        self.assertIn("should_cancel", captured["run"])
        self.assertIn("on_progress", captured["run"])
        self.assertIn("on_log", captured["run"])


class WinPathTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows extended paths only")
    def test_local_and_unc_paths_are_prefixed(self):
        local = str(long_path(r"C:\Temp\x.dwg"))
        unc = str(long_path(r"\\server\share\x.dwg"))
        self.assertTrue(local.startswith("\\\\?\\C:\\"), local)
        self.assertTrue(unc.startswith("\\\\?\\UNC\\server\\share"), unc)
        self.assertEqual(str(long_path(local)), local)


class GeneralClonerLispContractTests(unittest.TestCase):
    def test_lisp_declares_the_python_marker_and_has_no_activex(self):
        path = Path(__file__).resolve().parents[1] / "lisp" / "general_cloner.lsp"
        text = path.read_text(encoding="utf-8")
        self.assertIn(GeneralClonerEngine.LOAD_MARKER, text)
        self.assertIn(GeneralClonerEngine.SUCCESS_MARKER, text)
        self.assertIn(GeneralClonerEngine.FAILURE_MARKER, text)
        self.assertNotIn("vla-", text)
        self.assertIn("GNI:RUN-GENERAL-CLONE", text)

    def test_lisp_replaces_group_1_and_group_3_text(self):
        path = Path(__file__).resolve().parents[1] / "lisp" / "general_cloner.lsp"
        text = path.read_text(encoding="utf-8")
        self.assertIn("(= (car pair) 1)", text)
        self.assertIn("(= (car pair) 3)", text)
        self.assertIn("TEXT,MTEXT,ATTRIB,ATTDEF", text)
        self.assertIn("WRONG_DRAWING", text)


class StructuralRegressionTests(unittest.TestCase):
    def test_structural_scanner_still_applies_ga_as_sp_rules(self):
        self.assertEqual(Scanner.parse("SP-1001_A"), ("SP-1001", "A", ""))
        self.assertEqual(Scanner.dtype("x-GA-y.dwg"), "GA")
        files = [
            SourceFile("a_02.dwg", "a_02.dwg", "a_02.dwg", "DWG", 1, "2026-01-01T00:00:00+00:00", role="DWG", drawing_type="SP", identity="SP-1", revision="02"),
            SourceFile("a_A.dwg", "a_A.dwg", "a_A.dwg", "DWG", 1, "2024-01-01T00:00:00+00:00", role="DWG", drawing_type="SP", identity="SP-1", revision="A"),
        ]
        group = Scanner.groups(None, files)[0]
        self.assertTrue(group.primary_source.endswith("a_02.dwg"))
        self.assertTrue(group.revision_a_donor.endswith("a_A.dwg"))

    def test_general_scan_does_not_reuse_structural_grouping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SP-1_A.dwg").write_bytes(b"x")
            (root / "arbitrary.dwg").write_bytes(b"x")
            drawings = scan_general_dwgs(root)
            self.assertEqual(len(drawings), 2)
            self.assertEqual([item.relative_path for item in drawings], ["arbitrary.dwg", "SP-1_A.dwg"])

    def test_settings_default_to_structural_and_persist_general_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            self.assertEqual(Settings.load(path).cloner_mode, STRUCTURAL_MODE)
            saved = Settings(
                cloner_mode=GENERAL_MODE,
                general_conflict_policy="KEEP_BOTH",
                general_workers=7,
                general_batch_size=3,
            )
            saved.save(path)
            loaded = Settings.load(path)
            self.assertEqual(loaded.cloner_mode, GENERAL_MODE)
            self.assertEqual(loaded.general_conflict_policy, "KEEP_BOTH")
            self.assertEqual(loaded.general_workers, 7)
            self.assertEqual(loaded.general_batch_size, 3)

    def test_invalid_mode_and_policy_fall_back_to_safe_defaults(self):
        settings = Settings(cloner_mode="NOT_A_MODE", general_conflict_policy="OVERWRITE_EVERYTHING")
        self.assertEqual(settings.cloner_mode, STRUCTURAL_MODE)
        self.assertEqual(settings.general_conflict_policy, "BACKUP_OVERWRITE")


class GeneralModeUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import app as legacy

        self.legacy = legacy
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_cfg = legacy.CFG
        legacy.CFG = Path(self.tmp.name) / "settings.json"
        self.main = legacy.Main()

    def tearDown(self):
        self.main.close()
        self.main.deleteLater()
        self.legacy.CFG = self.saved_cfg
        self.tmp.cleanup()

    def _tab_index(self, title):
        tabs = self.main.centralWidget()
        return next(index for index in range(tabs.count()) if tabs.tabText(index) == title)

    def test_switching_modes_hides_structural_and_supporting_options(self):
        self.assertEqual(self.main.cloner_mode(), STRUCTURAL_MODE)
        self.assertFalse(self.main.pipeline_group.isHidden())
        self.assertFalse(self.main.recent_box.isHidden())
        self.main.mode.setCurrentIndex(self.main.mode.findData(GENERAL_MODE))
        self.main._mode_changed()
        self.assertTrue(self.main.pipeline_group.isHidden())
        self.assertTrue(self.main.recent_box.isHidden())
        tabs = self.main.centralWidget()
        for title in ("Drawing Candidates", "Supporting File Clone", "Title Block Discovery", "Revision Plan"):
            self.assertFalse(tabs.tabBar().isTabVisible(self._tab_index(title)), title)
        self.assertTrue(tabs.tabBar().isTabVisible(self._tab_index("General Cloner")))
        self.main.mode.setCurrentIndex(self.main.mode.findData(STRUCTURAL_MODE))
        self.main._mode_changed()
        self.assertFalse(self.main.pipeline_group.isHidden())
        for title in ("Drawing Candidates", "Supporting File Clone", "Title Block Discovery", "Revision Plan"):
            self.assertTrue(tabs.tabBar().isTabVisible(self._tab_index(title)), title)

    def test_general_tab_keeps_source_destination_mapping_progress_and_reports(self):
        self.assertTrue(hasattr(self.main, "general_runb"))
        self.assertTrue(hasattr(self.main, "general_cancelb"))
        self.assertTrue(hasattr(self.main, "general_tbl"))
        self.assertTrue(hasattr(self.main, "general_log"))
        self.assertTrue(hasattr(self.main, "general_prog"))
        self.assertGreaterEqual(len(self.main.maped), 5)
        # The mapping fields stay visible in both modes.
        self.assertFalse(self.main.maped[0][1].isHidden())


if __name__ == "__main__":
    unittest.main()
