from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
import csv
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Dict, List

from runtime.console_text import decode_console
from runtime.hashing import sha256_file
from runtime.process_tracking import run_console

STABLE_WRITER_VERSION = "0.4C.6-parallel-writer"


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
        os.replace(temporary, destination)
        return destination

    # Shared hasher: see runtime/hashing.py.
    _sha256 = staticmethod(sha256_file)

    # Shared decoder: see runtime/console_text.py (UTF-16LE without BOM).
    _decode = staticmethod(decode_console)

    @staticmethod
    def _scr_path(path):
        return str(Path(path).resolve()).replace("\\", "/").replace('"', '\\"')

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
            if not str(plan.get("status", "")).startswith("READY"):
                errors.append("PLAN_NOT_READY")
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

    def _run_output(self, plan, mode, job):
        started = time.time()
        identity = plan["identity"]
        source = Path(plan["primary_geometry"]).resolve()
        working = job / f"{identity}_{mode}.dwg"
        action_file = job / f"{mode}_actions.tsv"
        result_tsv = job / f"{mode}_result.tsv"
        log = job / f"{mode}_stable_writer.log"
        script = job / f"{mode}_stable_writer.scr"
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
        try:
            self._atomic_copy(source, working)
            actions = self._prepare_actions(plan, mode)
            output.actions_requested = len(actions)
            self._write_action_file(action_file, actions)

            lines = [
                '(setvar "FILEDIA" 0)',
                '(setvar "CMDDIA" 0)',
                '(if (getvar "REPORTERROR") (setvar "REPORTERROR" 0))',
                f'(setvar "TRUSTEDPATHS" "{self._scr_path(self.runtime_root)}")',
                f'(load "{self._scr_path(self.runtime_lisp)}")',
                f'(GNI:RUN-EXTERNAL-WRITER "{mode}" "{self._scr_path(action_file)}" "{self._scr_path(result_tsv)}" "{self.target_layer}" {self.target_color})',
                "",
            ]
            script.write_text("\n".join(lines), encoding="ascii", errors="replace")

            creation_flags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            )
            process = run_console(
                [
                    str(self.core),
                    "/i",
                    str(working),
                    "/s",
                    str(script),
                    "/l",
                    "en-US",
                ],
                capture_output=True,
                text=False,
                timeout=self.timeout,
                cwd=str(job),
                creationflags=creation_flags,
            )
            output.exit_code = process.returncode
            combined = self._decode(process.stdout) + "\n" + self._decode(process.stderr)
            log.write_text(
                f"VERSION={STABLE_WRITER_VERSION}\nEXIT_CODE={process.returncode}\n{combined}",
                encoding="utf-8",
                errors="replace",
            )

            if self.LOAD_MARKER not in combined:
                raise StableWriterError("STABLE_LISP_LOAD_FAILED")
            if self.FAILURE_MARKER in combined:
                raise StableWriterError("STABLE_ACTION_EXECUTION_FAILED")
            if self.SUCCESS_MARKER not in combined:
                raise StableWriterError("STABLE_SUCCESS_MARKER_NOT_FOUND")
            if self.EXIT_MARKER not in combined:
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

    def run_plan(self, plan):
        job = self.job_root / (
            datetime.now().strftime("%Y%m%d_%H%M%S_")
            + plan["drawing_type"]
            + "_"
            + plan["identity"]
            + "_"
            + uuid.uuid4().hex[:8]
        )
        job.mkdir(parents=True, exist_ok=False)
        result = StablePairResult(plan["identity"], plan["drawing_type"], "PROCESSING")
        for mode in ("A", "00"):
            result.outputs.append(self._run_output(plan, mode, job))
        failed = [item for item in result.outputs if item.status == "FAILED"]
        result.status = (
            "FAILED" if failed else "LOCAL_PAIR_WRITTEN_NOT_VALIDATED"
        )
        result.errors = [item.output_revision + ":" + item.error for item in failed]
        return result

    def run_many(self, plans, workers=None, on_result=None, should_cancel=None):
        """Run independent plans through a bounded Core Console worker pool.

        Core Console startup dominates (~5 s per process), so running pairs
        concurrently is the main throughput lever. Each plan owns a unique job
        folder, which keeps concurrent QSAVE writes isolated.
        """
        plans = list(plans)
        workers = max(1, int(workers or self.max_workers))
        results = {}
        order = {str(plan.get("identity")): index for index, plan in enumerate(plans)}

        def record(result):
            results[str(result.identity)] = result
            if on_result:
                on_result(result)

        if workers == 1 or len(plans) <= 1:
            for plan in plans:
                if should_cancel and should_cancel():
                    break
                record(self.run_plan(plan))
        else:
            def submit_next(pool, pending, iterator):
                try:
                    plan = next(iterator)
                except StopIteration:
                    return False
                pending[pool.submit(self.run_plan, plan)] = plan
                return True

            iterator = iter(plans)
            pending = {}
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="gni-writer"
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
        os.replace(temporary, path)
        return path
