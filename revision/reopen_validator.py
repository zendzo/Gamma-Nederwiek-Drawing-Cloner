from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
import csv
import json
import os
import subprocess
import tempfile
import time
import uuid
from typing import List

from runtime.batching import clamp_batch_size, run_batched
from runtime.atomic_io import atomic_replace
from runtime.console_text import decode_console
from runtime.hashing import sha256_file
from runtime.process_tracking import run_console

# 0.4C.26: one Core Console process reopens a whole batch of written pairs
# instead of one process per output.
VALIDATOR_VERSION = "0.4C.26-batched-validator"


class ReopenValidationError(RuntimeError):
    pass


@dataclass
class ValidationOutput:
    identity: str
    drawing_type: str
    output_revision: str
    status: str
    working_dwg: str
    action_file: str
    result_tsv: str = ""
    log_path: str = ""
    checks_requested: int = 0
    checks_passed: int = 0
    checks_failed: int = 0
    sha256_before: str = ""
    sha256_after: str = ""
    elapsed_seconds: float = 0.0
    exit_code: int | None = None
    error: str = ""


@dataclass
class ValidationPair:
    identity: str
    drawing_type: str
    status: str
    outputs: List[ValidationOutput] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class StableReopenValidator:
    LOAD_MARKER = "GNI_REOPEN_VALIDATOR_0_4C_1_LOADED"
    SUCCESS_MARKER = "GNI_REOPEN_VALIDATOR_OK:"
    FAILURE_MARKER = "GNI_REOPEN_VALIDATOR_FAILED:"
    EXIT_MARKER = "GNI_REOPEN_VALIDATOR_EXIT_READY"

    def __init__(self, core_console, lisp_source, application_root, timeout=180):
        self.core = Path(core_console).resolve()
        self.lisp_source = Path(lisp_source).resolve()
        self.application_root = Path(application_root).resolve()
        self.timeout = int(timeout)
        if not self.core.is_file():
            raise FileNotFoundError(f"accoreconsole.exe not found: {self.core}")
        if not self.lisp_source.is_file():
            raise FileNotFoundError(f"Reopen validator LSP not found: {self.lisp_source}")
        temp = Path(os.environ.get("TEMP") or tempfile.gettempdir()).resolve()
        self.runtime_root = temp / "GNI" / "trusted_runtime"
        self.validation_root = temp / "GNI" / "reopen_validator"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.validation_root.mkdir(parents=True, exist_ok=True)
        self.runtime_lisp = self.runtime_root / "reopen_validator.lsp"
        self.runtime_lisp.write_bytes(self.lisp_source.read_bytes())

    # Shared hasher: see runtime/hashing.py.
    _sha256 = staticmethod(sha256_file)

    # Shared decoder: see runtime/console_text.py (UTF-16LE without BOM).
    _decode = staticmethod(decode_console)

    @staticmethod
    def _scr_path(path):
        return str(Path(path).resolve()).replace("\\", "/").replace('"', '\\"')

    @classmethod
    def load_writer_results(cls, report_path, return_rejected=False):
        """Return validatable writer pairs, reporting rejected ones separately.

        Previously any rejected pair raised and blocked the whole validation
        batch, so one failed drawing stopped every other drawing from advancing.
        """
        path = Path(report_path)
        if not path.is_file():
            raise ReopenValidationError(f"STABLE_WRITER_REPORT_NOT_FOUND:{path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        accepted = []
        rejected = []
        for pair in payload.get("results", []):
            if pair.get("status") != "LOCAL_PAIR_WRITTEN_NOT_VALIDATED":
                rejected.append({"identity": pair.get("identity", ""), "errors": [str(pair.get("status", ""))]})
                continue
            outputs = pair.get("outputs", [])
            if {item.get("output_revision") for item in outputs} != {"A", "00"}:
                rejected.append({"identity": pair.get("identity", ""), "errors": ["PAIR_OUTPUTS_INCOMPLETE"]})
                continue
            accepted.append(pair)
        if not accepted:
            detail = ";".join(item["identity"] + "=" + ",".join(item["errors"]) for item in rejected)
            raise ReopenValidationError(
                "WRITER_RESULTS_NOT_VALIDATABLE:" + detail if rejected
                else "NO_LOCAL_PAIRS_TO_VALIDATE"
            )
        if return_rejected:
            return accepted, rejected
        return accepted

    @staticmethod
    def _count_actions(path):
        with Path(path).open("r", encoding="cp1252", errors="replace", newline="") as stream:
            return sum(1 for _ in csv.DictReader(stream, delimiter="\t"))

    def _stage_output(self, pair, source_output, session):
        """Prepare one output for validation (no Core Console yet)."""
        mode = source_output["output_revision"]
        dwg = Path(source_output["working_dwg"]).resolve()
        actions = Path(source_output["action_file"]).resolve()
        output = ValidationOutput(
            pair["identity"], pair["drawing_type"], mode, "PROCESSING",
            str(dwg), str(actions),
            str(session / f"{mode}_validation.tsv"),
            str(session / f"{mode}_validation.log"),
        )
        try:
            if not dwg.is_file():
                raise ReopenValidationError("WORKING_DWG_NOT_FOUND")
            if not actions.is_file():
                raise ReopenValidationError("ACTION_FILE_NOT_FOUND")
            output.checks_requested = self._count_actions(actions)
            output.sha256_before = self._sha256(dwg)
            expected_hash = str(source_output.get("sha256", ""))
            if expected_hash and output.sha256_before != expected_hash:
                raise ReopenValidationError("PRE_VALIDATION_HASH_MISMATCH")
            self._write_script(session, mode, actions, Path(output.result_tsv))
        except Exception as error:
            output.status = "FAILED"
            output.error = repr(error)
        return output

    def _write_script(self, session, mode, actions, result_tsv):
        script = Path(session) / f"{mode}_validation.scr"
        script.write_text("\n".join([
            '(setvar "FILEDIA" 0)',
            '(setvar "CMDDIA" 0)',
            '(if (getvar "REPORTERROR") (setvar "REPORTERROR" 0))',
            f'(setvar "TRUSTEDPATHS" "{self._scr_path(self.runtime_root)}")',
            f'(load "{self._scr_path(self.runtime_lisp)}")',
            f'(GNI:RUN-REOPEN-VALIDATOR "{mode}" "{self._scr_path(actions)}" '
            f'"{self._scr_path(result_tsv)}")',
            "",
        ]), encoding="ascii", errors="replace")
        return script

    def _script_header(self):
        return [
            '(setvar "FILEDIA" 0)',
            '(setvar "CMDDIA" 0)',
            '(if (getvar "REPORTERROR") (setvar "REPORTERROR" 0))',
            f'(setvar "TRUSTEDPATHS" "{self._scr_path(self.runtime_root)}")',
            f'(load "{self._scr_path(self.runtime_lisp)}")',
        ]

    @staticmethod
    def _call(mode, actions, result_tsv):
        clean = str(Path(actions).resolve()).replace("\\", "/").replace('"', '\\"')
        target = str(Path(result_tsv).resolve()).replace("\\", "/").replace('"', '\\"')
        return f'(GNI:RUN-REOPEN-VALIDATOR "{mode}" "{clean}" "{target}")'

    @staticmethod
    def _dirty_guard():
        """Never let a dirty drawing turn `_.OPEN` into a batch-killing prompt.

        Validation must not modify the working DWG (that is one of its checks),
        so this only fires when something already went wrong - and then saving
        the file makes the hash check below fail that output instead of hanging
        the rest of the batch.
        """
        return (
            '(if (/= 0 (getvar "DBMOD")) '
            '(progn (princ "\\nGNI_BATCH_DIRTY_DOCUMENT_SAVED") '
            '(vl-catch-all-apply (quote command-s) (list "_.QSAVE"))))'
        )

    @staticmethod
    def _open_line(path):
        """Open the next drawing from LISP so spaces in the path survive.

        Same reason as the writer: a bare ``_.OPEN`` line is truncated at the
        first space, and Core Console then blocks on the filename prompt until
        the batch timeout.
        """
        target = str(Path(path).resolve()).replace("\\", "/").replace('"', '\\"')
        return ('(vl-catch-all-apply (quote (lambda () '
                '(command-s "_.OPEN" "' + target + '"))))')

    def _verify_output(self, output, combined, exit_code, started, strict):
        """Judge one output by its own validation TSV, not by shared console text."""
        result_tsv = Path(output.result_tsv)
        dwg = Path(output.working_dwg)
        output.exit_code = exit_code
        try:
            if self.LOAD_MARKER not in combined:
                raise ReopenValidationError("VALIDATOR_LISP_LOAD_FAILED")
            if strict and self.FAILURE_MARKER in combined:
                raise ReopenValidationError("VALIDATION_CHECK_FAILED")
            if strict and (
                self.SUCCESS_MARKER not in combined
                or self.EXIT_MARKER not in combined
            ):
                raise ReopenValidationError("VALIDATION_SUCCESS_MARKER_MISSING")
            if not result_tsv.is_file():
                raise ReopenValidationError("VALIDATION_RESULT_MISSING")

            with result_tsv.open("r", encoding="utf-8-sig", errors="replace",
                                 newline="") as stream:
                rows = list(csv.DictReader(stream, delimiter="\t"))
            output.checks_passed = sum(row.get("Status") == "PASS" for row in rows)
            output.checks_failed = sum(row.get("Status") == "FAIL" for row in rows)
            if output.checks_failed:
                raise ReopenValidationError(f"VALIDATION_FAILURES:{output.checks_failed}")
            if len(rows) != output.checks_requested:
                raise ReopenValidationError(
                    f"VALIDATION_COUNT_MISMATCH:{len(rows)}:{output.checks_requested}"
                )
            output.sha256_after = self._sha256(dwg)
            if output.sha256_after != output.sha256_before:
                raise ReopenValidationError("VALIDATOR_MODIFIED_DWG")
            output.status = "VALIDATED"
        except subprocess.TimeoutExpired:
            output.status = "FAILED"
            output.error = f"VALIDATION_TIMEOUT:{self.timeout}"
        except Exception as error:
            output.status = "FAILED"
            output.error = repr(error)
            if dwg.is_file():
                output.sha256_after = self._sha256(dwg)
        output.elapsed_seconds = round(time.time() - started, 3)
        return output

    def _validate_output(self, pair, source_output, session):
        started = time.time()
        output = self._stage_output(pair, source_output, session)
        if output.status == "FAILED":
            output.elapsed_seconds = round(time.time() - started, 3)
            return output
        script = Path(session) / f"{output.output_revision}_validation.scr"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            process = run_console(
                [str(self.core), "/i", str(output.working_dwg), "/s", str(script),
                 "/l", "en-US"],
                capture_output=True, text=False, timeout=self.timeout,
                cwd=str(session), creationflags=flags,
            )
        except subprocess.TimeoutExpired:
            Path(output.log_path).write_text(
                f"VERSION={VALIDATOR_VERSION}\nSTATUS=FAILED\n"
                f"ERROR=VALIDATION_TIMEOUT:{self.timeout}\n",
                encoding="utf-8", errors="replace",
            )
            output.status = "FAILED"
            output.error = f"VALIDATION_TIMEOUT:{self.timeout}"
            output.elapsed_seconds = round(time.time() - started, 3)
            return output
        combined = self._decode(process.stdout) + "\n" + self._decode(process.stderr)
        Path(output.log_path).write_text(
            f"VERSION={VALIDATOR_VERSION}\nEXIT_CODE={process.returncode}\n{combined}",
            encoding="utf-8", errors="replace",
        )
        return self._verify_output(
            output, combined, process.returncode, started, strict=True
        )

    @staticmethod
    def _finish_pair(result):
        failed = [item for item in result.outputs if item.status != "VALIDATED"]
        if failed or result.status == "FAILED" or len(result.outputs) != 2:
            result.status = "FAILED"
            extra = [item.output_revision + ":" + item.error for item in failed]
            errors = list(dict.fromkeys(list(result.errors) + extra))
            result.errors = errors or ["PAIR_OUTPUTS_INCOMPLETE"]
            return result
        hashes = {item.sha256_before for item in result.outputs}
        if len(hashes) != 2:
            result.status = "FAILED"
            result.errors.append("A_AND_00_HASHES_IDENTICAL")
        else:
            result.status = "VALIDATED"
        return result

    def validate_pair(self, pair):
        session = self.validation_root / (
            datetime.now().strftime("%Y%m%d_%H%M%S_")
            + pair["drawing_type"] + "_" + pair["identity"] + "_" + uuid.uuid4().hex[:8]
        )
        session.mkdir(parents=True, exist_ok=False)
        result = ValidationPair(pair["identity"], pair["drawing_type"], "PROCESSING")
        for output in sorted(pair["outputs"], key=lambda item: item["output_revision"]):
            result.outputs.append(self._validate_output(pair, output, session))
        return self._finish_pair(result)

    def validate_batch(self, pairs):
        """Reopen every output of a batch of pairs in ONE Core Console process.

        Validation is read-only, so reopening drawing after drawing in a single
        session is safe as long as nothing is saved; the per-output hash check
        proves that, and one output's failure never reaches its siblings.
        """
        pairs = list(pairs)
        if not pairs:
            return []
        if len(pairs) == 1:
            return [self.validate_pair(pairs[0])]
        started = time.time()
        batch = self.validation_root / (
            datetime.now().strftime("%Y%m%d_%H%M%S_")
            + "batch_" + pairs[0]["identity"] + "_" + uuid.uuid4().hex[:8]
        )
        batch.mkdir(parents=True, exist_ok=False)
        results = []
        jobs = []
        for pair in pairs:
            session = batch / ("".join(
                char if char.isalnum() or char in "._-" else "_"
                for char in str(pair["identity"])
            )[:80] or "pair")
            try:
                session.mkdir(parents=True, exist_ok=False)
            except OSError as error:
                failed = ValidationPair(pair["identity"], pair["drawing_type"],
                                        "FAILED", errors=[f"STAGING_FAILED:{error!r}"])
                results.append(failed)
                continue
            result = ValidationPair(pair["identity"], pair["drawing_type"], "PROCESSING")
            for source_output in sorted(pair["outputs"],
                                        key=lambda item: item["output_revision"]):
                job = self._stage_output(pair, source_output, session)
                if job.status != "FAILED":
                    jobs.append((result, job))
                result.outputs.append(job)
            results.append(result)

        runnable = [job for _, job in jobs]
        if runnable:
            lines = list(self._script_header())
            for index, job in enumerate(runnable):
                if index:
                    lines.extend([self._dirty_guard(),
                                  self._open_line(job.working_dwg)])
                    lines.extend(self._script_header())
                lines.append(self._call(job.output_revision, job.action_file,
                                        job.result_tsv))
            lines.extend([self._dirty_guard(), "_.QUIT", "_Y"])
            script = batch / "batch_validator.scr"
            script.write_text("\n".join(lines) + "\n", encoding="ascii",
                              errors="replace")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            timeout = self.timeout * max(1, len(runnable))
            combined, failure, exit_code = "", None, None
            try:
                process = run_console(
                    [str(self.core), "/i", str(runnable[0].working_dwg), "/s",
                     str(script), "/l", "en-US"],
                    capture_output=True, text=False, timeout=timeout,
                    cwd=str(batch), creationflags=flags,
                )
                exit_code = process.returncode
                combined = (self._decode(process.stdout) + "\n"
                            + self._decode(process.stderr))
            except subprocess.TimeoutExpired as error:
                combined = self._decode(error.stdout) + "\n" + self._decode(error.stderr)
                failure = f"VALIDATION_TIMEOUT_BATCH:{len(runnable)}:{timeout}"
            except Exception as error:
                failure = f"VALIDATION_BATCH_FAILED:{error!r}"
            for job in runnable:
                if failure:
                    job.status = "FAILED"
                    job.error = failure
                    job.elapsed_seconds = round(time.time() - started, 3)
                    if Path(job.working_dwg).is_file():
                        job.sha256_after = self._sha256(job.working_dwg)
                else:
                    self._verify_output(job, combined, exit_code, started, strict=False)
                detail = (f"VERSION={VALIDATOR_VERSION}\nBATCH={batch.name}\n"
                          f"EXIT_CODE={exit_code}\nSTATUS={job.status}\n"
                          f"ERROR={job.error}\n")
                if job.status == "FAILED":
                    detail += combined
                Path(job.log_path).write_text(detail, encoding="utf-8",
                                              errors="replace")
        # Statuses were computed before the run; recompute once the artefacts
        # exist so a batch-promoted sibling is impossible.
        return [self._finish_pair(result) for result in results]

    def validate_many(self, pairs, workers=1, on_result=None, should_cancel=None,
                      batch_size=1):
        """Validate independent pairs through a bounded pool of batched processes."""
        pairs = list(pairs)
        workers = max(1, int(workers or 1))
        batch_size = clamp_batch_size(batch_size, default=1)
        return run_batched(
            self.validate_pair, self.validate_batch, pairs,
            batch_size=batch_size, workers=workers,
            on_result=on_result, should_cancel=should_cancel,
        )

    @staticmethod
    def write_report(bundle_root, results):
        reports = Path(bundle_root) / "_GNI_REPORTS"
        reports.mkdir(parents=True, exist_ok=True)
        path = reports / "reopen_validation_report.json"
        payload = {
            "schema": "GNI-Reopen-Validator-0.4C.1",
            "version": VALIDATOR_VERSION,
            "generated_at": datetime.now().astimezone().isoformat(),
            "publishing_enabled": False,
            "summary": {
                "pairs": len(results),
                "validated": sum(item.status == "VALIDATED" for item in results),
                "failed": sum(item.status == "FAILED" for item in results),
            },
            "results": [asdict(item) for item in results],
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        atomic_replace(temporary, path)
        return path
