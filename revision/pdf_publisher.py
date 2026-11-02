from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
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

# 0.4C.26: one Core Console process plots a whole batch of drawings (A and 00
# per pair) instead of one process per output.
# 0.4C.31: the staged plot copy sits next to the same reference rasters the
# published drawing resolved against (relative image paths otherwise break and
# the SEAtrium/Tennet logos vanish), and the plotter forces IMAGEFRAME to 0 so
# those logos plot without a frame border.
PDF_PUBLISHER_VERSION = "0.4C.31-logo-raster-plot"
PDF_PLOTTER_LOAD_MARKER = "GNI_PDF_MODELSPACE_PLOTTER_0_4C_6_LOADED"


class PdfPublishError(RuntimeError):
    pass


@dataclass
class PdfOutput:
    revision: str
    source_dwg: str
    destination_pdf: str
    status: str = "PENDING"
    pdf_sha256: str = ""
    pdf_size: int = 0
    page_count: int = 0
    backup_path: str = ""
    log_path: str = ""
    errors: List[str] = field(default_factory=list)
    source_sha256: str = ""


@dataclass
class PdfPair:
    transaction_id: str
    identity: str
    drawing_type: str
    status: str
    outputs: List[PdfOutput] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class PdfJob:
    """One staged plot: a drawing revision plotted in-process to a PDF."""

    identity: str
    drawing_type: str
    revision: str
    source: str
    pdf: str
    result: str
    log: str
    scr: str
    output: PdfOutput
    source_sha: str = ""


class PdfPublisher:
    TYPE_FOLDERS = {"SP": "1. SP", "AS": "2. AS", "GA": "3. GA"}
    # Reference rasters every published CAD folder holds next to the drawing.
    # Image references store these as bare filenames (relative), so the staged
    # plot copy must be given the same files or the logos do not plot at all.
    ASSET_NAMES = ("SEAtrium_Ge_.PNG", "Tennet LogO New.PNG")

    def __init__(self, core_console, bundle_root, application_root, timeout=240):
        self.core = Path(core_console).resolve()
        self.bundle = Path(bundle_root).resolve()
        self.app = Path(application_root).resolve()
        self.timeout = int(timeout)
        if not self.core.is_file():
            raise PdfPublishError("ACCORECONSOLE_NOT_FOUND:" + str(self.core))
        self.lisp = self.app / "lisp" / "pdf_modelspace_plotter.lsp"
        if not self.lisp.is_file():
            raise PdfPublishError("PDF_PLOTTER_LISP_NOT_FOUND")
        settings_path = self.app / "revision" / "pdf_settings.json"
        self.settings = json.loads(settings_path.read_text(encoding="utf-8"))
        self.max_workers = max(1, int(self.settings.get("max_workers", 4)))
        temp = Path(os.environ.get("TEMP") or tempfile.gettempdir())
        self.runtime = temp / "GNI" / "trusted_runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.runtime_lisp = self.runtime / self.lisp.name
        shutil.copy2(self.lisp, self.runtime_lisp)
        # Scripts, staged DWG copies, result TSVs and logs live on LOCAL temp,
        # never inside the bundle. Core Console cancels a `/s` script it is asked
        # to read from a network share ("File load canceled"), which is exactly
        # how the PDF stage died on the real RESULT share while discovery,
        # writer and validator (all under %TEMP%\GNI) worked. Backups and reports
        # stay with the bundle.
        self.staging = temp / "GNI" / "pdf_publisher"
        self.backup = self.bundle / "_GNI_BACKUP"
        self.reports = self.bundle / "_GNI_REPORTS"
        for path in (self.staging, self.backup, self.reports): path.mkdir(parents=True, exist_ok=True)
        self.config_signature = self._signature()
        self.previous, self.previous_signature, self.previous_ok = self._load_previous()

    def _signature(self):
        keys = ("plotter_candidates", "preferred_media_names", "preferred_media_contains", "plot_style", "plot_rotation", "minimum_pdf_bytes")
        payload = {key: self.settings.get(key) for key in keys}
        payload["version"] = PDF_PUBLISHER_VERSION
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def _load_previous(self):
        """Load the last report so unchanged pairs can skip replotting.

        A config signature match plus identical source/DWG hashes makes the
        existing PDF provably current; this makes reruns incremental.
        """
        path = self.reports / "pdf_publish_report.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}, "", False
        outputs = {}
        pair_results = payload.get("results", [])
        for pair in pair_results:
            for output in pair.get("outputs", []):
                if output.get("status") in ("PDF_PUBLISHED", "PDF_UNCHANGED"):
                    outputs[(str(pair.get("identity")), str(output.get("revision")))] = output
        published = any(pair.get("status") == "PDF_PAIR_PUBLISHED" for pair in pair_results)
        return outputs, str(payload.get("config_signature", "")), published

    # Hasher shared with the other engines (kept as an alias; see runtime/hashing.py).
    _sha = staticmethod(sha256_file)

    @staticmethod
    def _inside(path, root):
        try: Path(path).resolve().relative_to(Path(root).resolve()); return True
        except ValueError: return False

    @staticmethod
    def _lisp_list(values):
        def q(value): return '"' + str(value).replace("\\", "/").replace('"', '\\"') + '"'
        return "(" + " ".join(q(v) for v in values) + ")"

    @staticmethod
    def _pdf_valid(path, minimum):
        path = Path(path)
        if not path.is_file() or path.stat().st_size < minimum: return False, 0
        data = path.read_bytes()
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-4096:]: return False, 0
        pages = len(re.findall(rb"/Type\s*/Page(?!s)\b", data))
        return pages == 1, pages

    @classmethod
    def load_published_pairs(cls, bundle_root):
        report = Path(bundle_root) / "_GNI_REPORTS" / "atomic_publish_report.json"
        if not report.is_file(): raise PdfPublishError("ATOMIC_PUBLISH_REPORT_NOT_FOUND")
        payload = json.loads(report.read_text(encoding="utf-8"))
        latest = {}
        for tx in payload.get("transactions", []):
            if tx.get("status") == "PUBLISHED": latest[tx["identity"]] = tx
        if not latest: raise PdfPublishError("NO_PUBLISHED_DWG_PAIRS")
        return list(latest.values())

    def _script_header(self):
        def p(value): return str(Path(value).resolve()).replace("\\", "/")
        return [
            '(setvar "FILEDIA" 0)', '(setvar "CMDDIA" 0)',
            '(if (getvar "REPORTERROR") (setvar "REPORTERROR" 0))',
            f'(setvar "TRUSTEDPATHS" "{p(self.runtime)}")',
            f'(load "{p(self.runtime_lisp)}")',
        ]

    @staticmethod
    def _open_line(path):
        """Open the next drawing from LISP, not from a bare `_.OPEN` line.

        A ``.scr`` is fed one line at a time, so ``_.OPEN C:/a b/x.dwg`` loses
        everything after the first space: Core Console reported "Cannot find
        the specified drawing file", then waited for the rest of the name until
        the batch timeout. Every published drawing lives under ``1. SP`` /
        ``2. AS`` / ``3. GA``, so this was not theoretical. ``command-s`` is the
        same call the discovery LISP uses for its batch and takes the path as
        one string.
        """
        target = str(Path(path).resolve()).replace("\\", "/").replace('"', '\\"')
        return ('(vl-catch-all-apply (quote (lambda () '
                '(command-s "_.OPEN" "' + target + '"))))')

    @staticmethod
    def _dirty_guard():
        """Keep a dirty drawing from turning `_.OPEN` into a prompt.

        Plotting must leave the published DWG untouched (its hash is the resume
        key), so this only fires when something already went wrong. Saving the
        file then makes that plot fail its hash/validation instead of hanging
        the rest of the batch.
        """
        return (
            '(if (/= 0 (getvar "DBMOD")) '
            '(progn (princ "\\nGNI_BATCH_DIRTY_DOCUMENT_SAVED") '
            '(vl-catch-all-apply (quote command-s) (list "_.QSAVE"))))'
        )

    def _stage_plot(self, identity, drawing_type, revision, source, stage, source_sha=""):
        """Copy the published DWG aside and write this plot's script.

        The plot runs on a *copy* under the staging folder, never on the
        published DWG. `-PLOT` writes the page setup back into the drawing
        (DBMOD becomes non-zero), and the batch dirty-guard then QSAVEs it - on
        a destination file that silently breaks the validated SHA-256 the
        publish/dry-run reports recorded. A copy is also what keeps parallel
        workers from dropping `.dwl` lock files next to delivered drawings.
        """
        def p(value): return str(Path(value).resolve()).replace("\\", "/")
        suffix = "_A" if revision == "A" else "_00"
        published = Path(source).resolve()
        pdf = stage / revision / (identity + suffix + ".pdf")
        printable = stage / revision / published.name
        job = PdfJob(
            identity=identity,
            drawing_type=drawing_type,
            revision=revision,
            source=str(printable),
            pdf=str(pdf),
            result=str(stage / (revision + "_plot_result.tsv")),
            log=str(stage / (revision + "_plot.log")),
            scr=str(stage / (revision + "_plot.scr")),
            output=PdfOutput(
                revision, str(published),
                str(self.bundle / revision / self.TYPE_FOLDERS[drawing_type] / "PDF"
                    / pdf.name),
                "PROCESSING", source_sha256=source_sha,
            ),
            source_sha=source_sha,
        )
        pdf.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(published, printable)
        # The logos are attached by bare filename (relative), so the staged copy
        # must resolve them against the same PNGs the published drawing lives
        # next to. Without this, -PLOT renders each missing raster as an empty
        # frame (or nothing) and the SEAtrium/Tennet logos vanish from the PDF.
        for asset_name in self.ASSET_NAMES:
            asset = published.parent / asset_name
            if asset.is_file():
                shutil.copy2(asset, printable.parent / asset_name)
        plotters = self._lisp_list(self.settings["plotter_candidates"])
        media = self._lisp_list(
            self.settings.get("preferred_media_names")
            or self.settings.get("preferred_media_contains", [])
        )
        style = str(self.settings.get("plot_style", "")).replace('"', '')
        rotation = int(self.settings.get("plot_rotation", 0))
        Path(job.scr).write_text("\n".join([
            *self._script_header(),
            f'(GNI:RUN-PDF-PLOTTER "{p(pdf)}" "{p(job.result)}" \'{plotters} '
            f'\'{media} "{style}" {rotation})', "",
        ]), encoding="ascii", errors="replace")
        return job

    @staticmethod
    def _failure_reason(path):
        try:
            rows = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        for row in rows[1:]:
            fields = row.split("\t")
            if len(fields) >= 2 and fields[0] == "FAILED":
                return fields[1]
        return ""

    def _read_plot(self, job, text, exit_code, strict):
        """Judge one plot by its own result TSV and PDF, never by a sibling's."""
        output = job.output
        output.log_path = job.log
        try:
            if PDF_PLOTTER_LOAD_MARKER not in text:
                raise PdfPublishError(
                    job.revision + ":PLOTTER_LISP_NOT_LOADED; log=" + job.log
                )
            if not Path(job.result).is_file():
                raise PdfPublishError(
                    job.revision + ":PLOT_RESULT_MISSING; stage="
                    + str(Path(job.pdf).parent)
                )
            reason = self._failure_reason(job.result)
            if reason:
                raise PdfPublishError(
                    job.revision + ":PDF_PLOT_OR_VALIDATION_FAILED:" + reason
                    + "; log=" + job.log + "; stage=" + str(Path(job.pdf).parent)
                )
            if strict and "GNI_PDF_PUBLISHER_OK" not in text:
                raise PdfPublishError(
                    job.revision + ":PDF_PLOT_OR_VALIDATION_FAILED:NO_RESULT_REASON"
                    + "; log=" + job.log + "; stage=" + str(Path(job.pdf).parent)
                )
            valid, pages = self._pdf_valid(
                job.pdf, int(self.settings.get("minimum_pdf_bytes", 4096))
            )
            if not valid:
                raise PdfPublishError(
                    job.revision + ":PDF_PLOT_OR_VALIDATION_FAILED:NO_RESULT_REASON"
                    + "; log=" + job.log + "; stage=" + str(Path(job.pdf).parent)
                )
            output.pdf_sha256 = self._sha(job.pdf)
            output.pdf_size = Path(job.pdf).stat().st_size
            output.page_count = pages
            output.status = "STAGED_VALIDATED"
        except Exception as error:
            output.status = "FAILED"
            output.errors.append(str(error) or repr(error))
        return output

    def _run_plot(self, job):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            cp = run_console(
                [str(self.core), "/i", job.source, "/s", job.scr, "/l", "en-US"],
                capture_output=True, text=False, timeout=self.timeout,
                cwd=str(Path(job.pdf).parent.parent), creationflags=flags,
            )
        except subprocess.TimeoutExpired as error:
            Path(job.log).write_text(
                "TIMEOUT after " + str(self.timeout) + "s\n",
                encoding="utf-8", errors="replace",
            )
            job.output.status = "FAILED"
            job.output.errors.append(job.revision + ":PDF_PLOT_TIMEOUT")
            return job.output
        except Exception as error:
            job.output.status = "FAILED"
            job.output.errors.append(job.revision + ":PDF_PLOT_FAILED:" + repr(error))
            return job.output
        # Shared decoder: a mixed buffer used to be guessed as one encoding, so a
        # failed plot could log mojibake and never match PDF_PLOTTER_LOAD_MARKER.
        text = decode_console(cp.stdout) + "\n" + decode_console(cp.stderr)
        Path(job.log).write_text(
            "EXIT_CODE=" + str(cp.returncode) + "\n" + text,
            encoding="utf-8", errors="replace",
        )
        return self._read_plot(job, text, cp.returncode, strict=True)

    def _plot(self, identity, drawing_type, revision, source, stage, source_sha=""):
        """Plot one revision in its own Core Console process.

        Kept as the single-drawing entry point (and the seam tests patch); the
        batched path stages the same job through `_stage_plot` and runs a whole
        batch in one process instead.
        """
        job = self._stage_plot(identity, drawing_type, revision, source, stage,
                               source_sha=source_sha)
        self._run_plot(job)
        return job.output, Path(job.pdf)

    def _prepare_pair(self, tx, plot=None):
        """Stage one pair: hash-check, skip provably unchanged PDFs, build jobs.

        ``plot`` is the single-drawing plotter. When it is omitted the pair is
        only staged (scripts written, hashes checked) and the returned jobs are
        meant to run together in one Core Console process.
        """
        identity, drawing_type = tx["identity"], tx["drawing_type"]
        type_folder = self.TYPE_FOLDERS[drawing_type]
        tid = "PDF_" + datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        stage = self.staging / tid
        stage.mkdir(parents=True, exist_ok=False)
        pair = PdfPair(tid, identity, drawing_type, "PROCESSING")
        jobs, staged = [], []
        try:
            dwgs = {item["output_revision"]: Path(item["destination"]).resolve()
                    for item in tx["files"]}
            if set(dwgs) != {"A", "00"}:
                raise PdfPublishError("PUBLISHED_DWG_PAIR_INCOMPLETE")
            stable_run = self.previous_ok and self.previous_signature == self.config_signature
            for rev in ("A", "00"):
                if not dwgs[rev].is_file():
                    raise PdfPublishError(rev + ":PUBLISHED_DWG_MISSING")
                source_sha = self._sha(dwgs[rev])
                suffix = "_A" if rev == "A" else "_00"
                dest = self.bundle / rev / type_folder / "PDF" / (identity + suffix + ".pdf")
                previous = self.previous.get((identity, rev))
                if (stable_run and previous
                        and str(previous.get("source_sha256", "")) == source_sha
                        and str(previous.get("destination_pdf", "")) == str(dest)
                        and dest.is_file() and self._sha(dest) == str(previous.get("pdf_sha256", ""))):
                    pair.outputs.append(PdfOutput(
                        rev, str(dwgs[rev]), str(dest), "PDF_UNCHANGED",
                        str(previous.get("pdf_sha256", "")), dest.stat().st_size,
                        int(previous.get("page_count") or 1), source_sha256=source_sha,
                    ))
                    continue
                if plot is None:
                    job = self._stage_plot(identity, drawing_type, rev, dwgs[rev],
                                           stage, source_sha=source_sha)
                    jobs.append(job)
                    output, path = job.output, Path(job.pdf)
                else:
                    output, path = plot(identity, drawing_type, rev, dwgs[rev],
                                        stage, source_sha=source_sha)
                pair.outputs.append(output)
                staged.append((output, path))
        except Exception as error:
            pair.errors.append(repr(error))
            pair.status = "PDF_PUBLISH_FAILED_ROLLED_BACK"
        return pair, self.backup / tid, jobs, staged

    def _commit_pair(self, pair, backup, staged):
        """Copy staged PDFs over the destination atomically, rolling back on error."""
        committed = []
        try:
            for result, pdf in staged:
                if result.status != "STAGED_VALIDATED":
                    continue
                dest = Path(result.destination_pdf)
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.is_file():
                    b = backup / result.revision / dest.name
                    b.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dest, b)
                    result.backup_path = str(b)
                temp = dest.with_name(dest.name + ".gni-pdf-" + pair.transaction_id)
                shutil.copy2(pdf, temp)
                if self._sha(temp) != result.pdf_sha256:
                    raise PdfPublishError("PDF_TEMP_HASH_MISMATCH")
                atomic_replace(temp, dest)
                committed.append(result)
                if self._sha(dest) != result.pdf_sha256:
                    raise PdfPublishError("PDF_FINAL_HASH_MISMATCH")
                result.status = "PDF_PUBLISHED"
            pair.status = "PDF_PAIR_PUBLISHED"
        except Exception as error:
            pair.errors.append(repr(error))
            rollback_errors = []
            for result in reversed(committed):
                try:
                    dest = Path(result.destination_pdf)
                    if result.backup_path:
                        shutil.copy2(result.backup_path, dest)
                    else:
                        dest.unlink(missing_ok=True)
                except Exception as restore:
                    rollback_errors.append(repr(restore))
            pair.status = "PDF_ROLLBACK_FAILED" if rollback_errors else "PDF_PUBLISH_FAILED_ROLLED_BACK"
            pair.errors.extend(rollback_errors)
        return pair

    def _finish_pair(self, pair, backup, staged):
        if pair.status != "PROCESSING":
            return pair
        failed = [output for output, _ in staged if output.status != "STAGED_VALIDATED"]
        if failed:
            pair.status = "PDF_PUBLISH_FAILED_ROLLED_BACK"
            for output in failed:
                pair.errors.extend(output.errors or [output.revision + ":PLOT_FAILED"])
            return pair
        return self._commit_pair(pair, backup, staged)

    def _publish_pair(self, tx):
        pair, backup, _, staged = self._prepare_pair(tx, plot=self._plot)
        return self._finish_pair(pair, backup, staged)

    def _run_plot_batch(self, jobs):
        """Plot every job of a batch inside one Core Console process."""
        started = time.time()
        lines = list(self._script_header())
        for index, job in enumerate(jobs):
            if index:
                # LISP state does not survive `_.OPEN`, so the header (and the
                # plotter LSP) is re-issued for every drawing.
                lines.extend([self._dirty_guard(), self._open_line(job.source)])
                lines.extend(self._script_header())
            plotters = self._lisp_list(self.settings["plotter_candidates"])
            media = self._lisp_list(
                self.settings.get("preferred_media_names")
                or self.settings.get("preferred_media_contains", [])
            )
            style = str(self.settings.get("plot_style", "")).replace('"', '')
            rotation = int(self.settings.get("plot_rotation", 0))
            def p(value): return str(Path(value).resolve()).replace("\\", "/")
            lines.append(
                f'(GNI:RUN-PDF-PLOTTER "{p(job.pdf)}" "{p(job.result)}" '
                f'\'{plotters} \'{media} "{style}" {rotation})'
            )
        lines.extend([self._dirty_guard(), "_.QUIT", "_Y"])
        batch_script = self.staging / ("PDF_BATCH_" + uuid.uuid4().hex[:8] + ".scr")
        batch_script.write_text("\n".join(lines) + "\n", encoding="ascii",
                                errors="replace")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        timeout = self.timeout * max(1, len(jobs))
        text, failure, exit_code = "", None, None
        try:
            cp = run_console(
                [str(self.core), "/i", jobs[0].source, "/s", str(batch_script),
                 "/l", "en-US"],
                capture_output=True, text=False, timeout=timeout,
                cwd=str(self.staging), creationflags=flags,
            )
            exit_code = cp.returncode
            text = decode_console(cp.stdout) + "\n" + decode_console(cp.stderr)
        except subprocess.TimeoutExpired as error:
            text = decode_console(error.stdout) + "\n" + decode_console(error.stderr)
            failure = "PDF_PLOT_TIMEOUT_BATCH:" + str(len(jobs)) + ":" + str(timeout)
        except Exception as error:
            failure = "PDF_PLOT_BATCH_FAILED:" + repr(error)
        for job in jobs:
            if failure:
                job.output.status = "FAILED"
                job.output.errors.append(job.revision + ":" + failure)
            else:
                self._read_plot(job, text, exit_code, strict=False)
            detail = ("VERSION=" + PDF_PUBLISHER_VERSION + "\nBATCH="
                      + batch_script.name + "\nEXIT_CODE=" + str(exit_code)
                      + "\nSTATUS=" + job.output.status + "\n"
                      + ("\n".join(job.output.errors) + "\n" if job.output.errors else ""))
            if job.output.status == "FAILED":
                detail += text
            Path(job.log).write_text(detail, encoding="utf-8", errors="replace")
        return time.time() - started

    def publish_batch(self, txs):
        """Plot and publish a whole batch of pairs in ONE Core Console process.

        Staging, unchanged-PDF detection and the commit/rollback unit stay
        per pair: batching only shares the expensive Core Console session. A
        pair that fails never commits, and its siblings are unaffected.
        """
        txs = list(txs)
        if not txs:
            return []
        if len(txs) == 1:
            return [self._publish_pair(txs[0])]
        prepared = [self._prepare_pair(tx) for tx in txs]
        jobs = [job for _, _, pair_jobs, _ in prepared for job in pair_jobs]
        if jobs:
            self._run_plot_batch(jobs)
        pairs = []
        for pair, backup, _, staged in prepared:
            pairs.append(self._finish_pair(pair, backup, staged))
        # Isolated retry: when a batch killed only some plots, re-run those on
        # their own so a single bad drawing cannot take its siblings down with
        # it. A batch that failed entirely is left alone - that is systemic
        # (wrong plotter, wrong LISP) and retrying would only burn processes.
        if any(item.status == "PDF_PAIR_PUBLISHED" for item in pairs):
            pairs = [
                self._publish_pair(txs[index])
                if item.status != "PDF_PAIR_PUBLISHED" else item
                for index, item in enumerate(pairs)
            ]
        return pairs

    def publish_many(self, transactions, callback=None, workers=1, should_cancel=None,
                     batch_size=1):
        """Plot pairs concurrently; reruns skip provably unchanged PDFs.

        Two levers, exactly like the discovery stage: ``batch_size`` pairs per
        Core Console process and ``workers`` processes in flight. The first
        batch is used as a plotter/media preflight unless the previous
        successful run used the exact same plot configuration. After that a
        failure is recorded per pair instead of stopping a hundreds-file batch.
        """
        transactions = list(transactions)
        total = len(transactions)
        workers = max(1, int(workers or self.max_workers))
        batch_size = clamp_batch_size(batch_size, default=1)
        results = {}

        def notify(current, identity, stage):
            if callback: callback(current, total, identity, stage)

        def record(pair):
            results[str(pair.identity)] = pair

        cursor = 0
        preflight = total > 1 and not (self.previous_ok and self.previous_signature == self.config_signature)
        if preflight:
            notify(1, transactions[0]["identity"], "PDF preflight")
            first = self._publish_pair(transactions[0]); record(first)
            if first.status != "PDF_PAIR_PUBLISHED":
                first.errors.append("BATCH_STOPPED_BY_PDF_PREFLIGHT")
                return [first]
            cursor = 1
        queue = transactions[cursor:]
        done = [0]

        def progress(pair):
            done[0] += 1
            notify(done[0], pair.identity, pair.status)

        if batch_size > 1 and len(queue) > 1:
            for pair in run_batched(
                self._publish_pair, self.publish_batch, queue,
                batch_size=batch_size, workers=workers,
                on_result=progress, should_cancel=should_cancel,
            ):
                record(pair)
        elif workers == 1 or len(queue) <= 1:
            for tx in queue:
                if should_cancel and should_cancel(): break
                notify(len(results) + 1, tx["identity"], "Plotting and publishing PDF")
                record(self._publish_pair(tx))
        else:
            for pair in run_batched(
                self._publish_pair, self._publish_pair, queue,
                batch_size=1, workers=workers,
                on_result=progress, should_cancel=should_cancel,
            ):
                record(pair)
        order = {str(tx["identity"]): index for index, tx in enumerate(transactions)}
        return [results[key] for key in sorted(results, key=lambda key: order.get(key, 10**9))]

    @staticmethod
    def mark_report_incomplete(report_path, reason="CANCELLED"):
        """Flag a report written by an interrupted run as partial.

        ``write_report`` only serialises the pairs that finished, so a cancelled
        200-drawing batch produced a report that looked like a complete, passing
        run. The ``complete`` flag lets the pipeline and ``_report_state`` tell
        the difference instead of silently accepting half the bundle.
        """
        path = Path(report_path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        payload["complete"] = False
        payload["cancelled"] = True
        payload["cancel_reason"] = str(reason)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        atomic_replace(temp, path)
        return path

    def write_report(self, results):
        path=self.reports/"pdf_publish_report.json"
        unchanged=sum(output.status=="PDF_UNCHANGED" for pair in results for output in pair.outputs)
        failed=sum(x.status!="PDF_PAIR_PUBLISHED" for x in results)
        # A run that left one or more pairs failed still writes its report, so
        # without this flag _report_state() saw a report file and marked the PDF
        # stage DONE. The primary button then jumped to "Open Reports" and the
        # failed pairs could only be retried from Advanced. Mark the batch
        # partial instead; the report stays a valid resume record either way.
        payload={"schema":"GNI-PDF-Publisher-0.4C.6","version":PDF_PUBLISHER_VERSION,"generated_at":datetime.now().astimezone().isoformat(),"config_signature":self.config_signature,"complete":failed==0,"summary":{"pairs":len(results),"published":sum(x.status=="PDF_PAIR_PUBLISHED" for x in results),"unchanged":unchanged,"failed":failed},"results":[asdict(x) for x in results]}
        temp=path.with_suffix(".json.tmp");temp.write_text(json.dumps(payload,indent=2,ensure_ascii=False),encoding="utf-8");atomic_replace(temp,path);return path

