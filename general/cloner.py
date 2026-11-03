"""General Cloner engine: recursive DWG clone + configured find-and-replace.

The Structural Shop Drawing Cloner is a title-block/revision pipeline: it
discovers fields, builds per-handle revision plans, validates two output
revisions, and publishes into the fixed A/00 SP/AS/GA tree.  The General Cloner
is intentionally file-oriented instead:

* every ``.dwg`` under the selected source folder is a candidate;
* no GA/AS/SP, drawing-number, revision, discipline, or title-block rule is
  applied;
* each drawing is staged to a private ``%TEMP%`` copy, processed by
  ``lisp/general_cloner.lsp`` (literal, case-insensitive replacement of the
  configured tokens), verified, and only then published;
* the destination mirrors the source's relative subfolders;
* the existing ``BACKUP_OVERWRITE`` / ``SKIP_DIFFERENT`` / ``KEEP_BOTH``
  collision policies are reused verbatim.

The source DWG is never opened for writing.  All the shared runtime primitives
(Core Console process tracking, UTF-16 console decoding, SHA-256, atomic
replace, batching/pool) are reused from ``runtime/``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import csv
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Callable, List, Optional, Sequence, Tuple

from destination.builder import DestinationBuilder
from general.constants import GENERAL_MODE, GENERAL_POLICIES
from general.scanner import GeneralDrawing, scan_general_dwgs
from runtime.atomic_io import atomic_replace, atomic_write_json
from runtime.batching import clamp_batch_size, run_batched
from runtime.console_text import decode_console
from runtime.hashing import sha256_file
from runtime.process_tracking import run_console
from runtime.winpath import long_path

GENERAL_CLONER_VERSION = "0.5.0-general-cloner"
GENERAL_REPORT_NAME = "general_cloner_report.json"
GENERAL_LOG_NAME = "general_cloner.log"

# Order matters only for the report/config; the writer applies all pairs.
MAPPING_PAIRS = (
    ("project_from", "project_to"),
    ("drawing_from", "drawing_to"),
    ("long_from", "long_to"),
    ("short_from", "short_to"),
    ("contractor_doc_from", "contractor_doc_to"),
)


class GeneralClonerError(RuntimeError):
    pass


@dataclass
class GeneralCloneResult:
    """One source DWG's outcome, including every field the UI/report needs."""

    source_path: str
    relative_path: str
    destination_path: str
    status: str
    action: str = ""
    replacements: int = 0
    skip_reason: str = ""
    error: str = ""
    source_sha256: str = ""
    output_sha256: str = ""
    destination_sha256: str = ""
    backup_path: str = ""
    elapsed_seconds: float = 0.0
    log_path: str = ""
    result_tsv: str = ""
    exit_code: Optional[int] = None


@dataclass
class _Staged:
    drawing: GeneralDrawing
    result: GeneralCloneResult
    job: Path
    working: Path
    config: Path
    result_tsv: Path
    log: Path
    started: float


def mapping_pairs(mapping) -> List[Tuple[str, str]]:
    """Normalise the UI settings into ordered ``(from, to)`` literal pairs.

    The UI passes a dict of the historical settings attributes.  A sequence of
    ``(from, to)`` pairs is also accepted so tests and future callers do not
    have to know the attribute names.  Empty ``from`` tokens are ignored and a
    duplicate ``from`` token keeps its first mapping, preventing a token from
    being replaced twice in one pass.
    """
    pairs: List[Tuple[str, str]] = []
    if isinstance(mapping, dict):
        source = []
        for from_key, to_key in MAPPING_PAIRS:
            source.append((mapping.get(from_key, ""), mapping.get(to_key, "")))
    else:
        source = list(mapping or [])
    seen = set()
    for item in source:
        try:
            old, new = item
        except (TypeError, ValueError):
            continue
        old = str(old or "")
        new = str(new or "")
        if not old:
            continue
        key = old.casefold()
        if key in seen:
            continue
        seen.add(key)
        pairs.append((old, new))
    return pairs


def validate_general_job(source_root, destination_root, bundle_name, application_root):
    """Validate the mode-independent path rules and return a builder.

    Reuses :class:`DestinationBuilder` for bundle-name validation, source /
    destination separation, and the destination write probe.  It deliberately
    does **not** call ``build()``: General Cloner must not create the 16-folder
    Structural tree or copy its reference rasters.
    """
    source = Path(source_root).expanduser().resolve()
    if not source.is_dir():
        raise ValueError("Invalid source folder: " + str(source_root))
    builder = DestinationBuilder(destination_root, bundle_name, application_root)
    builder.validate_source_separation(source)
    builder._test_write_access()
    return builder


class GeneralClonerEngine:
    """Clone every DWG under a source root, preserving relative subfolders."""

    LOAD_MARKER = "GNI_GENERAL_CLONER_0_5_0_LOADED"
    SUCCESS_MARKER = "GNI_GENERAL_CLONER_OK:"
    FAILURE_MARKER = "GNI_GENERAL_CLONER_FAILED:"

    def __init__(
        self,
        source_root,
        destination_root,
        bundle_name,
        application_root,
        mapping,
        *,
        core_console="",
        lisp_source="",
        policy="BACKUP_OVERWRITE",
        workers=1,
        batch_size=1,
        timeout=240,
        process_runner: Optional[Callable] = None,
    ):
        self.source_root = Path(source_root).expanduser().resolve()
        self.builder = validate_general_job(
            self.source_root, destination_root, bundle_name, application_root
        )
        self.destination_root = self.builder.destination_root
        self.bundle_root = self.builder.bundle_root
        self.application_root = Path(application_root).resolve()
        self.mapping = mapping_pairs(mapping)
        self.policy = str(policy or "BACKUP_OVERWRITE").upper()
        if self.policy not in GENERAL_POLICIES:
            raise ValueError("Unsupported collision policy: " + str(policy))
        self.workers = max(1, int(workers or 1))
        self.batch_size = clamp_batch_size(batch_size, default=1)
        self.timeout = max(1, int(timeout or 240))
        self.process_runner = process_runner

        self.core = Path(core_console).resolve() if core_console else None
        self.lisp_source = Path(lisp_source).resolve() if lisp_source else None
        if self.process_runner is None:
            if self.core is None or not self.core.is_file():
                raise FileNotFoundError(
                    "accoreconsole.exe not found: " + str(core_console or "")
                )
            if self.lisp_source is None or not self.lisp_source.is_file():
                raise FileNotFoundError(
                    "General Cloner LSP not found: " + str(lisp_source or "")
                )

        temp = Path(os.environ.get("TEMP") or tempfile.gettempdir()).resolve()
        self.job_root = temp / "GNI" / "general_cloner"
        self.runtime_root = temp / "GNI" / "trusted_runtime"
        self.job_root.mkdir(parents=True, exist_ok=True)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.runtime_lisp = self.runtime_root / "general_cloner.lsp"
        if self.lisp_source is not None and not self._same_content(
            self.lisp_source, self.runtime_lisp
        ):
            self._atomic_copy(self.lisp_source, self.runtime_lisp)

        self.report_root = self.bundle_root / "_GNI_REPORTS"
        self.backup_root = self.bundle_root / "_GNI_BACKUP" / "General Cloner"
        self.report_path = self.report_root / GENERAL_REPORT_NAME
        self.log_path = self.report_root / GENERAL_LOG_NAME
        self._log_lock = threading.Lock()
        self._on_log: Optional[Callable[[str], None]] = None

    # -- shared primitives -------------------------------------------------
    _sha256 = staticmethod(sha256_file)
    _decode = staticmethod(decode_console)

    @staticmethod
    def _same_content(left: Path, right: Path) -> bool:
        try:
            if left.stat().st_size != right.stat().st_size:
                return False
            with left.open("rb") as a, right.open("rb") as b:
                return a.read() == b.read()
        except OSError:
            return False

    @staticmethod
    def _atomic_copy(source: Path, destination: Path):
        source = long_path(source)
        destination = long_path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # A short temporary name keeps the copy working even when the final
        # drawing name is already close to the 255-character component limit.
        temporary = destination.with_name(
            ".gni-" + uuid.uuid4().hex[:8] + destination.suffix
        )
        try:
            with source.open("rb", buffering=0) as src, temporary.open(
                "wb", buffering=0
            ) as dst:
                shutil.copyfileobj(src, dst, 4 * 1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            if temporary.stat().st_size != source.stat().st_size:
                raise OSError("Temporary copy size mismatch")
            atomic_replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return destination

    @staticmethod
    def _scr_path(path) -> str:
        return str(Path(path).resolve()).replace("\\", "/").replace('"', '\\"')

    @staticmethod
    def _safe(value) -> str:
        # Keep the temporary job/script paths ASCII: Core Console scripts are
        # written as ASCII and arbitrary source folders may contain non-ASCII
        # names that must not leak into a .scr path.
        return "".join(
            char
            if char.isascii() and (char.isalnum() or char in "._-")
            else "_"
            for char in str(value)
        )[:80]

    def _job_slug(self, value) -> str:
        """Short, ASCII-only job name; long names are what broke result paths."""
        text = self._safe(Path(str(value)).stem) or "job"
        return text[:24]

    def _new_job(self, name) -> Path:
        for _ in range(64):
            job = self.job_root / (
                datetime.now().strftime("%Y%m%d_%H%M%S_")
                + self._job_slug(name)
                + "_"
                + uuid.uuid4().hex[:8]
            )
            try:
                job.mkdir(parents=True, exist_ok=False)
                return job
            except FileExistsError:
                continue
        raise OSError("Could not allocate a General Cloner job folder for " + str(name))

    # -- logging -----------------------------------------------------------
    def _log(self, line: str) -> None:
        text = str(line).replace("\r", " ").replace("\n", " ")
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_lock:
                with self.log_path.open("a", encoding="utf-8", errors="replace") as stream:
                    stream.write(text + "\n")
        except OSError:
            pass
        callback = self._on_log
        if callback is not None:
            try:
                callback(text)
            except Exception:
                pass

    # -- discovery ---------------------------------------------------------
    def scan(self) -> List[GeneralDrawing]:
        return scan_general_dwgs(self.source_root, self.bundle_root)

    # -- process / verify --------------------------------------------------
    def _write_config(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        # cp1252 mirrors the existing stable writer's action file.  The tokens
        # are normally ASCII project/reference identifiers.
        def clean(value):
            return (
                str(value or "")
                .replace("\t", " ")
                .replace("\r", " ")
                .replace("\n", " ")
            )

        with path.open("w", encoding="cp1252", errors="replace", newline="") as stream:
            stream.write("from\tto\n")
            for old, new in self.mapping:
                stream.write(clean(old) + "\t" + clean(new) + "\n")
        return path

    def _script_header(self) -> List[str]:
        return [
            '(setvar "FILEDIA" 0)',
            '(setvar "CMDDIA" 0)',
            '(if (getvar "REPORTERROR") (setvar "REPORTERROR" 0))',
            f'(setvar "TRUSTEDPATHS" "{self._scr_path(self.runtime_root)}")',
            f'(load "{self._scr_path(self.runtime_lisp)}")',
        ]

    def _call_line(self, config: Path, result_tsv: Path) -> str:
        return (
            f'(GNI:RUN-GENERAL-CLONE "{self._scr_path(config)}" '
            f'"{self._scr_path(result_tsv)}")'
        )

    @staticmethod
    def _dirty_guard() -> str:
        return (
            '(if (/= 0 (getvar "DBMOD")) '
            '(progn (princ "\\nGNI_BATCH_DIRTY_DOCUMENT_SAVED") '
            '(vl-catch-all-apply (quote command-s) (list "_.QSAVE"))))'
        )

    @staticmethod
    def _open_line(path) -> str:
        target = str(Path(path).resolve()).replace("\\", "/").replace('"', '\\"')
        return (
            '(vl-catch-all-apply (quote (lambda () '
            '(command-s "_.OPEN" "' + target + '"))))'
        )

    def _run_script(self, script: Path, first_drawing: Path, job: Path,
                    expected_outputs: int = 1):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        timeout = self.timeout * max(1, int(expected_outputs))
        try:
            process = run_console(
                [str(self.core), "/i", str(first_drawing), "/s", str(script),
                 "/l", "en-US"],
                capture_output=True,
                text=False,
                timeout=timeout,
                cwd=str(job),
                creationflags=flags,
            )
            combined = self._decode(process.stdout) + "\n" + self._decode(process.stderr)
            return SimpleNamespace(
                returncode=process.returncode, stdout=combined, stderr=""
            )
        except subprocess.TimeoutExpired as error:
            combined = self._decode(error.stdout) + "\n" + self._decode(error.stderr)
            return SimpleNamespace(
                returncode=None,
                stdout=combined,
                stderr=f"CORE_TIMEOUT:{timeout}",
            )

    def _process_one(self, staged: _Staged):
        if self.process_runner is not None:
            return self.process_runner(
                job=staged.job,
                working_dwg=staged.working,
                config_path=staged.config,
                result_path=staged.result_tsv,
            )
        script = staged.job / "general_cloner.scr"
        lines = [
            *self._script_header(),
            self._call_line(staged.config, staged.result_tsv),
            "_.QUIT",
            "_Y",
            "",
        ]
        script.write_text("\n".join(lines), encoding="ascii", errors="replace")
        return self._run_script(script, staged.working, staged.job, 1)

    @staticmethod
    def _combined(outcome) -> str:
        return str(getattr(outcome, "stdout", "") or "") + "\n" + str(
            getattr(outcome, "stderr", "") or ""
        )

    def _verify_result(self, staged: _Staged, outcome, strict: bool) -> int:
        combined = self._combined(outcome)
        exit_code = getattr(outcome, "returncode", None)
        staged.result.exit_code = exit_code
        if strict:
            if self.LOAD_MARKER not in combined:
                raise GeneralClonerError("GENERAL_LISP_LOAD_FAILED")
            if exit_code not in (0, None):
                raise GeneralClonerError(f"CORE_EXIT_CODE:{exit_code}")
            if self.FAILURE_MARKER in combined:
                detail = combined.split(self.FAILURE_MARKER, 1)[1].splitlines()[0].strip()
                raise GeneralClonerError("GENERAL_CLONER_FAILED:" + (detail or "UNKNOWN"))

        result_tsv = Path(staged.result_tsv)
        if not result_tsv.is_file():
            raise GeneralClonerError("GENERAL_RESULT_TSV_MISSING")

        with result_tsv.open(
            "r", encoding="utf-8-sig", errors="replace", newline=""
        ) as stream:
            rows = list(csv.DictReader(stream, delimiter="\t"))
        if not rows:
            raise GeneralClonerError("GENERAL_RESULT_TSV_EMPTY")

        total = 0
        for row in rows:
            status = str(row.get("Status") or "").strip().upper()
            if status in ("FAILED", "QSAVE_FAILED"):
                raise GeneralClonerError("GENERAL_WRITE_FAILED:" + status)
        # The LISP writes one row per changed entity plus a summary row.  Sum
        # only the per-entity rows when they exist, otherwise a summary-only
        # result (the test runner) would be counted twice.
        entity_rows = [row for row in rows if str(row.get("Handle") or "").strip()]
        for row in (entity_rows or rows):
            try:
                total += int(str(row.get("Replacements") or "0").strip() or "0")
            except ValueError:
                continue
        return total

    def _backup_existing(self, destination: Path) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        relative = destination.relative_to(self.bundle_root)
        backup = self.backup_root / stamp / relative
        self._atomic_copy(destination, backup)
        if self._sha256(long_path(destination)) != self._sha256(long_path(backup)):
            raise OSError("Backup hash verification failed")
        return backup

    @staticmethod
    def _safe_suffix_name(destination: Path) -> Path:
        counter = 1
        while True:
            candidate = destination.with_name(
                f"{destination.stem}_COPY_{counter:02d}{destination.suffix}"
            )
            if not long_path(candidate).exists():
                return candidate
            counter += 1

    def _publish(self, staged: _Staged) -> None:
        result = staged.result
        working = Path(staged.working)
        destination = Path(result.destination_path)
        destination_io = long_path(destination)
        destination_io.parent.mkdir(parents=True, exist_ok=True)

        output_sha = self._sha256(long_path(working))
        result.output_sha256 = output_sha
        action = "COPY_NEW"

        if destination_io.is_file():
            destination_sha = self._sha256(destination_io)
            if destination_sha == output_sha:
                result.status = "SKIPPED"
                result.action = "SKIP_IDENTICAL"
                result.skip_reason = "Destination already matches the processed DWG"
                result.destination_sha256 = destination_sha
                return
            if self.policy == "SKIP_DIFFERENT":
                result.status = "SKIPPED"
                result.action = "SKIP_DIFFERENT"
                result.skip_reason = "Destination exists and differs (SKIP_DIFFERENT)"
                result.destination_sha256 = destination_sha
                return
            if self.policy == "KEEP_BOTH":
                destination = self._safe_suffix_name(destination)
                result.destination_path = str(destination)
                action = "KEEP_BOTH"
            else:
                result.backup_path = str(self._backup_existing(destination))
                action = "BACKUP_OVERWRITE"

        self._atomic_copy(working, destination)
        if long_path(destination).stat().st_size != long_path(working).stat().st_size:
            raise OSError("Destination size verification failed")
        destination_sha = self._sha256(long_path(destination))
        if destination_sha != output_sha:
            raise OSError("Destination hash verification failed")
        result.destination_sha256 = destination_sha
        result.action = action
        result.status = "SUCCESS"

    def _stage(self, drawing: GeneralDrawing, batch_root: Optional[Path] = None) -> _Staged:
        started = time.time()
        result = GeneralCloneResult(
            source_path=drawing.source_path,
            relative_path=drawing.relative_path,
            destination_path=drawing.destination_path,
            status="PROCESSING",
        )
        if drawing.duplicate_of:
            result.status = "SKIPPED"
            result.action = "SKIP_DUPLICATE"
            result.skip_reason = "Duplicate destination path (same as " + drawing.duplicate_of + ")"
            result.elapsed_seconds = round(time.time() - started, 3)
            return _Staged(drawing, result, Path(), Path(), Path(), Path(), Path(), started)

        job = None
        try:
            job = (
                batch_root / (self._job_slug(drawing.relative_path) + "_" + uuid.uuid4().hex[:6])
                if batch_root is not None
                else self._new_job(drawing.relative_path)
            )
            job.mkdir(parents=True, exist_ok=True)
            working = job / "working.dwg"
            result_tsv = job / "result.tsv"
            log = job / "clone.log"
            config = (batch_root / "replacements.tsv") if batch_root is not None else (job / "replacements.tsv")
            self._write_config(config)
            source = Path(drawing.source_path)
            source_io = long_path(source)
            if not source_io.is_file():
                raise FileNotFoundError("Source file not found: " + str(source))
            if source_io.stat().st_size <= 0:
                raise OSError("Source file is empty: " + str(source))
            result.source_sha256 = self._sha256(source_io)
            self._atomic_copy(source_io, working)
            return _Staged(
                drawing, result, job, working, config, result_tsv, log, started
            )
        except Exception as error:
            result.status = "FAILED"
            result.action = result.action or "NONE"
            result.error = repr(error)
            result.elapsed_seconds = round(time.time() - started, 3)
            if job is not None:
                result.log_path = str(job / "clone.log")
            return _Staged(
                drawing, result, job or Path(), Path(), Path(), Path(),
                (job / "clone.log") if job is not None else Path(), started
            )

    def _finish(self, staged: _Staged, outcome, strict: bool) -> None:
        result = staged.result
        if result.status != "PROCESSING":
            return
        try:
            result.replacements = self._verify_result(staged, outcome, strict)
            self._publish(staged)
        except Exception as error:
            result.status = "FAILED"
            result.action = result.action or "NONE"
            result.error = repr(error)
        finally:
            result.elapsed_seconds = round(time.time() - staged.started, 3)
            result.log_path = str(staged.log)
            result.result_tsv = str(staged.result_tsv)
            self._write_job_log(staged)

    def _write_job_log(self, staged: _Staged) -> None:
        result = staged.result
        if str(staged.log) not in ("", "."):
            try:
                staged.log.parent.mkdir(parents=True, exist_ok=True)
                staged.log.write_text(
                    f"VERSION={GENERAL_CLONER_VERSION}\n"
                    f"SOURCE={result.source_path}\n"
                    f"RELATIVE={result.relative_path}\n"
                    f"DESTINATION={result.destination_path}\n"
                    f"STATUS={result.status}\n"
                    f"ACTION={result.action}\n"
                    f"REPLACEMENTS={result.replacements}\n"
                    f"SKIP_REASON={result.skip_reason}\n"
                    f"ERROR={result.error}\n",
                    encoding="utf-8",
                    errors="replace",
                )
            except OSError:
                pass
        line = (
            f"STATUS={result.status} SOURCE={result.source_path} "
            f"DEST={result.destination_path} REPLACEMENTS={result.replacements}"
        )
        if result.skip_reason:
            line += " SKIP=" + result.skip_reason
        if result.error:
            line += " ERROR=" + result.error
        self._log(line)

    def clone_one(self, drawing: GeneralDrawing) -> GeneralCloneResult:
        staged = self._stage(drawing)
        if staged.result.status == "PROCESSING":
            try:
                outcome = self._process_one(staged)
            except Exception as error:
                staged.result.status = "FAILED"
                staged.result.action = staged.result.action or "NONE"
                staged.result.error = repr(error)
                staged.result.elapsed_seconds = round(time.time() - staged.started, 3)
                staged.result.log_path = str(staged.log)
                self._write_job_log(staged)
                return staged.result
            self._finish(staged, outcome, strict=True)
        else:
            staged.result.elapsed_seconds = round(time.time() - staged.started, 3)
            self._write_job_log(staged)
        return staged.result

    def clone_batch(self, drawings: Sequence[GeneralDrawing]) -> List[GeneralCloneResult]:
        drawings = list(drawings)
        if not drawings:
            return []
        if self.process_runner is not None or len(drawings) == 1:
            return [self.clone_one(drawing) for drawing in drawings]

        batch = self._new_job("batch_" + str(drawings[0].relative_path))
        staged_list: List[_Staged] = []
        for drawing in drawings:
            staged_list.append(self._stage(drawing, batch))
        runnable = [staged for staged in staged_list if staged.result.status == "PROCESSING"]
        # Report staging failures/skips even when the batch itself never runs.
        for staged in staged_list:
            if staged.result.status != "PROCESSING":
                staged.result.elapsed_seconds = round(time.time() - staged.started, 3)
                self._write_job_log(staged)
        if not runnable:
            return [staged.result for staged in staged_list]

        script = batch / "batch_general_cloner.scr"
        lines = list(self._script_header())
        for index, staged in enumerate(runnable):
            if index:
                lines.extend(
                    [
                        self._dirty_guard(),
                        self._open_line(staged.working),
                        *self._script_header(),
                    ]
                )
            lines.append(self._call_line(staged.config, staged.result_tsv))
        lines.extend([self._dirty_guard(), "_.QUIT", "_Y", ""])
        script.write_text("\n".join(lines), encoding="ascii", errors="replace")
        try:
            outcome = self._run_script(script, runnable[0].working, batch, len(runnable))
        except Exception as error:
            outcome = SimpleNamespace(
                returncode=None, stdout="", stderr=f"GENERAL_BATCH_FAILED:{error!r}"
            )

        batch_failure = str(getattr(outcome, "stderr", "") or "")
        for staged in runnable:
            if batch_failure.startswith("CORE_TIMEOUT:") or batch_failure.startswith(
                "GENERAL_BATCH_FAILED:"
            ):
                staged.result.status = "FAILED"
                staged.result.action = staged.result.action or "NONE"
                staged.result.error = batch_failure
                staged.result.elapsed_seconds = round(time.time() - staged.started, 3)
                staged.result.log_path = str(staged.log)
                self._write_job_log(staged)
            else:
                self._finish(staged, outcome, strict=False)

        for staged in staged_list:
            if staged.result.status != "PROCESSING":
                continue
            # Defensive: every staged item must end with a terminal status.
            staged.result.status = "FAILED"
            staged.result.action = staged.result.action or "NONE"
            staged.result.error = "GENERAL_BATCH_NOT_PROCESSED"
            staged.result.elapsed_seconds = round(time.time() - staged.started, 3)
            self._write_job_log(staged)
        return [staged.result for staged in staged_list]

    # -- run / report ------------------------------------------------------
    def write_report(self, results: List[GeneralCloneResult], discovered: int,
                     cancelled: bool = False) -> Path:
        self.report_root.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "GNI-General-Cloner-1.0",
            "version": GENERAL_CLONER_VERSION,
            "mode": GENERAL_MODE,
            "generated_at": datetime.now().astimezone().isoformat(),
            "source_root": str(self.source_root),
            "destination_root": str(self.destination_root),
            "bundle_root": str(self.bundle_root),
            "collision_policy": self.policy,
            "mapping": [{"from": old, "to": new} for old, new in self.mapping],
            "complete": not bool(cancelled),
            "cancelled": bool(cancelled),
            "summary": {
                "discovered": int(discovered),
                "processed": len(results),
                "success": sum(item.status == "SUCCESS" for item in results),
                "skipped": sum(item.status == "SKIPPED" for item in results),
                "failed": sum(item.status == "FAILED" for item in results),
            },
            "results": [asdict(item) for item in results],
        }
        atomic_write_json(self.report_path, payload, indent=2)
        return self.report_path

    def run(
        self,
        should_cancel: Optional[Callable[[], bool]] = None,
        on_result: Optional[Callable[[GeneralCloneResult], None]] = None,
        on_progress: Optional[Callable[[int, int, str, str], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ):
        """Scan, process, publish, and write the report.  Never raises per file."""
        self._on_log = on_log
        self.report_root.mkdir(parents=True, exist_ok=True)
        try:
            self.log_path.write_text("", encoding="utf-8")
        except OSError:
            pass
        self._log(f"MODE={GENERAL_MODE}")
        self._log(f"SOURCE={self.source_root}")
        self._log(f"DESTINATION={self.destination_root}")
        self._log(f"BUNDLE={self.bundle_root}")
        self._log(f"POLICY={self.policy}")
        self._log(f"WORKERS={self.workers} BATCH={self.batch_size}")

        drawings = self.scan()
        total = len(drawings)
        self._log(f"DISCOVERED={total}")
        if on_progress is not None:
            on_progress(0, total, "", "Discovered drawings")
        if total == 0:
            self._log("No DWG files were discovered; nothing to process.")
            report = self.write_report([], 0, cancelled=False)
            return [], report, False, 0

        done = [0]

        def record(result: GeneralCloneResult) -> None:
            done[0] += 1
            if on_progress is not None:
                on_progress(done[0], total, result.relative_path, result.status)
            if on_result is not None:
                on_result(result)

        results = run_batched(
            self.clone_one,
            self.clone_batch,
            drawings,
            batch_size=self.batch_size,
            workers=self.workers,
            on_result=record,
            should_cancel=should_cancel,
        )
        cancelled = bool(
            should_cancel is not None and should_cancel() and len(results) < total
        )
        report = self.write_report(results, total, cancelled=cancelled)
        success = sum(item.status == "SUCCESS" for item in results)
        skipped = sum(item.status == "SKIPPED" for item in results)
        failed = sum(item.status == "FAILED" for item in results)
        self._log(
            f"SUMMARY discovered={total} processed={len(results)} "
            f"success={success} skipped={skipped} failed={failed} "
            f"cancelled={bool(cancelled)}"
        )
        return results, report, cancelled, 0


__all__ = [
    "GENERAL_CLONER_VERSION",
    "GENERAL_LOG_NAME",
    "GENERAL_REPORT_NAME",
    "GeneralCloneResult",
    "GeneralClonerEngine",
    "GeneralClonerError",
    "GeneralDrawing",
    "mapping_pairs",
    "scan_general_dwgs",
    "validate_general_job",
]
