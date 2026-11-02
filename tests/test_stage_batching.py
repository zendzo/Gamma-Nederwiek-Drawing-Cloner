"""Contracts of the batched stages (writer, validator, dry run, PDF).

Measured on the production machine: ``accoreconsole.exe`` needs ~4.5 s to boot
while one drawing's work is 0.5-2 s, so any stage that starts one process per
drawing spends most of its wall clock loading AutoCAD. Title-block discovery
already batched several drawings into one process; ``runtime/batching.py`` now
gives every stage that same strategy.

What must stay true, and is guarded here:

* ``batch_size <= 1`` keeps exactly the historical one-process-per-item path;
* one ``run_console`` call per batch (not per item) once batching is on;
* every item is judged by its *own* artefact, so a sibling's success can never
  promote an item that produced nothing;
* the generated ``.scr`` re-issues the header and ``(load ...)`` after every
  ``_.OPEN`` (AutoCAD drops LISP state on OPEN) and always ends with
  ``_.QUIT`` so no batch is left waiting on a prompt;
* results come back in input order, and a cancel stops *between* batches.
"""

import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from revision import pdf_publisher as pdf_module
from revision import reopen_validator as validator_module
from revision import stable_external_writer as writer_module
from revision.atomic_publish_dry_run import AtomicPublisherDryRun
from revision.pdf_publisher import PDF_PLOTTER_LOAD_MARKER, PdfPublisher
from revision.reopen_validator import StableReopenValidator
from revision.stable_external_writer import StableExternalActionWriter, StableOutput
from runtime.batching import (
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    batches,
    clamp_batch_size,
    run_batched,
)

PDF_BODY = b"%PDF-1.4\n1 0 obj\n<< /Type /Page >>\nendobj\n%%EOF\n"


def _sha(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _file(self, name, body=b"DWG"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path


class BatchingRulesTests(unittest.TestCase):
    def test_batches_partition_without_loss(self):
        items = list(range(10))
        chunks = batches(items, 4)
        self.assertEqual([len(chunk) for chunk in chunks], [4, 4, 2])
        self.assertEqual([item for chunk in chunks for item in chunk], items)

    def test_batch_size_of_one_or_less_is_the_serial_rule(self):
        for size in (1, 0, -5):
            self.assertEqual(batches([1, 2, 3], size), [[1], [2], [3]])

    def test_clamp_keeps_the_default_only_when_unset(self):
        self.assertEqual(clamp_batch_size(None, default=4), 4)
        self.assertEqual(clamp_batch_size(0), 1)
        self.assertEqual(clamp_batch_size(-3), 1)
        self.assertEqual(clamp_batch_size(99), MAX_BATCH_SIZE)
        self.assertEqual(clamp_batch_size("8"), 8)
        self.assertEqual(clamp_batch_size("junk", default=3), 3)
        # The shared defaults must stay sane numbers, not "whatever the UI sent".
        self.assertGreaterEqual(DEFAULT_BATCH_SIZE, 1)


class RunBatchedTests(unittest.TestCase):
    """The scheduling contract every stage now shares."""

    def test_batch_size_one_calls_the_item_function_once_per_item(self):
        calls = []
        results = run_batched(
            lambda item: calls.append([item]) or item,
            lambda chunk: self.fail("batch_function must not run"),
            ["a", "b", "c"], batch_size=1, workers=1,
        )
        self.assertEqual(calls, [["a"], ["b"], ["c"]])
        self.assertEqual(results, ["a", "b", "c"])

    def test_batch_size_groups_items_into_one_call_per_batch(self):
        calls = []
        results = run_batched(
            lambda item: self.fail("item_function must not run"),
            lambda chunk: calls.append(list(chunk)) or list(chunk),
            list("abcdefg"), batch_size=3, workers=1,
        )
        self.assertEqual(calls, [["a", "b", "c"], ["d", "e", "f"], ["g"]])
        self.assertEqual(results, list("abcdefg"))

    def test_results_keep_input_order_when_batches_finish_out_of_order(self):
        def work(chunk):
            time.sleep(0.05 if chunk[0] == "a" else 0.0)
            return [item.upper() for item in chunk]

        results = run_batched(lambda item: item.upper(), work, list("abcd"),
                              batch_size=2, workers=2)
        self.assertEqual(results, ["A", "B", "C", "D"])

    def test_cancel_stops_between_batches_and_keeps_completed_work(self):
        seen = []

        def work(chunk):
            return list(chunk)

        results = run_batched(
            lambda item: item, work, list(range(6)),
            batch_size=2, workers=1,
            on_result=seen.append,
            should_cancel=lambda: len(seen) >= 2,
        )
        # The in-flight batch always finishes; nothing new is launched.
        self.assertEqual(results, [0, 1])
        self.assertEqual(seen, [0, 1])

    def test_a_fully_empty_run_is_not_an_error(self):
        self.assertEqual(run_batched(len, len, [], batch_size=4, workers=3), [])


class WriterBatchTests(_TmpCase):
    """One process, several plans: every output is judged by its own TSV."""

    def setUp(self):
        super().setUp()
        self.writer = object.__new__(StableExternalActionWriter)
        self.writer.core = Path("accoreconsole.exe")
        self.writer.timeout = 5
        self.writer.job_root = self.root / "jobs"
        self.writer.runtime_root = self.root / "trusted"
        self.writer.runtime_lisp = self.writer.runtime_root / "writer.lsp"
        self.writer.job_root.mkdir(parents=True)
        self.writer.runtime_root.mkdir(parents=True)
        self.writer.runtime_lisp.write_text(";; lsp\n", encoding="utf-8")
        self.writer.target_layer = "NEDERWIEK_DATA"
        self.writer.target_color = 6
        self.writer.source_layers = {"other"}
        self.plans = [self._plan(name) for name in ("ID1", "ID2", "ID3")]

    def _plan(self, identity):
        source = self._file(identity + ".dwg", ("DWG-" + identity).encode())
        return {
            "identity": identity,
            "drawing_type": "GA",
            "primary_geometry": str(source),
            "actions": [
                {
                    "output_revision": mode, "handle": "1A", "action": "WRITE",
                    "new_value": identity, "layer": "Other",
                    "entity_type": "ATTRIB", "tag": "REV",
                    "target_field": "REV", "reason": "TEST",
                }
                for mode in ("A", "00")
            ],
        }

    @staticmethod
    def _calls(script):
        text = Path(script).read_text(encoding="ascii")
        return re.findall(
            r'\(GNI:RUN-EXTERNAL-WRITER "[^"]+" "([^"]+)" "([^"]+)"', text
        )

    def _fake_console(self, skip=(), seen=None):
        """Emulate the LISP writer: one result TSV per invoked output."""

        def fake(command, **kwargs):
            script = Path(command[command.index("/s") + 1])
            if seen is not None:
                seen.append(script)
            for actions, result in self._calls(script):
                if any(token in actions for token in skip):
                    continue
                rows = len(Path(actions).read_text(encoding="cp1252").splitlines()) - 1
                Path(result).write_text(
                    "Action\tStatus\n" + "WRITE\tWRITTEN\n" * rows,
                    encoding="utf-8",
                )
            return subprocess.CompletedProcess(
                command, 0,
                stdout=(StableExternalActionWriter.LOAD_MARKER + "\n").encode("ascii"),
                stderr=b"",
            )

        return fake

    def test_one_process_writes_every_output_of_a_batch(self):
        seen = []
        with mock.patch.object(writer_module, "run_console", self._fake_console(seen=seen)):
            results = self.writer.run_batch(self.plans)
        self.assertEqual(len(seen), 1, "batching must share one Core Console process")
        self.assertEqual([item.identity for item in results], ["ID1", "ID2", "ID3"])
        self.assertTrue(all(item.status == "LOCAL_PAIR_WRITTEN_NOT_VALIDATED"
                            for item in results), [item.errors for item in results])

    def test_serial_path_starts_one_process_per_output(self):
        seen = []
        with mock.patch.object(writer_module, "run_console", self._fake_console(seen=seen)):
            self.writer.run_many(self.plans, workers=1, batch_size=1)
        # Two outputs per plan, one process each: the cost batching removes.
        self.assertEqual(len(seen), 6)

    def test_every_output_keeps_its_own_folder_and_action_file(self):
        seen = []
        with mock.patch.object(writer_module, "run_console", self._fake_console(seen=seen)):
            results = self.writer.run_batch(self.plans)
        pairs = [result for result in results]
        action_files = [output.action_file for pair in pairs for output in pair.outputs]
        self.assertEqual(len(set(action_files)), 6)
        self.assertEqual(len({Path(f).parent for f in action_files}), 3, "A and 00 share a plan folder")
        self.assertEqual(len({output.working_dwg for pair in pairs for output in pair.outputs}), 6)

    def test_a_missing_result_tsv_fails_only_that_output(self):
        with mock.patch.object(writer_module, "run_console",
                               self._fake_console(skip=("/ID2/00_actions",))):
            results = self.writer.run_batch(self.plans)
        by_identity = {item.identity: item for item in results}
        # The sibling pair succeeded in the same process; ID2 must not inherit it.
        self.assertEqual(by_identity["ID1"].status, "LOCAL_PAIR_WRITTEN_NOT_VALIDATED")
        self.assertEqual(by_identity["ID3"].status, "LOCAL_PAIR_WRITTEN_NOT_VALIDATED")
        self.assertEqual(by_identity["ID2"].status, "FAILED")
        self.assertTrue(any("STABLE_RESULT_MISSING" in error
                            for error in by_identity["ID2"].errors),
                        by_identity["ID2"].errors)

    def test_batch_script_reopens_each_drawing_and_quits(self):
        seen = []
        with mock.patch.object(writer_module, "run_console", self._fake_console(seen=seen)):
            results = self.writer.run_batch(self.plans)
        text = seen[0].read_text(encoding="ascii")
        outputs = [output for pair in results for output in pair.outputs]
        # The first drawing arrives through `/i`; the rest are opened by script.
        self.assertEqual(text.count("_.OPEN"), len(outputs) - 1)
        self.assertEqual(text.count('(load "'), len(outputs))
        self.assertIn("_.QUIT", text)
        # A bare `_.OPEN` script line splits on spaces, and every published
        # drawing lives under `1. SP/2. AS/3. GA`.
        self.assertNotIn("\n_.OPEN\n", text)
        self.assertEqual(text.count('(command-s "_.OPEN" "'), len(outputs) - 1)
        self.assertGreater(text.index("_.OPEN"), text.index('(load "'))
        self.assertGreater(text.rindex('(load "'), text.index("_.OPEN"))
        # `_.OPEN` on a dirty drawing would prompt and swallow the batch; the
        # guard also runs once before `_.QUIT`.
        self.assertEqual(text.count('"DBMOD"'), text.count("_.OPEN") + 1)

    def test_batch_timeout_is_the_full_per_drawing_allowance(self):
        seen = {}

        def fake(command, **kwargs):
            seen["timeout"] = kwargs["timeout"]
            return self._fake_console()(command, **kwargs)

        with mock.patch.object(writer_module, "run_console", fake):
            self.writer.run_batch(self.plans)
        self.assertEqual(seen["timeout"], 5 * 6)

    def test_source_drawings_are_never_written(self):
        before = {plan["identity"]: _sha(plan["primary_geometry"]) for plan in self.plans}
        with mock.patch.object(writer_module, "run_console", self._fake_console()):
            self.writer.run_batch(self.plans)
        after = {plan["identity"]: _sha(plan["primary_geometry"]) for plan in self.plans}
        self.assertEqual(before, after)


class WriterRasterMapTests(_TmpCase):
    """0.4C.32: the writer tells LISP the packaged PNG sizes before QSAVE.

    Older Gamma drawings stored a 922x260 Tennet IMAGEDEF while the packaged
    PNG is 1136x305. AutoCAD renders at the file size, so the logo grew ~23%
    and overlapped the SEAtrium logo. The writer's working copy has no PNG next
    to it, so it still sees the original 922 and can rescale to preserve the
    drawn footprint.
    """

    def _writer(self):
        writer = object.__new__(StableExternalActionWriter)
        writer.application_root = Path(__file__).resolve().parents[1]
        writer.target_layer = "NEDERWIEK_DATA"
        writer.target_color = 6
        writer.source_layers = {"other"}
        writer.job_root = self.root / "jobs"
        writer.runtime_root = self.root / "trusted"
        writer.runtime_lisp = writer.runtime_root / "writer.lsp"
        writer.job_root.mkdir(parents=True)
        writer.runtime_root.mkdir(parents=True)
        writer.runtime_lisp.write_text(";; lsp\n", encoding="utf-8")
        return writer

    def test_raster_map_rows_read_the_packaged_png_dimensions(self):
        rows = self._writer()._raster_map_rows("GA")
        by_name = {row.split("\t")[0]: row.split("\t")[1:] for row in rows}
        self.assertEqual(by_name["SEAtrium_Ge_.PNG"], ["1235", "290"])
        self.assertEqual(by_name["Tennet LogO New.PNG"], ["1136", "305"])

    def test_staged_output_writes_the_raster_map_and_passes_it_to_lisp(self):
        writer = self._writer()
        source = self._file("ID1.dwg", b"DWG")
        plan = {
            "identity": "ID1",
            "drawing_type": "GA",
            "primary_geometry": str(source),
            "actions": [
                {"output_revision": mode, "handle": "1A", "action": "VERIFY_ONLY",
                 "expected_old": "", "entity_type": "ATTRIB", "tag": "REV"}
                for mode in ("A", "00")
            ],
        }
        job = writer._new_job("ID1")
        output = writer._stage_output(plan, "A", job)
        map_path = Path(output.job_folder) / "raster_map.tsv"
        self.assertTrue(map_path.is_file(), "raster map must be staged next to the job")
        self.assertIn("Tennet LogO New.PNG\t1136\t305", map_path.read_text(encoding="ascii"))
        call = writer._writer_call(output)
        self.assertIn("raster_map.tsv", call)


class ValidatorBatchTests(_TmpCase):
    def setUp(self):
        super().setUp()
        self.validator = object.__new__(StableReopenValidator)
        self.validator.core = Path("accoreconsole.exe")
        self.validator.timeout = 5
        self.validator.validation_root = self.root / "validation"
        self.validator.runtime_root = self.root / "trusted"
        self.validator.runtime_lisp = self.validator.runtime_root / "validator.lsp"
        self.validator.validation_root.mkdir(parents=True)
        self.validator.runtime_root.mkdir(parents=True)
        self.validator.runtime_lisp.write_text(";; lsp\n", encoding="utf-8")

    def _pair(self, identity):
        outputs = []
        for mode in ("A", "00"):
            # Distinct content: the validator rejects identical A/00 hashes.
            dwg = self._file(f"{identity}/{mode}.dwg",
                             (identity + "-" + mode).encode())
            actions = self._file(f"{identity}/{mode}_actions.tsv",
                                 b"Handle\tAction\n1A\tWRITE\n")
            outputs.append({
                "output_revision": mode,
                "working_dwg": str(dwg),
                "action_file": str(actions),
                "sha256": _sha(dwg),
            })
        return {"identity": identity, "drawing_type": "GA", "outputs": outputs}

    @staticmethod
    def _calls(script):
        return re.findall(
            r'\(GNI:RUN-REOPEN-VALIDATOR "[^"]+" "([^"]+)" "([^"]+)"\)',
            Path(script).read_text(encoding="ascii"),
        )

    def _fake_console(self, skip=(), seen=None):
        def fake(command, **kwargs):
            script = Path(command[command.index("/s") + 1])
            if seen is not None:
                seen.append(script)
            for actions, result in self._calls(script):
                if any(token in result for token in skip):
                    continue
                rows = len(Path(actions).read_text(encoding="cp1252").splitlines()) - 1
                Path(result).write_text("Check\tStatus\n" + "HASH\tPASS\n" * rows,
                                        encoding="utf-8")
            return subprocess.CompletedProcess(
                command, 0,
                stdout=(StableReopenValidator.LOAD_MARKER + "\n").encode("ascii"),
                stderr=b"",
            )

        return fake

    def test_a_batch_is_validated_in_one_process(self):
        seen = []
        pairs = [self._pair("ID1"), self._pair("ID2"), self._pair("ID3")]
        with mock.patch.object(validator_module, "run_console",
                               self._fake_console(seen=seen)):
            results = self.validator.validate_batch(pairs)
        self.assertEqual(len(seen), 1)
        self.assertEqual([item.identity for item in results], ["ID1", "ID2", "ID3"])
        self.assertTrue(all(item.status == "VALIDATED" for item in results),
                        [item.errors for item in results])

    def test_a_missing_validation_tsv_fails_only_that_pair(self):
        pairs = [self._pair("ID1"), self._pair("ID2")]
        with mock.patch.object(validator_module, "run_console",
                               self._fake_console(skip=("ID2/00",))):
            results = self.validator.validate_batch(pairs)
        self.assertEqual(results[0].status, "VALIDATED")
        self.assertEqual(results[1].status, "FAILED")
        self.assertTrue(any("VALIDATION_RESULT_MISSING" in error
                            for error in results[1].errors), results[1].errors)


class DryRunBatchTests(_TmpCase):
    """The dry run has no Core Console, so only the grouping is under test."""

    def _engine(self):
        bundle = self.root / "BUNDLE"
        bundle.mkdir()
        engine = AtomicPublisherDryRun(bundle, self.root / "app")
        engine._inspect_assets = lambda drawing_type: []
        return engine

    def _pair(self, identity):
        files = {}
        for mode in ("A", "00"):
            path = self._file(f"{identity}_{mode}.dwg", (identity + mode).encode())
            files[mode] = path
        return {
            "identity": identity,
            "drawing_type": "GA",
            "writer": {"outputs": [
                {"output_revision": mode, "working_dwg": str(files[mode])}
                for mode in ("A", "00")
            ]},
            "validation": {"outputs": [
                {"output_revision": mode, "sha256_after": _sha(files[mode])}
                for mode in ("A", "00")
            ]},
        }

    def test_inspect_many_groups_pairs_and_keeps_input_order(self):
        engine = self._engine()
        calls = []
        original = engine.inspect_pair
        engine.inspect_pair = lambda pair: calls.append(pair["identity"]) or original(pair)
        pairs = [self._pair(name) for name in ("ID1", "ID2", "ID3", "ID4", "ID5")]
        results = engine.inspect_many(pairs, workers=1, batch_size=2)
        self.assertEqual(calls, ["ID1", "ID2", "ID3", "ID4", "ID5"])
        self.assertEqual([item.identity for item in results], calls)

    def test_batch_size_one_inspects_one_pair_per_call(self):
        engine = self._engine()
        calls = []
        engine.inspect_batch = lambda pairs: self.fail("batch path must not run")
        original = engine.inspect_pair
        engine.inspect_pair = lambda pair: calls.append(pair["identity"]) or original(pair)
        engine.inspect_many([self._pair("ID1"), self._pair("ID2")], workers=1, batch_size=1)
        self.assertEqual(calls, ["ID1", "ID2"])


class PdfBatchTests(_TmpCase):
    """A whole batch of plots in one process, one commit/rollback unit per pair."""

    def setUp(self):
        super().setUp()
        self.bundle = self.root / "BUNDLE"
        self.publisher = object.__new__(PdfPublisher)
        self.publisher.core = Path("accoreconsole.exe")
        self.publisher.bundle = self.bundle
        self.publisher.timeout = 5
        self.publisher.staging = self.bundle / "_GNI_STAGING"
        self.publisher.backup = self.bundle / "_GNI_BACKUP"
        self.publisher.staging.mkdir(parents=True)
        self.publisher.backup.mkdir(parents=True)
        self.publisher.runtime = self.root / "trusted"
        self.publisher.runtime_lisp = self.publisher.runtime / "pdf.lsp"
        self.publisher.runtime.mkdir(parents=True)
        self.publisher.runtime_lisp.write_text(";; lsp\n", encoding="utf-8")
        self.publisher.settings = {
            "plotter_candidates": ["DWG To PDF.pc3"],
            "preferred_media_names": ["ISO A1 (841.00 x 594.00 MM)"],
            "plot_style": "monochrome.ctb",
            "plot_rotation": 0,
            "minimum_pdf_bytes": 16,
        }
        self.publisher.config_signature = "sig"
        self.publisher.previous_signature = "sig"
        self.publisher.previous_ok = True
        self.publisher.previous = {}

    def _tx(self, identity):
        files = []
        for mode in ("A", "00"):
            path = self._file(f"{identity}_{mode}.dwg", (identity + mode).encode())
            files.append({"output_revision": mode, "destination": str(path)})
        return {"identity": identity, "drawing_type": "GA", "files": files}

    @staticmethod
    def _calls(script):
        return re.findall(
            r'\(GNI:RUN-PDF-PLOTTER "([^"]+)" "([^"]+)"',
            Path(script).read_text(encoding="ascii"),
        )

    def _fake_console(self, skip=(), seen=None):
        def fake(command, **kwargs):
            script = Path(command[command.index("/s") + 1])
            if seen is not None:
                seen.append(script)
            for pdf, result in self._calls(script):
                if any(token in pdf for token in skip):
                    continue
                Path(result).write_text("Status\tReason\nOK\tplotted\n",
                                        encoding="utf-8")
                Path(pdf).parent.mkdir(parents=True, exist_ok=True)
                Path(pdf).write_bytes(PDF_BODY)
            return subprocess.CompletedProcess(
                command, 0,
                stdout=(PDF_PLOTTER_LOAD_MARKER + "\n").encode("ascii"),
                stderr=b"",
            )

        return fake

    def test_a_batch_is_plotted_in_one_process(self):
        seen = []
        txs = [self._tx("ID1"), self._tx("ID2")]
        with mock.patch.object(pdf_module, "run_console", self._fake_console(seen=seen)):
            results = self.publisher.publish_batch(txs)
        self.assertEqual(len(seen), 1)
        self.assertEqual([item.identity for item in results], ["ID1", "ID2"])
        self.assertTrue(all(item.status == "PDF_PAIR_PUBLISHED" for item in results),
                        [item.errors for item in results])
        self.assertTrue((self.bundle / "A" / "3. GA" / "PDF" / "ID1_A.pdf").is_file())
        self.assertTrue((self.bundle / "00" / "3. GA" / "PDF" / "ID2_00.pdf").is_file())

    def test_a_missing_plot_tsv_fails_only_that_pair(self):
        txs = [self._tx("ID1"), self._tx("ID2")]
        with mock.patch.object(pdf_module, "run_console",
                               self._fake_console(skip=("ID2_00.pdf",))):
            results = self.publisher.publish_batch(txs)
        self.assertEqual(results[0].status, "PDF_PAIR_PUBLISHED")
        self.assertEqual(results[1].status, "PDF_PUBLISH_FAILED_ROLLED_BACK")
        # The failing pair must not leave a half-published PDF behind.
        self.assertFalse((self.bundle / "00" / "3. GA" / "PDF" / "ID2_00.pdf").is_file())

    def test_pdf_batch_script_reopens_each_drawing_and_quits(self):
        seen = []
        txs = [self._tx("ID1"), self._tx("ID2")]
        with mock.patch.object(pdf_module, "run_console", self._fake_console(seen=seen)):
            self.publisher.publish_batch(txs)
        text = seen[0].read_text(encoding="ascii")
        # Four plots (A and 00 of two pairs) in one process.
        self.assertEqual(text.count("GNI:RUN-PDF-PLOTTER"), 4)
        self.assertEqual(text.count("_.OPEN"), 3)
        self.assertEqual(text.count('(load "'), 4)
        self.assertIn("_.QUIT", text)
        self.assertNotIn("\n_.OPEN\n", text)
        self.assertEqual(text.count('(command-s "_.OPEN" "'), 3)

    def test_open_line_keeps_a_path_with_spaces_intact(self):
        # `1. SP` / `2. AS` / `3. GA` guarantee spaces in a staging copy path;
        # a bare OPEN line used to be cut there and hung the batch.
        line = PdfPublisher._open_line(r"C:\bundle\_GNI_STAGING\2. AS\ID_A.dwg")
        self.assertIn('(command-s "_.OPEN" "C:/bundle/_GNI_STAGING/2. AS/ID_A.dwg")', line)
        self.assertEqual(
            line.count('"') % 2, 0, "unbalanced quotes would break the script"
        )

    def test_plot_runs_on_a_staged_copy_not_the_published_dwg(self):
        """`-PLOT` marks the drawing modified; the dirty guard then QSAVEs it.

        When the plot ran straight on the delivered DWG that save rewrote a
        published file, so its SHA-256 no longer matched the validated hash the
        publish report recorded. Measured on a real run: 1 of 12 published DWGs
        changed. The plot must always work on a copy.
        """
        tx = self._tx("ID1")
        published = Path(tx["files"][0]["destination"]).resolve()
        before = _sha(published)
        stage = self.root / "stage"
        stage.mkdir()
        job = self.publisher._stage_plot("ID1", "GA", "A", published, stage,
                                         source_sha=before)
        self.assertNotEqual(Path(job.source).resolve(), published)
        self.assertEqual(Path(job.source).read_bytes(), published.read_bytes())
        self.assertEqual(Path(job.source).parent.name, "A")
        # The report still names the delivered file and its hash.
        self.assertEqual(job.output.source_dwg, str(published))
        self.assertEqual(job.output.source_sha256, before)
        self.assertEqual(_sha(published), before)

    def test_staged_plot_copies_reference_rasters_for_relative_image_paths(self):
        """The logos attach by bare filename, so the staged copy must carry them.

        Measured on the production machine: the DWG's IMAGE entities point at
        "SEAtrium_Ge_.PNG" / "Tennet LogO New.PNG" (relative paths). -PLOT runs
        on the staged DWG copy, and with no PNG next to it the raster resolves
        nowhere - the SEAtrium/Tennet logos silently vanish from the PDF.
        """
        cad = self.root / "cad"
        cad.mkdir()
        published = cad / "ID1_A.dwg"
        published.write_bytes(b"DWG")
        for name in PdfPublisher.ASSET_NAMES:
            (cad / name).write_bytes(b"PNG-" + name.encode("ascii"))
        stage = self.root / "stage"
        stage.mkdir()
        job = self.publisher._stage_plot("ID1", "GA", "A", str(published), stage)
        staged = Path(job.source).parent
        self.assertEqual(
            sorted(path.name for path in staged.glob("*.PNG")),
            list(PdfPublisher.ASSET_NAMES),
        )
        self.assertEqual(
            (staged / "SEAtrium_Ge_.PNG").read_bytes(),
            (cad / "SEAtrium_Ge_.PNG").read_bytes(),
        )


class PdfStagingLocationTests(unittest.TestCase):
    """Core Console cancels `/s` scripts served from a network share."""

    def test_scripts_and_copies_are_staged_on_local_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "BUNDLE"
            bundle.mkdir()
            core = Path(tmp) / "accoreconsole.exe"
            core.write_bytes(b"MZ")
            app = Path(__file__).resolve().parents[1]
            publisher = PdfPublisher(core, bundle, app)
            self.assertFalse(
                Path(publisher.staging).is_relative_to(bundle.resolve()),
                "staging must not live on the bundle share: " + str(publisher.staging),
            )
            # The plot work can be regenerated, so it must not bloat the bundle;
            # backups and reports must stay next to the drawings.
            self.assertEqual(Path(publisher.backup), bundle.resolve() / "_GNI_BACKUP")
            self.assertEqual(Path(publisher.reports), bundle.resolve() / "_GNI_REPORTS")


if __name__ == "__main__":
    unittest.main()
