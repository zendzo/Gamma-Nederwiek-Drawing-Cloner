from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
import csv
import json
import os
import shutil
import struct
import subprocess
import tempfile
import time
import uuid
from typing import Dict, List

from runtime.batching import clamp_batch_size, run_batched
from runtime.atomic_io import atomic_replace
from runtime.console_text import decode_console
from runtime.hashing import sha256_file
from runtime.process_tracking import run_console

# 0.4C.26: one Core Console process writes a whole batch of plans (A and 00 per
# plan) instead of one process per output. Startup is ~4.5 s and a write is
# 0.5-2 s, so batching is the difference between hours and minutes at 700 files.
# 0.4C.32: before QSAVE the writer normalizes reference rasters. Older Gamma
# drawings stored a 922x260 Tennet IMAGEDEF while the packaged PNG is 1136x305;
# AutoCAD renders at the file size, so the logo grew ~23% and overlapped its
# neighbour. The writer sees the original size (its working copy has no PNGs).
STABLE_WRITER_VERSION = "0.4C.32-logo-raster-scale"


class StableWriterError(RuntimeError):
    pass


@dataclass
class StableOutput:
    identity: str
    drawing_type: str
    output_revision: str
    status: str
    source_path: str
    working_dwg: str
    job_folder: str
    action_file: str = ""
    result_tsv: str = ""
    log_path: str = ""
    actions_requested: int = 0
    actions_written: int = 0
    actions_moved: int = 0
    actions_verified: int = 0
    actions_failed: int = 0
    sha256: str = ""
    elapsed_seconds: float = 0.0
    exit_code: int | None = None
    error: str = ""


@dataclass
class StablePairResult:
    identity: str
    drawing_type: str
    status: str
    outputs: List[StableOutput] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class StableExternalActionWriter:
    LOAD_MARKER = "GNI_STABLE_EXTERNAL_WRITER_0_4C_0_LOADED"
    SUCCESS_MARKER = "GNI_STABLE_EXTERNAL_WRITER_OK:"
    FAILURE_MARKER = "GNI_STABLE_EXTERNAL_WRITER_FAILED:"
    EXIT_MARKER = "GNI_STABLE_EXTERNAL_WRITER_EXIT_READY"
    # Reference rasters whose file pixel size may differ from the IMAGEDEF
    # stored in an older drawing. See STABLE_WRITER_VERSION 0.4C.32.
    RASTER_ASSETS = ("SEAtrium_Ge_.PNG", "Tennet LogO New.PNG")

    def __init__(
        self,
        core_console,
        lisp_source,
        application_root,
        timeout=240,
    ):
        self.core = Path(core_console).resolve()
        self.lisp_source = Path(lisp_source).resolve()
        self.application_root = Path(application_root).resolve()
        self.timeout = int(timeout)
        if not self.core.is_file():
            raise FileNotFoundError(f"accoreconsole.exe not found: {self.core}")
        if not self.lisp_source.is_file():
            raise FileNotFoundError(f"Stable writer LSP not found: {self.lisp_source}")

        self.settings = self._load_settings()
        self.max_workers = max(1, int(self.settings.get("max_workers", 4)))
        self.target_layer = str(
            self.settings.get("target_layer_name", "NEDERWIEK_DATA")
        )
        self.target_color = int(self.settings.get("target_layer_color_aci", 6))
        self.source_layers = {
            str(value).casefold()
            for value in self.settings.get(
                "target_source_layers", ["Other", "Other object type"]
            )
        }

        temp = Path(os.environ.get("TEMP") or tempfile.gettempdir()).resolve()
        self.job_root = temp / "GNI" / "stable_external_writer"
        self.runtime_root = temp / "GNI" / "trusted_runtime"
        self.job_root.mkdir(parents=True, exist_ok=True)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.runtime_lisp = self.runtime_root / "stable_external_writer.lsp"
        self._atomic_copy(self.lisp_source, self.runtime_lisp)

    def _load_settings(self):
        path = self.application_root / "revision" / "writer_settings.json"
        if not path.is_file():
            return {
                "target_layer_name": "NEDERWIEK_DATA",
                "target_layer_color_aci": 6,
                "target_source_layers": ["Other", "Other object type"],
            }
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _atomic_copy(source, destination):
        source = Path(source)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(
            destination.suffix + ".copying-" + uuid.uuid4().hex[:8]
        )
        with source.open("rb", buffering=0) as src, temporary.open(
            "wb", buffering=0
        ) as dst:
            shutil.copyfileobj(src, dst, 4 * 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        if temporary.stat().st_size != source.stat().st_size:
            temporary.unlink(missing_ok=True)
            raise OSError("Copy size mismatch")
        atomic_replace(temporary, destination)
        return destination

    # Shared hasher: see runtime/hashing.py.
    _sha256 = staticmethod(sha256_file)

    # Shared decoder: see runtime/console_text.py (UTF-16LE without BOM).
    _decode = staticmethod(decode_console)

    @staticmethod
    def _scr_path(path):
        return str(Path(path).resolve()).replace("\\", "/").replace('"', '\\"')

    @staticmethod
    def _png_size(path):
        """Pixel size from a PNG IHDR chunk, or None when unreadable."""
        try:
            data = Path(path).read_bytes()
        except OSError:
            return None
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            return struct.unpack(">II", data[16:24])
        return None

    def _raster_map_rows(self, drawing_type):
        """filename TAB width TAB height for this type's reference PNGs.

        The writer's working copy has no PNG beside it, so AutoLISP still sees
        the drawing's *original* IMAGEDEF size. These rows tell it what the
        packaged file really is, letting it rescale without guessing.
        """
        app = getattr(self, "application_root", None)
        if app is None:
            return []
        folder = (
            Path(app) / "resources" / "reference_images"
            / str(drawing_type).strip().upper()
        )
        rows = []
        for name in self.RASTER_ASSETS:
            size = self._png_size(folder / name)
            if size:
                rows.append(f"{name}\t{size[0]}\t{size[1]}")
        return rows

    @staticmethod
    def _clean_field(value):
        return (
            str(value or "")
            .replace("\t", " ")
            .replace("\r", " ")
            .replace("\n", " ")
        )

    @classmethod
    def load_ready_plans(cls, path, return_rejected=False):
        """Return executable plans, dropping (not aborting on) rejected ones.

        A single BLOCKED drawing used to raise ``STABLE_WRITER_PREFLIGHT_FAILED``
        and stop the whole batch. Now the rejected entries are returned so the UI
        can report them while the rest of the migration proceeds.
        """
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        plans = []
        rejected = []
        for plan in payload.get("plans", []):
            identity = plan.get("identity", "")
            errors = []
            status = str(plan.get("status", ""))
            if not status.startswith("READY"):
                # A BLOCKED plan has no usable actions *by design*, so listing
                # NO_ACTIONS for it only buried the real reason. Report the
                # status and stop collecting secondary defects.
                errors.append("PLAN_NOT_READY:" + (status or "UNKNOWN"))
                rejected.append({"identity": identity, "errors": errors})
                continue
            primary = Path(str(plan.get("primary_geometry", "")))
            if not primary.is_file():
                errors.append("PRIMARY_GEOMETRY_NOT_FOUND")
            if str(plan.get("revision_a_donor", "") or "").strip():
                errors.append("EXTERNAL_A_DONOR_NOT_ALLOWED")
            for mode in ("A", "00"):
                actions = [
                    action
                    for action in plan.get("actions", [])
                    if action.get("output_revision") == mode
                ]
                if not actions:
                    errors.append("NO_ACTIONS:" + mode)
                handles = [
                    action.get("handle")
                    for action in actions
                    if action.get("action") in ("WRITE", "MOVE_ONLY", "VERIFY_ONLY")
                ]
                if any(not str(handle or "").strip() for handle in handles):
                    errors.append("EMPTY_HANDLE:" + mode)
            if errors:
                rejected.append({"identity": identity, "errors": errors})
            else:
                plans.append(plan)
        if not plans:
            detail = "; ".join(
                item["identity"] + "=" + ",".join(item["errors"]) for item in rejected
            )
            raise StableWriterError(
                "STABLE_WRITER_PREFLIGHT_FAILED: " + detail if rejected
                else "NO_READY_STABLE_WRITER_PLANS"
            )
        if return_rejected:
            return plans, rejected
        return plans

    def _prepare_actions(self, plan: Dict, mode: str):
        """Consolidate value and area operations into one action per handle.

        Priority:
        clear/write > value write > move only > verify only.
        Layer movement is merged into the winning action. Revision-area reasons
        always force NEDERWIEK_DATA, regardless of source layer.
        """
        raw_actions = [
            dict(action)
            for action in plan.get("actions", [])
            if action.get("output_revision") == mode
            and str(action.get("action", "")).upper()
            in ("WRITE", "MOVE_ONLY", "VERIFY_ONLY")
        ]
        if not raw_actions:
            raise StableWriterError("NO_EXECUTABLE_ACTIONS:" + mode)

        area_reason_prefixes = (
            "MOVE_RETAINED_",
            "CLEAR_OUTPUT_A_",
            "CLEAR_OUTPUT_00_",
            "CLEAR_NON_OUTPUT_REVISION_ROW",
        )

        def is_area(action):
            reason = str(action.get("reason", ""))
            field = str(action.get("target_field", ""))
            return reason.startswith(area_reason_prefixes) or field.startswith(
                ("RETAINED_", "REVISION_AREA_")
            )

        def rank(action):
            action_mode = str(action.get("action", "")).upper()
            new_value = self._clean_field(action.get("new_value", ""))
            if action_mode == "WRITE" and new_value == "":
                return 400
            if action_mode == "WRITE":
                return 300
            if action_mode == "MOVE_ONLY":
                return 200
            return 100

        grouped = {}
        order = []
        for action in raw_actions:
            handle = str(action.get("handle", "")).strip()
            if not handle:
                continue
            if handle not in grouped:
                grouped[handle] = []
                order.append(handle)
            grouped[handle].append(action)

        prepared = []
        for sequence, handle in enumerate(order, 1):
            candidates = grouped[handle]
            winner = max(candidates, key=rank)
            action_mode = str(winner.get("action", "")).upper()

            force_layer = any(is_area(action) for action in candidates)
            source_layer_allowed = str(
                winner.get("layer", "")
            ).casefold() in self.source_layers
            target_layer = (
                self.target_layer
                if action_mode in ("WRITE", "MOVE_ONLY")
                and (force_layer or source_layer_allowed)
                else ""
            )

            # VERIFY_ONLY plus an area MOVE_ONLY becomes MOVE_ONLY. A WRITE plus
            # MOVE_ONLY remains WRITE but inherits the target layer.
            if action_mode == "VERIFY_ONLY" and force_layer:
                action_mode = "MOVE_ONLY"
                target_layer = self.target_layer

            reasons = []
            for action in candidates:
                reason = self._clean_field(action.get("reason", ""))
                if reason and reason not in reasons:
                    reasons.append(reason)

            prepared.append(
                {
                    "sequence": sequence,
                    "handle": handle,
                    "entity_type": self._clean_field(
                        winner.get("entity_type", "")
                    ),
                    "tag": self._clean_field(winner.get("tag", "")),
                    "old_value": self._clean_field(winner.get("old_value", "")),
                    "new_value": self._clean_field(winner.get("new_value", "")),
                    "target_field": self._clean_field(
                        winner.get("target_field", "")
                    ),
                    "action": action_mode,
                    "expected_layer": self._clean_field(
                        winner.get("layer", "")
                    ),
                    "target_layer": target_layer,
                    "reason": ";".join(reasons),
                }
            )

        if not prepared:
            raise StableWriterError("NO_EXECUTABLE_ACTIONS:" + mode)
        return prepared

    @staticmethod
    def _write_action_file(path, actions):
        fields = [
            "sequence",
            "handle",
            "entity_type",
            "tag",
            "old_value",
            "new_value",
            "target_field",
            "action",
            "expected_layer",
            "target_layer",
            "reason",
        ]
        # cp1252 keeps AutoCAD 2018 text-file behavior deterministic. Control
        # characters were removed by _clean_field before this stage.
        with Path(path).open("w", encoding="cp1252", errors="replace", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, delimiter="\t")
            writer.writeheader()
            writer.writerows(actions)

    @staticmethod
    def _safe(value):
        return "".join(
            char if char.isalnum() or char in "._-" else "_" for char in str(value)
        )[:80]

    def _new_job(self, name):
        """Fresh job folder; a name collision must never reuse a stale one."""
        for _ in range(64):
            job = self.job_root / (
                datetime.now().strftime("%Y%m%d_%H%M%S_")
                + self._safe(name)
                + "_"
                + uuid.uuid4().hex[:8]
            )
            try:
                job.mkdir(parents=True, exist_ok=False)
                return job
            except FileExistsError:
                continue
        raise OSError(f"Could not allocate a stable-writer job folder for {name}")

    def _stage_output(self, plan, mode, job):
        """Copy the pristine source and write this output's action TSV.

        Staging is per (plan, mode) on purpose: the A and 00 outputs are written
        from the same source but must never share a working DWG, or one output's
        QSAVE would leak into the other.
        """
        identity = plan["identity"]
        source = Path(plan["primary_geometry"]).resolve()
        working = job / f"{self._safe(identity)}_{mode}.dwg"
        action_file = job / f"{mode}_actions.tsv"
        result_tsv = job / f"{mode}_result.tsv"
        log = job / f"{mode}_stable_writer.log"
        output = StableOutput(
            identity,
            plan["drawing_type"],
            mode,
            "PROCESSING",
            str(source),
            str(working),
            str(job),
            str(action_file),
            str(result_tsv),
            str(log),
        )
        actions = self._prepare_actions(plan, mode)
        output.actions_requested = len(actions)
        self._atomic_copy(source, working)
        self._write_action_file(action_file, actions)
        rows = self._raster_map_rows(plan["drawing_type"])
        if rows:
            (job / "raster_map.tsv").write_text(
                "\n".join(rows) + "\n", encoding="ascii"
            )
        return output

    def _script_header(self):
        return [
            '(setvar "FILEDIA" 0)',
            '(setvar "CMDDIA" 0)',
            '(if (getvar "REPORTERROR") (setvar "REPORTERROR" 0))',
            f'(setvar "TRUSTEDPATHS" "{self._scr_path(self.runtime_root)}")',
            f'(load "{self._scr_path(self.runtime_lisp)}")',
        ]

    def _writer_call(self, output):
        map_path = Path(output.job_folder) / "raster_map.tsv"
        map_arg = f'"{self._scr_path(map_path)}"' if map_path.is_file() else '""'
        return (
            f'(GNI:RUN-EXTERNAL-WRITER "{output.output_revision}" '
            f'"{self._scr_path(output.action_file)}" '
            f'"{self._scr_path(output.result_tsv)}" '
            f'"{self.target_layer}" {self.target_color} {map_arg})'
        )

    @staticmethod
    def _dirty_guard():
        """Save a modified document so the next `_.OPEN` cannot prompt.

        A failed action set leaves the working copy dirty. `_.OPEN` on a dirty
        drawing asks "Do you want to save changes?", and in a `.scr` that
        question swallows the rest of the batch. The working copy is a throwaway
        under %TEMP% and its result TSV already records the failure, so saving it
        is safe and keeps the remaining drawings of the batch alive.
        """
        return (
            '(if (/= 0 (getvar "DBMOD")) '
            '(progn (princ "\\nGNI_BATCH_DIRTY_DOCUMENT_SAVED") '
            '(vl-catch-all-apply (quote command-s) (list "_.QSAVE"))))'
        )

    @staticmethod
    def _open_line(path):
        """Open the next drawing from LISP so spaces in the path survive.

        A ``.scr`` feeds one line per prompt, so a bare ``_.OPEN C:/a b/x.dwg``
        is truncated at the space and Core Console then waits for the rest of
        the name until the batch timeout. ``command-s`` takes the path as a
        single string (the discovery batch uses the same call).
        """
        target = str(Path(path).resolve()).replace("\\", "/").replace('"', '\\"')
        return ('(vl-catch-all-apply (quote (lambda () '
                '(command-s "_.OPEN" "' + target + '"))))')

    def _verify_output(self, output, combined, exit_code, started, strict):
        """Decide one output's status from *its own* artefacts.

        Console output is shared by every drawing in the process, so in a batch
        only the per-output result TSV can prove that this output was written.
        That is the same rule the batched discovery stage uses for its TSV.
        """
        result_tsv = Path(output.result_tsv)
        working = Path(output.working_dwg)
        output.exit_code = exit_code
        try:
            if self.LOAD_MARKER not in combined:
                raise StableWriterError("STABLE_LISP_LOAD_FAILED")
            if strict and self.FAILURE_MARKER in combined:
                raise StableWriterError("STABLE_ACTION_EXECUTION_FAILED")
            if strict and self.SUCCESS_MARKER not in combined:
                raise StableWriterError("STABLE_SUCCESS_MARKER_NOT_FOUND")
            if strict and self.EXIT_MARKER not in combined:
                raise StableWriterError("STABLE_EXIT_MARKER_NOT_FOUND")
            if not result_tsv.is_file():
                raise StableWriterError("STABLE_RESULT_MISSING")

            with result_tsv.open(
                "r", encoding="utf-8-sig", errors="replace", newline=""
            ) as stream:
                rows = list(csv.DictReader(stream, delimiter="\t"))
            output.actions_written = sum(row.get("Status") == "WRITTEN" for row in rows)
            output.actions_moved = sum(row.get("Status") == "MOVED" for row in rows)
            output.actions_verified = sum(row.get("Status") == "VERIFIED" for row in rows)
            output.actions_failed = sum(row.get("Status") == "FAILED" for row in rows)
            if output.actions_failed:
                raise StableWriterError(
                    f"STABLE_ACTIONS_FAILED:{output.actions_failed}"
                )
            if len(rows) != output.actions_requested:
                raise StableWriterError(
                    f"STABLE_RESULT_COUNT_MISMATCH:{len(rows)}:{output.actions_requested}"
                )
            output.sha256 = self._sha256(working)
            output.status = "LOCAL_WRITE_SUCCESS_NOT_VALIDATED"
        except subprocess.TimeoutExpired:
            output.status = "FAILED"
            output.error = f"CORE_TIMEOUT:{self.timeout}"
        except Exception as error:
            output.status = "FAILED"
            output.error = repr(error)
        output.elapsed_seconds = round(time.time() - started, 3)
        return output

    def _failed_output(self, plan, mode, job, error, started):
        output = StableOutput(
            plan["identity"], plan["drawing_type"], mode, "FAILED",
            str(Path(plan["primary_geometry"]).resolve()),
            str(job / f"{self._safe(plan['identity'])}_{mode}.dwg"), str(job),
            result_tsv=str(job / f"{mode}_result.tsv"),
            log_path=str(job / f"{mode}_stable_writer.log"),
        )
        output.error = error
        output.elapsed_seconds = round(time.time() - started, 3)
        return output

    def _run_output(self, plan, mode, job):
        started = time.time()
        try:
            output = self._stage_output(plan, mode, job)
        except Exception as error:
            return self._failed_output(plan, mode, job, repr(error), started)
        script = job / f"{mode}_stable_writer.scr"
        script.write_text(
            "\n".join([*self._script_header(), self._writer_call(output), ""]),
            encoding="ascii",
            errors="replace",
        )
        creation_flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        )
        try:
            process = run_console(
                [str(self.core), "/i", str(output.working_dwg), "/s", str(script),
                 "/l", "en-US"],
                capture_output=True,
                text=False,
                timeout=self.timeout,
                cwd=str(job),
                creationflags=creation_flags,
            )
        except subprocess.TimeoutExpired:
            Path(output.log_path).write_text(
                f"VERSION={STABLE_WRITER_VERSION}\nSTATUS=FAILED\n"
                f"ERROR=CORE_TIMEOUT:{self.timeout}\n",
                encoding="utf-8", errors="replace",
            )
            output.status = "FAILED"
            output.error = f"CORE_TIMEOUT:{self.timeout}"
            output.elapsed_seconds = round(time.time() - started, 3)
            return output
        combined = self._decode(process.stdout) + "\n" + self._decode(process.stderr)
        Path(output.log_path).write_text(
            f"VERSION={STABLE_WRITER_VERSION}\nEXIT_CODE={process.returncode}\n{combined}",
            encoding="utf-8",
            errors="replace",
        )
        return self._verify_output(
            output, combined, process.returncode, started, strict=True
        )

    @staticmethod
    def _finish_pair(pair):
        failed = [item for item in pair.outputs if item.status == "FAILED"]
        if failed or pair.status == "FAILED" or len(pair.outputs) != 2:
            pair.status = "FAILED"
            pair.errors = list(pair.errors) + [
                item.output_revision + ":" + item.error for item in failed
            ]
            if not pair.errors:
                pair.errors = ["PAIR_OUTPUTS_INCOMPLETE"]
        else:
            pair.status = "LOCAL_PAIR_WRITTEN_NOT_VALIDATED"
            pair.errors = []
        return pair

    def run_plan(self, plan):
        job = self._new_job(plan["drawing_type"] + "_" + plan["identity"])
        result = StablePairResult(plan["identity"], plan["drawing_type"], "PROCESSING")
        for mode in ("A", "00"):
            result.outputs.append(self._run_output(plan, mode, job))
        return self._finish_pair(result)

    def run_batch(self, plans):
        """Write every plan's A and 00 output inside ONE Core Console process.

        Core Console startup (~4.5 s) dwarfs a single write (0.5-2 s), so a
        process that opens drawing after drawing is the only lever that removes
        that cost. Every output keeps its own folder, working DWG, action TSV
        and result TSV, and each one is judged only by its own artefacts, so a
        sibling's success can never promote it.
        """
        plans = list(plans)
        if not plans:
            return []
        if len(plans) == 1:
            return [self.run_plan(plans[0])]
        started = time.time()
        batch = self._new_job("batch_" + str(plans[0].get("identity", "")))
        batch_log = batch / "batch.log"
        batch_log.write_text(
            f"VERSION={STABLE_WRITER_VERSION}\nBATCH_PLANS={len(plans)}\n",
            encoding="utf-8",
        )
        pairs = []
        runnable = []
        for plan in plans:
            pair = StablePairResult(
                plan["identity"], plan["drawing_type"], "PROCESSING"
            )
            pairs.append(pair)
            try:
                job = batch / self._safe(plan["identity"])
                job.mkdir(parents=True, exist_ok=False)
            except OSError as error:
                pair.status = "FAILED"
                pair.errors = [f"STAGING_FAILED:{error!r}"]
                continue
            for mode in ("A", "00"):
                try:
                    pair.outputs.append(self._stage_output(plan, mode, job))
                except Exception as error:
                    pair.outputs.append(
                        self._failed_output(plan, mode, job, repr(error), started)
                    )
            if any(item.status == "PROCESSING" for item in pair.outputs):
                runnable.append(pair)

        if runnable:
            outputs = [item for pair in runnable for item in pair.outputs
                       if item.status == "PROCESSING"]
            lines = list(self._script_header())
            for index, output in enumerate(outputs):
                if index:
                    # LISP state does not survive `_.OPEN`, so the header is
                    # re-issued before every write.
                    lines.extend([self._dirty_guard(),
                                  self._open_line(output.working_dwg)])
                    lines.extend(self._script_header())
                lines.append(self._writer_call(output))
            lines.extend([self._dirty_guard(), "_.QUIT", "_Y"])
            script = batch / "batch_writer.scr"
            script.write_text("\n".join(lines) + "\n", encoding="ascii",
                              errors="replace")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            # Every output keeps the full single-drawing allowance, so batching
            # never tightens the timeout a serial run would have had.
            timeout = self.timeout * max(1, len(outputs))
            combined, failure = "", None
            try:
                process = run_console(
                    [str(self.core), "/i", str(outputs[0].working_dwg), "/s",
                     str(script), "/l", "en-US"],
                    capture_output=True, text=False, timeout=timeout,
                    cwd=str(batch), creationflags=flags,
                )
                combined = (self._decode(process.stdout) + "\n"
                            + self._decode(process.stderr))
                exit_code = process.returncode
                with batch_log.open("a", encoding="utf-8", errors="replace") as stream:
                    stream.write(f"\nSTATUS=CORE_COMPLETED\nEXIT_CODE={exit_code}\n"
                                 f"{combined}")
            except subprocess.TimeoutExpired as error:
                combined = self._decode(error.stdout) + "\n" + self._decode(error.stderr)
                exit_code = None
                failure = f"CORE_TIMEOUT_BATCH:{len(outputs)}:{timeout}"
                with batch_log.open("a", encoding="utf-8", errors="replace") as stream:
                    stream.write(f"\nSTATUS=FAILED\nERROR={failure}\n{combined}")
            except Exception as error:
                combined, exit_code = "", None
                failure = f"STABLE_WRITER_BATCH_FAILED:{error!r}"
                with batch_log.open("a", encoding="utf-8", errors="replace") as stream:
                    stream.write(f"\nSTATUS=FAILED\nERROR={failure}\n")
            for output in outputs:
                if failure:
                    output.status = "FAILED"
                    output.error = failure
                    output.elapsed_seconds = round(time.time() - started, 3)
                    Path(output.log_path).write_text(
                        f"VERSION={STABLE_WRITER_VERSION}\nBATCH={batch.name}\n"
                        f"STATUS=FAILED\nERROR={failure}\n{combined}",
                        encoding="utf-8", errors="replace",
                    )
                    continue
                self._verify_output(output, combined, exit_code, started, strict=False)
                detail = (f"VERSION={STABLE_WRITER_VERSION}\nBATCH={batch.name}\n"
                          f"EXIT_CODE={exit_code}\nSTATUS={output.status}\n"
                          f"ERROR={output.error}\n")
                if output.status == "FAILED":
                    detail += combined
                Path(output.log_path).write_text(detail, encoding="utf-8",
                                                 errors="replace")
        return [self._finish_pair(pair) for pair in pairs]

    def run_many(self, plans, workers=None, on_result=None, should_cancel=None,
                 batch_size=1):
        """Write independent plans through a bounded pool of batched processes.

        Two levers, exactly like the discovery stage: ``batch_size`` plans per
        Core Console process and ``workers`` processes in flight. Each plan owns
        a unique job folder, which keeps concurrent QSAVE writes isolated.
        """
        plans = list(plans)
        workers = max(1, int(workers or self.max_workers))
        batch_size = clamp_batch_size(batch_size, default=1)
        return run_batched(
            self.run_plan, self.run_batch, plans,
            batch_size=batch_size, workers=workers,
            on_result=on_result, should_cancel=should_cancel,
        )

    @staticmethod
    def write_report(bundle_root, results):
        reports = Path(bundle_root) / "_GNI_REPORTS"
        reports.mkdir(parents=True, exist_ok=True)
        path = reports / "stable_external_writer_report.json"
        payload = {
            "schema": "GNI-Stable-External-Writer-0.4C.0",
            "version": STABLE_WRITER_VERSION,
            "generated_at": datetime.now().astimezone().isoformat(),
            "publishing_enabled": False,
            "validation_required_before_publish": True,
            "summary": {
                "plans": len(results),
                "local_pairs": sum(
                    item.status == "LOCAL_PAIR_WRITTEN_NOT_VALIDATED"
                    for item in results
                ),
                "failed": sum(item.status == "FAILED" for item in results),
            },
            "results": [asdict(item) for item in results],
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        atomic_replace(temporary, path)
        return path
