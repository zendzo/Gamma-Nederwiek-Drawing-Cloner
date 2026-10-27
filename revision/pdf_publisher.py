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
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import List

from runtime.process_tracking import run_console

PDF_PUBLISHER_VERSION = "0.4C.6-parallel-plotter"
PDF_PLOTTER_LOAD_MARKER = "GNI_PDF_MODELSPACE_PLOTTER_0_4C_5_LOADED"


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


class PdfPublisher:
    TYPE_FOLDERS = {"SP": "1. SP", "AS": "2. AS", "GA": "3. GA"}

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
        self.staging = self.bundle / "_GNI_STAGING"
        self.backup = self.bundle / "_GNI_BACKUP"
        self.reports = self.bundle / "_GNI_REPORTS"
        for path in (self.staging, self.backup, self.reports): path.mkdir(parents=True, exist_ok=True)
        self.config_signature = self._signature()
        self.previous, self.previous_signature, self.previous_ok = self._load_previous()

    def _signature(self):
        keys = ("plotter_candidates", "preferred_media_names", "preferred_media_contains", "plot_style", "plot_rotation", "minimum_pdf_bytes")
        payload = {key: self.settings.get(key) for key in keys}
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

    @staticmethod
    def _sha(path):
        h = hashlib.sha256()
        with Path(path).open("rb") as f:
            for block in iter(lambda: f.read(4 * 1024 * 1024), b""): h.update(block)
        return h.hexdigest()

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

    def _plot(self, identity, drawing_type, revision, source, stage, source_sha=""):
        type_folder = self.TYPE_FOLDERS[drawing_type]
        suffix = "_A" if revision == "A" else "_00"
        pdf = stage / revision / (identity + suffix + ".pdf")
        result = stage / (revision + "_plot_result.tsv")
        log = stage / (revision + "_plot.log")
        scr = stage / (revision + "_plot.scr")
        pdf.parent.mkdir(parents=True, exist_ok=True)
        plotters = self._lisp_list(self.settings["plotter_candidates"])
        media = self._lisp_list(self.settings.get("preferred_media_names") or self.settings.get("preferred_media_contains", []))
        style = str(self.settings.get("plot_style", "")).replace('"', '')
        rotation = int(self.settings.get("plot_rotation", 0))
        def p(value): return str(Path(value).resolve()).replace("\\", "/")
        scr.write_text("\n".join([
            '(setvar "FILEDIA" 0)', '(setvar "CMDDIA" 0)',
            '(if (getvar "REPORTERROR") (setvar "REPORTERROR" 0))',
            f'(setvar "TRUSTEDPATHS" "{p(self.runtime)}")',
            f'(load "{p(self.runtime_lisp)}")',
            f'(GNI:RUN-PDF-PLOTTER "{p(pdf)}" "{p(result)}" \'{plotters} \'{media} "{style}" {rotation})', ""
        ]), encoding="ascii", errors="replace")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            cp = run_console([str(self.core), "/i", str(source), "/s", str(scr), "/l", "en-US"], capture_output=True, text=False, timeout=self.timeout, cwd=str(stage), creationflags=flags)
        except subprocess.TimeoutExpired as error:
            log.write_text("TIMEOUT after " + str(self.timeout) + "s\n", encoding="utf-8", errors="replace")
            raise PdfPublishError(revision + ":PDF_PLOT_TIMEOUT; stage=" + str(stage)) from error
        output = (cp.stdout or b"") + b"\n" + (cp.stderr or b"")
        text = output.decode("utf-16le", errors="replace") if (b"\x00" in output[:256]) else output.decode("utf-8", errors="replace")
        log.write_text("EXIT_CODE=" + str(cp.returncode) + "\n" + text, encoding="utf-8", errors="replace")
        if PDF_PLOTTER_LOAD_MARKER not in text:
            raise PdfPublishError(revision + ":PLOTTER_LISP_NOT_LOADED; log=" + str(log))
        valid, pages = self._pdf_valid(pdf, int(self.settings.get("minimum_pdf_bytes", 4096)))
        if "GNI_PDF_PUBLISHER_OK" not in text or not valid:
            reason = self._failure_reason(result)
            raise PdfPublishError(revision + ":PDF_PLOT_OR_VALIDATION_FAILED:" + (reason or "NO_RESULT_REASON") + "; log=" + str(log) + "; stage=" + str(stage))
        destination = self.bundle / revision / type_folder / "PDF" / pdf.name
        return PdfOutput(revision, str(source), str(destination), "STAGED_VALIDATED", self._sha(pdf), pdf.stat().st_size, pages, log_path=str(log), source_sha256=source_sha), pdf

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

    def _publish_pair(self, tx):
        identity, drawing_type = tx["identity"], tx["drawing_type"]
        type_folder = self.TYPE_FOLDERS[drawing_type]
        tid = "PDF_" + datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        stage = self.staging / tid; backup = self.backup / tid
        stage.mkdir(parents=True, exist_ok=False)
        pair = PdfPair(tid, identity, drawing_type, "PROCESSING")
        committed = []
        try:
            dwgs = {item["output_revision"]: Path(item["destination"]).resolve() for item in tx["files"]}
            if set(dwgs) != {"A", "00"}: raise PdfPublishError("PUBLISHED_DWG_PAIR_INCOMPLETE")
            staged = {}
            stable_run = self.previous_ok and self.previous_signature == self.config_signature
            for rev in ("A", "00"):
                if not dwgs[rev].is_file(): raise PdfPublishError(rev + ":PUBLISHED_DWG_MISSING")
                source_sha = self._sha(dwgs[rev])
                suffix = "_A" if rev == "A" else "_00"
                dest = self.bundle / rev / type_folder / "PDF" / (identity + suffix + ".pdf")
                previous = self.previous.get((identity, rev))
                if (stable_run and previous
                        and str(previous.get("source_sha256", "")) == source_sha
                        and str(previous.get("destination_pdf", "")) == str(dest)
                        and dest.is_file() and self._sha(dest) == str(previous.get("pdf_sha256", ""))):
                    pair.outputs.append(PdfOutput(rev, str(dwgs[rev]), str(dest), "PDF_UNCHANGED", str(previous.get("pdf_sha256", "")), dest.stat().st_size, int(previous.get("page_count") or 1), source_sha256=source_sha))
                    continue
                result, path = self._plot(identity, drawing_type, rev, dwgs[rev], stage, source_sha=source_sha)
                pair.outputs.append(result); staged[rev] = path
            for result in pair.outputs:
                if result.revision not in staged: continue
                dest = Path(result.destination_pdf); dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.is_file():
                    b = backup / result.revision / dest.name; b.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dest, b); result.backup_path = str(b)
                temp = dest.with_name(dest.name + ".gni-pdf-" + tid)
                shutil.copy2(staged[result.revision], temp)
                if self._sha(temp) != result.pdf_sha256: raise PdfPublishError("PDF_TEMP_HASH_MISMATCH")
                os.replace(temp, dest); committed.append(result)
                if self._sha(dest) != result.pdf_sha256: raise PdfPublishError("PDF_FINAL_HASH_MISMATCH")
                result.status = "PDF_PUBLISHED"
            pair.status = "PDF_PAIR_PUBLISHED"
        except Exception as error:
            pair.errors.append(repr(error))
            rollback_errors = []
            for result in reversed(committed):
                try:
                    dest = Path(result.destination_pdf)
                    if result.backup_path: shutil.copy2(result.backup_path, dest)
                    else: dest.unlink(missing_ok=True)
                except Exception as restore: rollback_errors.append(repr(restore))
            pair.status = "PDF_ROLLBACK_FAILED" if rollback_errors else "PDF_PUBLISH_FAILED_ROLLED_BACK"
            pair.errors.extend(rollback_errors)
        return pair

    def publish_many(self, transactions, callback=None, workers=1, should_cancel=None):
        """Plot pairs concurrently; reruns skip provably unchanged PDFs.

        The first pair is used as a plotter/media preflight unless the previous
        successful run used the exact same plot configuration. After that a
        failure is recorded per pair instead of stopping a hundreds-file batch.
        """
        transactions = list(transactions); total = len(transactions)
        workers = max(1, int(workers or self.max_workers))
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
        if workers == 1 or len(queue) <= 1:
            for tx in queue:
                if should_cancel and should_cancel(): break
                notify(len(results) + 1, tx["identity"], "Plotting and publishing PDF")
                record(self._publish_pair(tx))
        else:
            def submit_next(pool, pending, iterator):
                try: tx = next(iterator)
                except StopIteration: return False
                pending[pool.submit(self._publish_pair, tx)] = tx
                return True

            iterator = iter(queue); pending = {}
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gni-pdf") as pool:
                for _ in range(workers):
                    if not submit_next(pool, pending, iterator): break
                while pending:
                    done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                    for future in done:
                        pending.pop(future); pair = future.result(); record(pair)
                        notify(len(results), pair.identity, pair.status)
                    if should_cancel and should_cancel():
                        for future in pending: future.cancel()
                        break
                    while len(pending) < workers and submit_next(pool, pending, iterator):
                        pass
        order = {str(tx["identity"]): index for index, tx in enumerate(transactions)}
        return [results[key] for key in sorted(results, key=lambda key: order.get(key, 10**9))]

    def write_report(self, results):
        path=self.reports/"pdf_publish_report.json"
        unchanged=sum(output.status=="PDF_UNCHANGED" for pair in results for output in pair.outputs)
        payload={"schema":"GNI-PDF-Publisher-0.4C.6","version":PDF_PUBLISHER_VERSION,"generated_at":datetime.now().astimezone().isoformat(),"config_signature":self.config_signature,"summary":{"pairs":len(results),"published":sum(x.status=="PDF_PAIR_PUBLISHED" for x in results),"unchanged":unchanged,"failed":sum(x.status!="PDF_PAIR_PUBLISHED" for x in results)},"results":[asdict(x) for x in results]}
        temp=path.with_suffix(".json.tmp");temp.write_text(json.dumps(payload,indent=2,ensure_ascii=False),encoding="utf-8");os.replace(temp,path);return path

