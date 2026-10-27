from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import json
import os
import time
import traceback

from PySide6.QtCore import QThread, Signal

from revision.reopen_validator import StableReopenValidator

RESPONSIVE_VALIDATOR_VERSION = "0.4C.3.2-responsive-reopen-validator"


class ResponsiveReopenValidatorWorker(QThread):
    progress = Signal(int, int, str, str, str)
    heartbeat = Signal(int, int, str, str, int)
    completed = Signal(list, str, bool, int)
    failed = Signal(str)

    def __init__(self, core_console, lisp_source, application_root,
                 bundle_root, pairs, timeout=180):
        super().__init__()
        self.core_console = core_console
        self.lisp_source = lisp_source
        self.application_root = application_root
        self.bundle_root = Path(bundle_root)
        self.pairs = list(pairs)
        self.timeout = int(timeout)
        self._cancel_requested = False
        self._current_identity = ""
        self._current_output = ""
        self._stage = "Preparing validator"
        self._started = time.monotonic()
        self._pair_started = self._started

    def cancel(self):
        self._cancel_requested = True

    def run(self):
        try:
            validator = StableReopenValidator(
                self.core_console,
                self.lisp_source,
                self.application_root,
                timeout=self.timeout,
            )
            previous = self._load_checkpoint()
            results = []
            skipped = 0
            total = len(self.pairs)

            for index, pair in enumerate(self.pairs, 1):
                if self._cancel_requested:
                    break
                identity = str(pair.get("identity", ""))
                self._current_identity = identity
                self._pair_started = time.monotonic()

                reusable = self._reusable(previous.get(identity), pair)
                if reusable:
                    results.append(self._restore_result(reusable))
                    skipped += 1
                    self.progress.emit(
                        index, total, identity, "PAIR",
                        "Skipped: already validated and hashes unchanged",
                    )
                    self._write_checkpoint(results, total, False, skipped)
                    continue

                self._stage = "Reopening A and 00"
                self.progress.emit(
                    index, total, identity, "PAIR",
                    "Reopening and validating local pair",
                )
                result = validator.validate_pair(pair)
                results.append(result)
                self._stage = "Saving validation checkpoint"
                self.progress.emit(
                    index, total, identity, "PAIR",
                    "Validation completed: " + result.status,
                )
                self._write_checkpoint(results, total, False, skipped)

            cancelled = self._cancel_requested and len(results) < total
            report = validator.write_report(self.bundle_root, results)
            self._write_checkpoint(results, total, cancelled, skipped)
            self.completed.emit(results, str(report), cancelled, skipped)
        except Exception:
            self.failed.emit(traceback.format_exc())

    def emit_heartbeat(self):
        total_elapsed = int(time.monotonic() - self._started)
        pair_elapsed = int(time.monotonic() - self._pair_started)
        self.heartbeat.emit(
            0, len(self.pairs), self._current_identity,
            self._stage, max(total_elapsed, pair_elapsed),
        )

    @property
    def checkpoint_path(self):
        return self.bundle_root / "_GNI_REPORTS" / "reopen_validation_checkpoint.json"

    def _load_checkpoint(self):
        path = self.checkpoint_path
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return {
                item.get("identity", ""): item
                for item in payload.get("results", [])
                if item.get("status") == "VALIDATED"
            }
        except Exception:
            return {}

    @staticmethod
    def _current_hashes(pair):
        hashes = {}
        for output in pair.get("outputs", []):
            mode = output.get("output_revision", "")
            hashes[mode] = str(output.get("sha256", ""))
        return hashes

    @classmethod
    def _reusable(cls, record, pair):
        if not record or record.get("status") != "VALIDATED":
            return None
        recorded = {
            item.get("output_revision", ""): item.get("sha256_before", "")
            for item in record.get("outputs", [])
        }
        current = cls._current_hashes(pair)
        if set(recorded) != {"A", "00"} or set(current) != {"A", "00"}:
            return None
        return record if recorded == current else None

    @staticmethod
    def _restore_result(record):
        from revision.reopen_validator import ValidationPair, ValidationOutput
        outputs = [ValidationOutput(**item) for item in record.get("outputs", [])]
        return ValidationPair(
            record.get("identity", ""),
            record.get("drawing_type", ""),
            record.get("status", ""),
            outputs,
            list(record.get("errors", [])),
        )

    def _write_checkpoint(self, results, total, cancelled, skipped):
        path = self.checkpoint_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "GNI-Reopen-Validation-Checkpoint-0.4C.3.2",
            "version": RESPONSIVE_VALIDATOR_VERSION,
            "updated_at": datetime.now().astimezone().isoformat(),
            "total_pairs": total,
            "completed_pairs": len(results),
            "validated_pairs": sum(item.status == "VALIDATED" for item in results),
            "failed_pairs": sum(item.status == "FAILED" for item in results),
            "skipped_already_validated": skipped,
            "cancelled": bool(cancelled),
            "results": [asdict(item) for item in results],
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)
