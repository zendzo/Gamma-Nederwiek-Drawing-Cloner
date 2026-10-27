from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
import csv
import hashlib
import json
import os
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import List

VALIDATOR_VERSION = "0.4C.6-parallel-validator"


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

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _decode(value):
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if value.startswith(b"\xff\xfe") or (len(value) > 4 and value[1:2] == b"\x00"):
            return value.decode("utf-16", errors="replace")
        for encoding in ("utf-8", "cp1252"):
            try:
                return value.decode(encoding)
            except UnicodeDecodeError:
                pass
        return value.decode("utf-8", errors="replace")

    @staticmethod
    def _scr_path(path):
        return str(Path(path).resolve()).replace("\\", "/").replace('"', '\\"')

    @classmethod
    def load_writer_results(cls, report_path):
        path = Path(report_path)
        if not path.is_file():
            raise ReopenValidationError(f"STABLE_WRITER_REPORT_NOT_FOUND:{path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        accepted = []
        rejected = []
        for pair in payload.get("results", []):
            if pair.get("status") != "LOCAL_PAIR_WRITTEN_NOT_VALIDATED":
                rejected.append(pair.get("identity", "") + "=" + str(pair.get("status", "")))
                continue
            outputs = pair.get("outputs", [])
            if {item.get("output_revision") for item in outputs} != {"A", "00"}:
                rejected.append(pair.get("identity", "") + "=PAIR_OUTPUTS_INCOMPLETE")
                continue
            accepted.append(pair)
        if rejected:
            raise ReopenValidationError("WRITER_RESULTS_NOT_VALIDATABLE:" + ";".join(rejected))
        if not accepted:
            raise ReopenValidationError("NO_LOCAL_PAIRS_TO_VALIDATE")
        return accepted

    @staticmethod
    def _count_actions(path):
        with Path(path).open("r", encoding="cp1252", errors="replace", newline="") as stream:
            return sum(1 for _ in csv.DictReader(stream, delimiter="\t"))

    def _validate_output(self, pair, source_output, session):
        started = time.time()
        mode = source_output["output_revision"]
        dwg = Path(source_output["working_dwg"]).resolve()
        actions = Path(source_output["action_file"]).resolve()
        result_tsv = session / f"{mode}_validation.tsv"
        log = session / f"{mode}_validation.log"
        script = session / f"{mode}_validation.scr"
        output = ValidationOutput(
            pair["identity"], pair["drawing_type"], mode, "PROCESSING",
            str(dwg), str(actions), str(result_tsv), str(log),
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

            script.write_text("\n".join([
                '(setvar "FILEDIA" 0)',
                '(setvar "CMDDIA" 0)',
                '(if (getvar "REPORTERROR") (setvar "REPORTERROR" 0))',
                f'(setvar "TRUSTEDPATHS" "{self._scr_path(self.runtime_root)}")',
                f'(load "{self._scr_path(self.runtime_lisp)}")',
                f'(GNI:RUN-REOPEN-VALIDATOR "{mode}" "{self._scr_path(actions)}" "{self._scr_path(result_tsv)}")',
                "",
            ]), encoding="ascii", errors="replace")

            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            process = subprocess.run(
                [str(self.core), "/i", str(dwg), "/s", str(script), "/l", "en-US"],
                capture_output=True, text=False, timeout=self.timeout,
                cwd=str(session), creationflags=flags,
            )
            output.exit_code = process.returncode
            combined = self._decode(process.stdout) + "\n" + self._decode(process.stderr)
            log.write_text(
                f"VERSION={VALIDATOR_VERSION}\nEXIT_CODE={process.returncode}\n{combined}",
                encoding="utf-8", errors="replace",
            )
            if self.LOAD_MARKER not in combined:
                raise ReopenValidationError("VALIDATOR_LISP_LOAD_FAILED")
            if self.FAILURE_MARKER in combined:
                raise ReopenValidationError("VALIDATION_CHECK_FAILED")
            if self.SUCCESS_MARKER not in combined or self.EXIT_MARKER not in combined:
                raise ReopenValidationError("VALIDATION_SUCCESS_MARKER_MISSING")
            if not result_tsv.is_file():
                raise ReopenValidationError("VALIDATION_RESULT_MISSING")

            with result_tsv.open("r", encoding="utf-8-sig", errors="replace", newline="") as stream:
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

    def validate_pair(self, pair):
        session = self.validation_root / (
            datetime.now().strftime("%Y%m%d_%H%M%S_")
            + pair["drawing_type"] + "_" + pair["identity"] + "_" + uuid.uuid4().hex[:8]
        )
        session.mkdir(parents=True, exist_ok=False)
        result = ValidationPair(pair["identity"], pair["drawing_type"], "PROCESSING")
        for output in sorted(pair["outputs"], key=lambda item: item["output_revision"]):
            result.outputs.append(self._validate_output(pair, output, session))
        failed = [item for item in result.outputs if item.status != "VALIDATED"]
        if not failed:
            hashes = {item.sha256_before for item in result.outputs}
            if len(hashes) != 2:
                result.status = "FAILED"
                result.errors.append("A_AND_00_HASHES_IDENTICAL")
            else:
                result.status = "VALIDATED"
        else:
            result.status = "FAILED"
            result.errors.extend(item.output_revision + ":" + item.error for item in failed)
        return result

    def validate_many(self, pairs, workers=1, on_result=None, should_cancel=None):
        """Validate independent pairs through a bounded Core Console pool."""
        pairs = list(pairs)
        workers = max(1, int(workers or 1))
        results = {}
        order = {str(pair.get("identity")): index for index, pair in enumerate(pairs)}

        def record(result):
            results[str(result.identity)] = result
            if on_result:
                on_result(result)

        if workers == 1 or len(pairs) <= 1:
            for pair in pairs:
                if should_cancel and should_cancel():
                    break
                record(self.validate_pair(pair))
        else:
            def submit_next(pool, pending, iterator):
                try:
                    pair = next(iterator)
                except StopIteration:
                    return False
                pending[pool.submit(self.validate_pair, pair)] = pair
                return True

            iterator = iter(pairs)
            pending = {}
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="gni-validator"
            ) as pool:
                for _ in range(workers):
                    if not submit_next(pool, pending, iterator):
                        break
                while pending:
                    done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                    for future in done:
                        pending.pop(future)
                        record(future.result())
                    if should_cancel and should_cancel():
                        for future in pending:
                            future.cancel()
                        break
                    while len(pending) < workers and submit_next(
                        pool, pending, iterator
                    ):
                        pass

        return [
            results[key] for key in sorted(results, key=lambda key: order.get(key, 10**9))
        ]

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
        os.replace(temporary, path)
        return path
