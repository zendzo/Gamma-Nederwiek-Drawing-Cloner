"""Contracts of the batched, resumable discovery stage.

Discovery used to run one Core Console process per drawing, serially. On the
production machine that is ~5.5 s per drawing of which ~4.7 s is process
startup, so the stage scaled with drawing count instead of with the CPU.

Three things must stay true after batching, and each is guarded here:

* ``batch_size <= 1`` still means exactly one process per drawing (the
  deterministic path the rest of the suite relies on);
* a drawing can never be promoted to SUCCESS because a *sibling* in the same
  process succeeded — every drawing is validated against its own TSV;
* a checkpoint record is reused only when the source DWG still hashes to the
  recorded value, so a cancelled sweep resumes but a re-saved drawing does not.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from runtime.production_workers import DiscoveryWorker
from titleblock import discovery as discovery_module
from titleblock.discovery import DiscoveryField, DiscoveryResult, TitleBlockDiscoveryEngine

TSV_HEADER = (
    "Drawing\tSpace\tEntity Type\tHandle\tLayer\tTag\tValue\tInsertion Point\t"
    "Owner Handle\tOwner Type\tBlock Name\n"
)


def _group(identity, source):
    return SimpleNamespace(identity=identity, drawing_type="GA", primary_source=str(source))


def _tsv_row(identity, value="A"):
    return (
        f"{identity}.dwg\tMODEL\tATTRIB\t1A\tNEDERWIEK_DATA\tREV\t{value}\t"
        "0,0,0\t1B\tINSERT\tTITLE\n"
    )


class _FakeEngine:
    """Minimal stand-in so worker batching can be tested without Core Console."""

    def __init__(self):
        self.calls = []
        self.report_calls = []

    @staticmethod
    def _batches(groups, size):
        return [groups[i:i + size] for i in range(0, len(groups), max(1, size))]

    def discover_batch(self, groups):
        self.calls.append([g.identity for g in groups])
        return [
            DiscoveryResult(g.identity, g.drawing_type, g.primary_source, "SUCCESS", "")
            for g in groups
        ]

    def write_report(self, bundle_root, results, complete=True):
        self.report_calls.append((list(results), complete))
        root = Path(bundle_root)
        return root / "r.json", root / "r.csv"


class BatchSizeTests(unittest.TestCase):
    def test_batches_partition_without_loss(self):
        groups = list(range(10))
        batches = TitleBlockDiscoveryEngine._batches(groups, 4)
        self.assertEqual([len(b) for b in batches], [4, 4, 2])
        self.assertEqual([item for batch in batches for item in batch], groups)

    def test_batch_size_is_clamped_to_at_least_one(self):
        self.assertEqual(TitleBlockDiscoveryEngine._batches([1, 2, 3], 0), [[1], [2], [3]])
        self.assertEqual(TitleBlockDiscoveryEngine._batches([1, 2, 3], -5), [[1], [2], [3]])

    def test_single_drawing_batch_falls_back_to_the_serial_path(self):
        # discover_batch must never try to launch an empty `.scr` loop.
        data = {"used": []}

        def fake_group(self, group):
            data["used"].append(group.identity)
            return DiscoveryResult(group.identity, "GA", "", "SUCCESS", "")

        with mock.patch.object(TitleBlockDiscoveryEngine, "__init__", lambda self, *a, **k: None), \
                mock.patch.object(TitleBlockDiscoveryEngine, "discover_group", fake_group):
            engine = TitleBlockDiscoveryEngine()
            self.assertEqual([r.identity for r in engine.discover_batch([_group("A", "x.dwg")])], ["A"])
        self.assertEqual(data["used"], ["A"])


class PerDrawingIsolationTests(unittest.TestCase):
    """One process, several drawings: success must be decided per drawing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.engine = TitleBlockDiscoveryEngine.__new__(TitleBlockDiscoveryEngine)
        self.engine.Load_X = None
        self.engine.VERSION_MARKER = TitleBlockDiscoveryEngine.VERSION_MARKER
        self.engine.SUCCESS_MARKER = TitleBlockDiscoveryEngine.SUCCESS_MARKER
        self.engine.settings = SimpleNamespace(
            project_from="", project_to="", drawing_from="", drawing_to="",
            long_from="", long_to="", short_from="", short_to="",
        )
        self.engine.job_root = self.root / "jobs"
        self.engine.runtime_root = self.root / "trusted"
        self.engine.runtime_lisp = self.engine.runtime_root / "title_block_discovery.lsp"
        for folder in (self.engine.job_root, self.engine.runtime_root):
            folder.mkdir(parents=True)
        self.engine.runtime_lisp.write_text(";; lsp\n", encoding="utf-8")
        self.engine.core = Path("accoreconsole.exe")
        self.engine.timeout = 5

    def _source(self, name, body=b"DWG"):
        path = self.root / name
        path.write_bytes(body)
        return path

    def _result(self, identity, job, tsv_text):
        job.mkdir(parents=True, exist_ok=True)
        tsv = job / "title_block_discovery.tsv"
        if tsv_text is not None:
            tsv.write_text(tsv_text, encoding="utf-8")
        result = DiscoveryResult(identity, "GA", str(self._source(identity + ".dwg")), "PROCESSING",
                                 str(job), tsv_path=str(tsv), log_path=str(job / "discovery.log"))
        (job / "discovery.log").write_text("STATUS=JOB_CREATED\n", encoding="utf-8")
        result._group = _group(identity, result.source_path)
        return result

    def test_only_the_drawing_with_a_tsv_becomes_success(self):
        good = self._result("GOOD", self.root / "j1", TSV_HEADER + _tsv_row("GOOD"))
        missing = self._result("MISSING", self.root / "j2", None)
        combined = (
            f"{TitleBlockDiscoveryEngine.LOAD_MARKER}\n"
            f"{TitleBlockDiscoveryEngine.VERSION_MARKER}\n"
            f"{TitleBlockDiscoveryEngine.SUCCESS_MARKER}good.tsv\n"
        )
        self.engine._read(good, combined, 0.0, log_suffix=False)
        self.engine._read(missing, combined, 0.0, log_suffix=False)
        self.assertEqual(good.status, "SUCCESS")
        self.assertEqual(missing.status, "FAILED")
        self.assertEqual(missing.error, "DISCOVERY_TSV_NOT_CREATED")
        self.assertEqual(missing.fields, [])

    def test_empty_tsv_is_a_failure_not_an_empty_success(self):
        empty = self._result("EMPTY", self.root / "j3", TSV_HEADER + "\n" * 60)
        combined = f"{TitleBlockDiscoveryEngine.LOAD_MARKER}\n{TitleBlockDiscoveryEngine.VERSION_MARKER}\n{TitleBlockDiscoveryEngine.SUCCESS_MARKER}x\n"
        self.engine._read(empty, combined, 0.0, log_suffix=False)
        self.assertEqual(empty.status, "FAILED")
        self.assertEqual(empty.error, "DISCOVERY_TSV_HAS_NO_DATA_ROWS")

    def test_missing_lisp_banner_fails_the_drawing(self):
        good = self._result("NOLOAD", self.root / "j4", TSV_HEADER + _tsv_row("NOLOAD"))
        self.engine._read(good, "nothing useful\n", 0.0, log_suffix=False)
        self.assertEqual(good.status, "FAILED")
        self.assertEqual(good.error, "DISCOVERY_LISP_LOAD_FAILED")

    def test_killed_batch_marks_every_drawing_failed(self):
        """A dead process must not leave half a batch as SUCCESS."""
        groups = [_group("A", self._source("A.dwg")), _group("B", self._source("B.dwg"))]

        def boom(*args, **kwargs):
            raise subprocess.TimeoutExpired("accoreconsole", 1, output=b"", stderr=b"")

        with mock.patch.object(discovery_module, "run_console", boom):
            results = self.engine.discover_batch(groups)
        self.assertEqual([r.identity for r in results], ["A", "B"])
        self.assertTrue(all(r.status == "FAILED" for r in results))
        self.assertTrue(all("CORE_TIMEOUT" in r.error for r in results))

    def test_batch_results_keep_input_order(self):
        groups = [_group(i, self._source(i + ".dwg")) for i in ("C", "A", "B")]
        script = self.root / "produced.scr"

        def fake_run(command, **kwargs):
            # Simulate the real .scr: write one TSV per staged drawing, but on
            # purpose out of submission order to prove ordering is not assumed.
            jobfile = next(Path(kwargs["cwd"]).glob("discover_job.txt"))
            lines = [line for line in jobfile.read_text(encoding="ascii").splitlines() if line]
            for dwg, tsv in zip(lines[0::2], lines[1::2]):
                Path(tsv).write_text(TSV_HEADER + _tsv_row(Path(dwg).stem), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0,
                                               stdout=f"{TitleBlockDiscoveryEngine.LOAD_MARKER}\n"
                                                      f"{TitleBlockDiscoveryEngine.VERSION_MARKER}\n"
                                                      f"{TitleBlockDiscoveryEngine.SUCCESS_MARKER}x\n",
                                               stderr=b"")

        with mock.patch.object(discovery_module, "run_console", fake_run):
            results = self.engine.discover_batch(groups)
        self.assertEqual([r.identity for r in results], ["C", "A", "B"])
        self.assertTrue(all(r.status == "SUCCESS" for r in results))

    def test_batch_timeout_scales_with_batch_size(self):
        seen = {}

        def fake_run(command, **kwargs):
            seen["timeout"] = kwargs["timeout"]
            raise subprocess.TimeoutExpired(command, 1, output=b"", stderr=b"")

        self.engine.timeout = 7
        groups = [_group(i, self._source(i + ".dwg")) for i in ("A", "B", "C")]
        with mock.patch.object(discovery_module, "run_console", fake_run):
            self.engine.discover_batch(groups)
        self.assertEqual(seen["timeout"], 21)

    def test_each_batched_drawing_gets_its_own_tsv_and_folder(self):
        """A shared staging folder silently overwrote one TSV with the next.

        The batch still reported SUCCESS with plausible row counts, but three
        of four results carried another drawing's data. Paths must be unique.
        """
        seen = {}

        def fake_run(command, **kwargs):
            jobfile = next(Path(kwargs["cwd"]).glob("discover_job.txt"))
            lines = [line for line in jobfile.read_text(encoding="ascii").splitlines() if line]
            seen["pairs"] = list(zip(lines[0::2], lines[1::2]))
            for dwg, tsv in seen["pairs"]:
                # Stage folders are named `<batch>_<identity>`, the way the real
                # engine names them; DWGNAME is whatever `_.OPEN` loaded.
                identity = Path(dwg).parent.name.replace("_batch_batch", "").rsplit("_", 1)[-1]
                Path(tsv).write_text(TSV_HEADER + _tsv_row(identity), encoding="utf-8")
            return subprocess.CompletedProcess(
                command, 0,
                stdout=f"{TitleBlockDiscoveryEngine.LOAD_MARKER}\n"
                       f"{TitleBlockDiscoveryEngine.VERSION_MARKER}\n"
                       f"{TitleBlockDiscoveryEngine.SUCCESS_MARKER}x\n",
                stderr=b"")

        groups = [_group(i, self._source(i + ".dwg")) for i in ("A", "B", "C", "D")]
        with mock.patch.object(discovery_module, "run_console", fake_run):
            results = self.engine.discover_batch(groups)
        tsvs = [tsv for _, tsv in seen["pairs"]]
        self.assertEqual(len(set(tsvs)), 4, f"TSV paths collide: {tsvs}")
        self.assertEqual(len({Path(t).parent for t in tsvs}), 4, "drawings share a job folder")
        self.assertEqual(len({Path(d).parent for d, _ in seen["pairs"]}), 4, "staged DWGs share a folder")
        # Every result must describe its own drawing, not a sibling's.
        for result in results:
            self.assertEqual(result.status, "SUCCESS", result.error)
            self.assertEqual(result.fields[0].drawing_identity, result.identity)
            self.assertIn(result.identity + ".dwg", result.fields[0].drawing)


class ResumableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bundle = self.root / "BUNDLE"
        self.groups = []
        self.sources = {}
        for identity in ("A", "B"):
            path = self.root / (identity + ".dwg")
            path.write_bytes(b"DWG-" + identity.encode())
            self.sources[identity] = path
            self.groups.append(_group(identity, path))
        # A real instance: `_resumable` reads self.groups, so a bare __new__
        # object would pass for the wrong reason.
        self.worker = DiscoveryWorker(self.groups, "accoreconsole.exe", "dst", "BUNDLE",
                                      "src", SimpleNamespace(), Path("."))

    def _success(self, identity, source=None):
        return DiscoveryResult(identity, "GA", str(source or self.sources[identity]), "SUCCESS", "")

    def _write(self, results):
        DiscoveryWorker._write_checkpoint(self.bundle, results, len(results), False)
        return json.loads((self.bundle / "_GNI_REPORTS"
                           / "title_block_discovery_checkpoint.json").read_text(encoding="utf-8"))

    def test_checkpoint_records_source_hash_for_success_only(self):
        payload = self._write([
            self._success("A"),
            DiscoveryResult("B", "GA", str(self.sources["B"]), "FAILED", "", error="boom"),
        ])
        records = {item["identity"]: item for item in payload["results"]}
        self.assertIn("source_sha256", records["A"])
        self.assertNotIn("source_sha256", records["B"])

    def test_checkpoint_never_serialises_private_fields(self):
        result = self._success("A")
        result._group = self.groups[0]
        result._resumed = True
        payload = self._write([result])
        record = payload["results"][0]
        self.assertNotIn("_group", record)
        self.assertNotIn("_resumed", record)

    def test_unchanged_drawing_is_resumed_and_missing_one_is_not(self):
        self._write([self._success("A")])
        todo, accepted = self.worker._resumable(self.bundle)
        self.assertEqual([g.identity for g in accepted], ["A"])
        self.assertEqual([g.identity for g in todo], ["B"])
        self.assertIs(accepted[0]._resumed, True)

    def test_modified_source_is_discovered_again(self):
        self._write([self._success("A")])
        self.sources["A"].write_bytes(b"RE-SAVED")
        todo, accepted = self.worker._resumable(self.bundle)
        self.assertEqual(accepted, [])
        self.assertEqual([g.identity for g in todo], ["A", "B"])

    def test_failed_record_is_never_resumed(self):
        DiscoveryWorker._write_checkpoint(
            self.bundle,
            [DiscoveryResult("A", "GA", str(self.sources["A"]), "FAILED", "", error="x")],
            1, False,
        )
        todo, accepted = self.worker._resumable(self.bundle)
        self.assertEqual(accepted, [])
        self.assertEqual(len(todo), 2)

    def test_corrupt_checkpoint_falls_back_to_a_full_run(self):
        path = self.bundle / "_GNI_REPORTS" / "title_block_discovery_checkpoint.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        todo, accepted = self.worker._resumable(self.bundle)
        self.assertEqual(accepted, [])
        self.assertEqual(len(todo), 2)

    def test_resumed_fields_survive_the_round_trip(self):
        result = self._success("A")
        result.fields = [DiscoveryField(
            "A", "GA", str(self.sources["A"]), "A.dwg", "ATTRIB", "1A", "NEDERWIEK_DATA",
            "REV", "A", "0,0,0", "1B", "INSERT", "TITLE", "REVISION", "HIGH", 98, "TAG=REV",
            "A", "00", "",
        )]
        self._write([result])
        _, accepted = self.worker._resumable(self.bundle)
        self.assertEqual(len(accepted[0].fields), 1)
        self.assertEqual(accepted[0].fields[0].suggested_field, "REVISION")
        self.assertEqual(accepted[0].status, "SUCCESS")


class WorkerBatchingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _run_worker(self, groups, workers, batch_size):
        fake = _FakeEngine()
        settings = SimpleNamespace()
        worker = DiscoveryWorker(groups, "core.exe", "dst", "BUNDLE", "src", settings,
                                 Path("."), workers=workers, batch_size=batch_size)
        builder = mock.Mock()
        builder.bundle_root = "BUNDLE"
        builder_result = mock.Mock(bundle_root="BUNDLE")
        with mock.patch("runtime.production_workers.DestinationBuilder") as dest, \
                mock.patch("runtime.production_workers.TitleBlockDiscoveryEngine",
                           return_value=fake), \
                mock.patch.object(DiscoveryWorker, "_write_checkpoint") as checkpoint:
            dest.return_value.build.return_value = builder_result
            worker.run()
        return fake, worker, checkpoint

    def test_batch_size_groups_the_sweep(self):
        groups = [_group(f"G{i}", f"{i}.dwg") for i in range(10)]
        fake, worker, _ = self._run_worker(groups, workers=2, batch_size=4)
        self.assertEqual([len(call) for call in fake.calls], [4, 4, 2])
        self.assertEqual(sorted(i for call in fake.calls for i in call),
                         sorted(g.identity for g in groups))

    def test_batch_size_one_is_the_serial_path(self):
        groups = [_group(f"G{i}", f"{i}.dwg") for i in range(5)]
        fake, _, _ = self._run_worker(groups, workers=1, batch_size=1)
        self.assertEqual(len(fake.calls), 5)
        self.assertTrue(all(len(call) == 1 for call in fake.calls))

    def test_report_is_written_once_in_plan_order(self):
        groups = [_group(f"G{i}", f"{i}.dwg") for i in range(6)]
        fake, _, _ = self._run_worker(groups, workers=3, batch_size=2)
        self.assertEqual(len(fake.report_calls), 1)
        results, complete = fake.report_calls[0]
        self.assertIs(complete, True)
        self.assertEqual([r.identity for r in results], [g.identity for g in groups])

    def test_workers_and_batch_size_are_clamped(self):
        self.assertEqual(DiscoveryWorker([], "", "", "", "", None, ".").workers, 1)
        self.assertEqual(DiscoveryWorker([], "", "", "", "", None, ".", workers=-3).workers, 1)
        # An unset batch size means "use the measured default", not "disable
        # batching": a bare `or 1` would silently fall back to one process per
        # drawing, which is the cost this stage exists to avoid.
        self.assertEqual(DiscoveryWorker([], "", "", "", "", None, ".").batch_size,
                         DiscoveryWorker.DEFAULT_BATCH_SIZE)
        self.assertEqual(DiscoveryWorker([], "", "", "", "", None, ".", batch_size=0).batch_size, 1)
        self.assertEqual(DiscoveryWorker([], "", "", "", "", None, ".", batch_size=-2).batch_size, 1)


class ScriptContractTests(unittest.TestCase):
    """The generated `.scr` and the LISP job format must agree."""

    @classmethod
    def setUpClass(cls):
        cls.lsp = (Path(__file__).resolve().parents[1] / "lisp"
                   / "title_block_discovery.lsp").read_text(encoding="utf-8")

    def test_lisp_exposes_the_batch_command(self):
        self.assertIn("(defun GNI:DISCOVER-BATCH", self.lsp)
        self.assertIn("GNI_TITLEBLOCK_DISCOVERY_0_4A_3_BATCH", self.lsp)

    def test_lisp_never_saves_a_drawing(self):
        # Discovery is read-only: the staged copy must be opened, scanned and
        # closed, never written back. Check code, not the comment that says so.
        code = [line.split(";")[0] for line in self.lsp.splitlines()]
        code = "\n".join(code)
        for token in ("QSAVE", "SAVEAS", "_.SAVE"):
            self.assertNotIn(token, code)

    def test_script_disables_dialogs_and_loads_from_trusted_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TitleBlockDiscoveryEngine.__new__(TitleBlockDiscoveryEngine)
            engine.runtime_root = Path(tmp) / "trusted"
            engine.runtime_lisp = engine.runtime_root / "title_block_discovery.lsp"
            script = engine._write_script(Path(tmp), ['(GNI:DISCOVER-BATCH "job.txt")'])
            text = script.read_text(encoding="ascii")
        self.assertIn('(setvar "FILEDIA" 0)', text)
        self.assertIn('(setvar "CMDDIA" 0)', text)
        self.assertIn("GNI:DISCOVER-BATCH", text)
        self.assertIn("_.QUIT", text)
        self.assertNotIn("\\", text.split("TRUSTEDPATHS")[1].split(")")[0])

    def test_script_never_sets_sdi(self):
        # Measured on AutoCAD 2027 Core Console: `(setvar "SDI" 1)` is rejected
        # with "; error:" and the stage then aborts every drawing with
        # DISCOVERY_LISP_RUNTIME_ERROR. It also is not needed in Core Console.
        with tempfile.TemporaryDirectory() as tmp:
            engine = TitleBlockDiscoveryEngine.__new__(TitleBlockDiscoveryEngine)
            engine.runtime_root = Path(tmp) / "trusted"
            engine.runtime_lisp = engine.runtime_root / "title_block_discovery.lsp"
            text = engine._write_script(Path(tmp), ['(GNI:DISCOVER "x.tsv")']).read_text(encoding="ascii")
        self.assertNotIn('"SDI"', text)

    def test_lisp_is_staged_only_when_its_content_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "a.lsp"; dst = Path(tmp) / "b.lsp"
            src.write_text("one", encoding="utf-8")
            self.assertFalse(TitleBlockDiscoveryEngine._same_content(src, dst))
            TitleBlockDiscoveryEngine._copy(src, dst)
            self.assertTrue(TitleBlockDiscoveryEngine._same_content(src, dst))
            # A same-size edit must still be detected.
            src.write_text("two", encoding="utf-8")
            self.assertFalse(TitleBlockDiscoveryEngine._same_content(src, dst))


if __name__ == "__main__":
    unittest.main()
