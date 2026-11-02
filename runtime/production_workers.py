from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import json
import os
import time
import traceback
import threading

from runtime.atomic_io import atomic_replace

from PySide6.QtCore import QThread, Signal

from destination.builder import DestinationBuilder
from titleblock.discovery import TitleBlockDiscoveryEngine
from revision.completion_plan import TitleBlockCompletionPlanBuilder
from runtime.hashing import sha256_file
from runtime.parallel import run_pool

RUNTIME_VERSION = "0.4C.2D-2E-responsive-production-scale"


class DiscoveryWorker(QThread):
    """Discover every selected drawing, in batches, across a bounded pool.

    Two independent levers against the measured cost profile of the production
    machine (Core Console startup ~4.7 s, scan <1 s per drawing):

    * ``batch_size`` drawings per Core Console process, so startup is amortised
      instead of paid per drawing;
    * ``workers`` batches in flight at once, so the otherwise idle cores are
      actually used.

    Wall clock is roughly ``ceil(N / (batch*workers)) * (startup + batch*scan)``.
    600 drawings at batch=4, workers=12 is ~13 passes instead of 600.
    """

    progress = Signal(int, int, str, str)
    activity = Signal(str, str)
    completed = Signal(list, str, str, bool)
    failed = Signal(str)

    #: Drawings opened per Core Console process. Kept small on purpose: one
    #: hung drawing costs its whole batch, and a batch timeout is a hard cap.
    DEFAULT_BATCH_SIZE = 4

    def __init__(self, groups, core_console, destination_root, bundle_name,
                 source_root, settings, application_root, workers=1, batch_size=None):
        super().__init__()
        self.groups = list(groups)
        self.core_console = core_console
        self.destination_root = destination_root
        self.bundle_name = bundle_name
        self.source_root = source_root
        self.settings = settings
        self.application_root = Path(application_root)
        self.workers = max(1, int(workers or 1))
        # Only `None` means "unset". An explicit 0/negative is clamped to 1
        # (one process per drawing) instead of silently enabling batching.
        self.batch_size = (self.DEFAULT_BATCH_SIZE if batch_size is None
                           else max(1, int(batch_size)))
        self._cancel_requested = False
        self._checkpoint_lock = threading.Lock()

    def cancel(self):
        self._cancel_requested = True

    def run(self):
        try:
            builder = DestinationBuilder(
                self.destination_root, self.bundle_name, self.application_root
            )
            builder.build(self.source_root, copy_assets_to_all_cad=True)
            engine = TitleBlockDiscoveryEngine(
                self.core_console,
                self.application_root / "lisp" / "title_block_discovery.lsp",
                self.settings,
                self.application_root,
            )
            total = len(self.groups)
            todo, already = self._resumable(builder.bundle_root)
            results = list(already)
            if already:
                self.progress.emit(len(already), total, "", f"Resumed {len(already)} from checkpoint")
            done = [len(already)]

            def discover(chunk):
                return engine.discover_batch(chunk)

            def record(chunked):
                # ``on_result`` fires in completion order, so progress is counted
                # here rather than derived from the chunk index.
                results.extend(chunked)
                with self._checkpoint_lock:
                    self._write_checkpoint(builder.bundle_root, results, total,
                                           self._cancel_requested)
                done[0] += len(chunked)
                self.progress.emit(done[0], total, str(getattr(chunked[0], "identity", "")),
                                   "Batch completed")

            batches = engine._batches(todo, self.batch_size)
            for index, _ in enumerate(batches, 1):
                identity = str(getattr(batches[index - 1][0], "identity", ""))
                self.progress.emit(done[0], total, identity,
                                   f"Opening batch {index} of {len(batches)}")
            run_pool(discover, batches, workers=self.workers, on_result=record,
                     should_cancel=lambda: self._cancel_requested)
            ordered = {str(item.identity): item for item in results}
            results = [ordered[str(group.identity)] for group in self.groups
                       if str(group.identity) in ordered]
            cancelled = self._cancel_requested and len(results) < total
            json_path, csv_path = engine.write_report(
                builder.bundle_root, results, complete=not cancelled
            )
            with self._checkpoint_lock:
                self._write_checkpoint(builder.bundle_root, results, total, cancelled)
            self.completed.emit(
                results, str(json_path), str(csv_path), cancelled
            )
        except Exception:
            self.failed.emit(traceback.format_exc())

    def _resumable(self, bundle_root):
        """Return ``(groups still to run, results already SUCCESS)``.

        The checkpoint used to be written every drawing and read by nobody, so
        a cancelled sweep was re-run from zero. A record is only reused when the
        source DWG still hashes to the recorded value; a re-saved or replaced
        drawing is discovered again rather than trusted.
        """
        previous = self._load_checkpoint(bundle_root)
        if not previous:
            return list(self.groups), []
        accepted, todo = [], []
        for group in self.groups:
            record = self._load_checkpoint_record(previous, group)
            result = self._reusable(record, group) if record else None
            if result is not None:
                accepted.append(result)
            else:
                todo.append(group)
        return todo, accepted

    @staticmethod
    def _load_checkpoint_record(previous, group):
        return previous.get(str(group.identity))

    @staticmethod
    def _load_checkpoint(bundle_root):
        path = Path(bundle_root) / "_GNI_REPORTS" / "title_block_discovery_checkpoint.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            records = payload.get("results") or []
        except (OSError, ValueError, AttributeError):
            return {}
        return {
            str(item.get("identity")): item
            for item in records
            if isinstance(item, dict) and item.get("status") == "SUCCESS"
        }

    @staticmethod
    def _reusable(record, group):
        """Rebuild a SUCCESS result from *record*, or return None.

        Only the source hash decides. A drawing re-saved since the checkpoint
        is discovered again instead of trusted, which keeps the "never promote
        stale data" rule the writer checkpoint already follows.
        """
        from titleblock.discovery import DiscoveryField, DiscoveryResult

        try:
            source = Path(group.primary_source)
            if not source.is_file():
                return None
            if record.get("source_sha256") != sha256_file(source):
                return None
            allowed = set(DiscoveryField.__dataclass_fields__)
            fields = [
                DiscoveryField(**{key: value for key, value in item.items() if key in allowed})
                for item in record.get("fields") or []
                if isinstance(item, dict)
            ]
            result = DiscoveryResult(
                group.identity,
                group.drawing_type,
                str(source.resolve()),
                "SUCCESS",
                str(record.get("job_folder", "")),
                tsv_path=str(record.get("tsv_path", "")),
                log_path=str(record.get("log_path", "")),
                fields=fields,
                error="",
                elapsed_seconds=0.0,
            )
            result._resumed = True
            return result
        except (TypeError, ValueError, OSError, KeyError):
            return None

    _sha_cache = {}

    @classmethod
    def _source_sha(cls, path):
        """SHA-256 of a source DWG, recomputed only when size or mtime changes.

        The checkpoint is rewritten after every batch and used to re-hash every
        finished drawing each time: O(batches x drawings) full reads of the
        source share. A drawing's bytes cannot change without its size or mtime
        changing, so the cache key is exact.
        """
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        digest = cls._sha_cache.get(key)
        if digest is None:
            digest = sha256_file(path)
            cls._sha_cache[key] = digest
        return digest

    @staticmethod
    def _write_checkpoint(bundle_root, results, total, cancelled):
        report_root = Path(bundle_root) / "_GNI_REPORTS"
        report_root.mkdir(parents=True, exist_ok=True)
        path = report_root / "title_block_discovery_checkpoint.json"
        records = []
        for item in results:
            record = asdict(item)
            # `_group` holds a live scanner object and must never be serialised.
            record.pop("_group", None)
            record.pop("_resumed", None)
            # Only a SUCCESS record may be resumed, and only against the exact
            # source bytes, so every stored record carries its source hash.
            if record.get("status") == "SUCCESS":
                try:
                    record["source_sha256"] = DiscoveryWorker._source_sha(Path(record["source_path"]))
                except OSError:
                    record["status"] = "FAILED"
                    record["error"] = record.get("error") or "SOURCE_UNREADABLE"
            records.append(record)
        payload = {
            "schema": "GNI-Discovery-Checkpoint-0.4C.24",
            "version": RUNTIME_VERSION,
            "updated_at": datetime.now().astimezone().isoformat(),
            "total_drawings": total,
            "completed_drawings": len(records),
            "cancelled": bool(cancelled),
            "results": records,
        }
        temporary = path.with_name(
            path.name + ".tmp-" + str(os.getpid()) + "-" + str(threading.get_ident())
        )
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        atomic_replace(temporary, path)


class ProductionPlanWorker(QThread):
    progress = Signal(int, int, str, str)
    completed = Signal(list, str, str, str, str, bool)
    failed = Signal(str)

    #: Drawings planned per planner call (one discovery-CSV parse each).
    PLAN_CHUNK = 16

    def __init__(self, groups, discovery_csv, destination_root, bundle_name,
                 source_root, settings, application_root):
        super().__init__()
        self.groups = list(groups)
        self.discovery_csv = Path(discovery_csv)
        self.destination_root = destination_root
        self.bundle_name = bundle_name
        self.source_root = source_root
        self.settings = settings
        self.application_root = Path(application_root)
        self._cancel_requested = False

    def cancel(self):
        self._cancel_requested = True

    def run(self):
        try:
            builder = DestinationBuilder(
                self.destination_root, self.bundle_name, self.application_root
            )
            builder.build(self.source_root, copy_assets_to_all_cad=True)
            planner = TitleBlockCompletionPlanBuilder(self.settings)
            plans = []
            total = len(self.groups)
            fragments = builder.bundle_root / "_GNI_REPORTS" / "plans"
            fragments.mkdir(parents=True, exist_ok=True)
            # The planner re-parses the discovery CSV on every build() call and
            # the index is rewritten after each call, so planning one drawing at
            # a time was O(drawings x CSV) plus O(drawings^2) index bytes. Plans
            # are independent per drawing, so a chunk gives identical output with
            # one CSV parse and one index write per chunk.
            for start in range(0, total, self.PLAN_CHUNK):
                if self._cancel_requested:
                    break
                chunk = self.groups[start:start + self.PLAN_CHUNK]
                last = start + len(chunk)
                identity = str(getattr(chunk[-1], "identity", ""))
                self.progress.emit(last, total, identity, "Building spatial plan")
                built = planner.build(chunk, self.discovery_csv)
                plans.extend(built)
                for plan in built:
                    self._write_fragment(fragments, plan)
                self._write_index(fragments, plans, total, self._cancel_requested)
                self.progress.emit(last, total, identity, "Plan checkpoint saved")
            outputs = planner.write_reports(builder.bundle_root, plans)
            cancelled = self._cancel_requested and len(plans) < total
            self._mark_plan_report_complete(builder.bundle_root, complete=not cancelled, cancelled=cancelled)
            self._write_index(fragments, plans, total, cancelled)
            self.completed.emit(
                plans, *(str(path) for path in outputs), cancelled
            )
        except Exception:
            self.failed.emit(traceback.format_exc())

    @staticmethod
    def _safe_name(value):
        return "".join(
            char if char.isalnum() or char in "-_." else "_"
            for char in str(value)
        )

    @classmethod
    def _write_fragment(cls, root, plan):
        path = root / (cls._safe_name(plan.identity) + ".json")
        temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()) + "-" + str(threading.get_ident()))
        temporary.write_text(
            json.dumps(asdict(plan), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        atomic_replace(temporary, path)

    @staticmethod
    def _mark_plan_report_complete(bundle_root, complete=True, cancelled=False):
        """Stamp ``complete`` into the plan JSON so a cancelled run is detectable.

        The planner writes ``revision_pilot_write_plan.json`` through several
        layers (base -> cleanup -> completion), so the flag is added once here
        rather than threaded through every writer signature.
        """
        path = Path(bundle_root) / "_GNI_REPORTS" / "revision_pilot_write_plan.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        payload["complete"] = bool(complete)
        payload["cancelled"] = bool(cancelled)
        temporary = path.with_name(
            path.name + ".tmp-" + str(os.getpid()) + "-" + str(threading.get_ident())
        )
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        atomic_replace(temporary, path)

    @staticmethod
    def _write_index(root, plans, total, cancelled):
        path = root / "index.json"
        payload = {
            "schema": "GNI-Production-Plan-Index-0.4C.7",
            "version": RUNTIME_VERSION,
            "updated_at": datetime.now().astimezone().isoformat(),
            "total_drawings": total,
            "completed_drawings": len(plans),
            "cancelled": bool(cancelled),
            "complete": not bool(cancelled),
            "ready": sum(str(item.status).startswith("READY") for item in plans),
            "blocked": sum(item.status == "BLOCKED" for item in plans),
            "plans": [
                {
                    "identity": item.identity,
                    "drawing_type": item.drawing_type,
                    "status": item.status,
                    "actions": len(item.actions),
                    "warnings": list(item.warnings),
                    "errors": list(item.errors),
                }
                for item in plans
            ],
        }
        temporary = path.with_name(
            path.name + ".tmp-" + str(os.getpid()) + "-" + str(threading.get_ident())
        )
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        atomic_replace(temporary, path)
        # Called explicitly instead of patching _write_index at import time
        # (GNI_MANUAL_CHECK_REPORT_0_4C_2F_1 used to monkey-patch this method).
        from revision.recovery_00 import write_manual_check_report

        write_manual_check_report(root.parent, plans, cancelled)
