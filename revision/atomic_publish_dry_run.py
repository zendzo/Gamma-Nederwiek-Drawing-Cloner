from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
import hashlib
import json
import os
import tempfile
import uuid
from typing import Dict, List

DRY_RUN_VERSION = "0.4C.2A-atomic-publisher-dry-run"


class AtomicPublishDryRunError(RuntimeError):
    pass


@dataclass
class DryRunFile:
    output_revision: str
    source: str
    destination: str
    validated_sha256: str
    actual_sha256: str = ""
    source_size: int = 0
    destination_exists: bool = False
    destination_size: int = 0
    destination_sha256: str = ""
    destination_locked: bool = False
    backup_required: bool = False
    status: str = "PENDING"
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class DryRunPair:
    transaction_id: str
    identity: str
    drawing_type: str
    status: str
    files: List[DryRunFile] = field(default_factory=list)
    assets: List[Dict] = field(default_factory=list)
    backup_root: str = ""
    staging_root: str = ""
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class AtomicPublisherDryRun:
    TYPE_FOLDERS = {"SP": "1. SP", "AS": "2. AS", "GA": "3. GA"}

    def __init__(self, bundle_root, application_root):
        self.bundle_root = Path(bundle_root).resolve()
        self.application_root = Path(application_root).resolve()
        if not self.bundle_root.is_dir():
            raise AtomicPublishDryRunError(
                f"BUNDLE_ROOT_NOT_FOUND:{self.bundle_root}"
            )
        self.reports_root = self.bundle_root / "_GNI_REPORTS"
        self.backup_root = self.bundle_root / "_GNI_BACKUP"
        self.staging_root = self.bundle_root / "_GNI_STAGING"
        self.asset_root = self.application_root / "resources" / "reference_images"

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _inside(path, parent):
        path = Path(path).resolve()
        parent = Path(parent).resolve()
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    @staticmethod
    def _load_json(path, missing_code):
        path = Path(path)
        if not path.is_file():
            raise AtomicPublishDryRunError(f"{missing_code}:{path}")
        return json.loads(path.read_text(encoding="utf-8"))

    @classmethod
    def load_validated_pairs(cls, bundle_root, return_rejected=False):
        """Return pairs ready for a dry run, with rejects reported separately."""
        reports = Path(bundle_root).resolve() / "_GNI_REPORTS"
        writer = cls._load_json(
            reports / "stable_external_writer_report.json",
            "STABLE_WRITER_REPORT_NOT_FOUND",
        )
        validation = cls._load_json(
            reports / "reopen_validation_report.json",
            "REOPEN_VALIDATION_REPORT_NOT_FOUND",
        )

        writer_by_identity = {
            item.get("identity", ""): item for item in writer.get("results", [])
        }
        accepted = []
        rejected = []
        for validated in validation.get("results", []):
            identity = validated.get("identity", "")
            if validated.get("status") != "VALIDATED":
                rejected.append({"identity": identity, "errors": ["VALIDATION_NOT_PASSED"]})
                continue
            written = writer_by_identity.get(identity)
            if not written:
                rejected.append({"identity": identity, "errors": ["WRITER_PAIR_NOT_FOUND"]})
                continue
            if written.get("status") != "LOCAL_PAIR_WRITTEN_NOT_VALIDATED":
                rejected.append({"identity": identity, "errors": ["WRITER_PAIR_STATUS_INVALID"]})
                continue
            validation_outputs = {
                item.get("output_revision"): item
                for item in validated.get("outputs", [])
            }
            writer_outputs = {
                item.get("output_revision"): item
                for item in written.get("outputs", [])
            }
            if set(validation_outputs) != {"A", "00"}:
                rejected.append({"identity": identity, "errors": ["VALIDATION_PAIR_INCOMPLETE"]})
                continue
            if set(writer_outputs) != {"A", "00"}:
                rejected.append({"identity": identity, "errors": ["WRITER_PAIR_INCOMPLETE"]})
                continue
            if any(
                validation_outputs[mode].get("status") != "VALIDATED"
                for mode in ("A", "00")
            ):
                rejected.append({"identity": identity, "errors": ["OUTPUT_VALIDATION_INCOMPLETE"]})
                continue
            accepted.append(
                {
                    "identity": identity,
                    "drawing_type": validated.get(
                        "drawing_type", written.get("drawing_type", "")
                    ),
                    "writer": written,
                    "validation": validated,
                }
            )
        if not accepted:
            detail = ";".join(item["identity"] + "=" + ",".join(item["errors"]) for item in rejected)
            raise AtomicPublishDryRunError(
                "PAIR_GATE_REJECTED:" + detail if rejected else "NO_VALIDATED_PAIRS"
            )
        if return_rejected:
            return accepted, rejected
        return accepted

    def _destination(self, identity, drawing_type, output_revision):
        type_folder = self.TYPE_FOLDERS.get(str(drawing_type).upper())
        if not type_folder:
            raise AtomicPublishDryRunError(
                f"UNKNOWN_DRAWING_TYPE:{drawing_type}"
            )
        revision_root = "A" if output_revision == "A" else "00"
        suffix = "_A.dwg" if output_revision == "A" else "_00.dwg"
        destination = (
            self.bundle_root
            / revision_root
            / type_folder
            / "CAD"
            / (identity + suffix)
        ).resolve()
        expected_parent = (self.bundle_root / revision_root).resolve()
        if not self._inside(destination, expected_parent):
            raise AtomicPublishDryRunError(
                f"DESTINATION_OUTSIDE_BUNDLE:{destination}"
            )
        return destination

    @staticmethod
    def _lock_paths(destination):
        destination = Path(destination)
        return [
            destination.with_suffix(".dwl"),
            destination.with_suffix(".dwl2"),
        ]

    @staticmethod
    def _probe_directory(path):
        path = Path(path)
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / (".gni-dry-run-probe-" + uuid.uuid4().hex + ".tmp")
            with probe.open("xb") as stream:
                stream.write(b"GNI_ATOMIC_PUBLISH_DRY_RUN")
                stream.flush()
                os.fsync(stream.fileno())
            probe.unlink()
            return True, ""
        except Exception as error:
            return False, repr(error)

    def _inspect_file(self, pair, mode, transaction_id):
        writer_outputs = {
            item["output_revision"]: item
            for item in pair["writer"]["outputs"]
        }
        validation_outputs = {
            item["output_revision"]: item
            for item in pair["validation"]["outputs"]
        }
        writer_output = writer_outputs[mode]
        validation_output = validation_outputs[mode]
        source = Path(writer_output["working_dwg"]).resolve()
        destination = self._destination(
            pair["identity"], pair["drawing_type"], mode
        )
        validated_hash = str(validation_output.get("sha256_after", ""))
        item = DryRunFile(
            mode,
            str(source),
            str(destination),
            validated_hash,
        )

        if not source.is_file():
            item.errors.append("VALIDATED_SOURCE_NOT_FOUND")
            item.status = "FAILED"
            return item
        item.source_size = source.stat().st_size
        item.actual_sha256 = self._sha256(source)
        if not validated_hash:
            item.errors.append("VALIDATED_HASH_MISSING")
        elif item.actual_sha256 != validated_hash:
            item.errors.append("VALIDATED_SOURCE_HASH_CHANGED")

        item.destination_exists = destination.is_file()
        item.destination_locked = any(
            lock.exists() for lock in self._lock_paths(destination)
        )
        if item.destination_locked:
            item.errors.append("DESTINATION_DWG_LOCKED")

        if item.destination_exists:
            item.backup_required = True
            item.destination_size = destination.stat().st_size
            item.destination_sha256 = self._sha256(destination)
            if item.destination_sha256 == item.actual_sha256:
                item.warnings.append("DESTINATION_ALREADY_MATCHES_VALIDATED_SOURCE")

        writable, detail = self._probe_directory(destination.parent)
        if not writable:
            item.errors.append("DESTINATION_DIRECTORY_NOT_WRITABLE:" + detail)

        item.status = "READY" if not item.errors else "FAILED"
        return item

    def _inspect_assets(self, drawing_type):
        type_folder = self.TYPE_FOLDERS[drawing_type.upper()]
        results = []
        for name in ("SEAtrium_Ge_.PNG", "Tennet LogO New.PNG"):
            source = self.asset_root / name
            record = {
                "name": name,
                "source": str(source),
                "source_exists": source.is_file(),
                "source_sha256": self._sha256(source) if source.is_file() else "",
                "destinations": [],
                "status": "READY",
            }
            if not source.is_file():
                record["status"] = "FAILED"
                record["error"] = "REFERENCE_ASSET_NOT_FOUND"
            for revision in ("A", "00"):
                destination = (
                    self.bundle_root
                    / revision
                    / type_folder
                    / "CAD"
                    / name
                ).resolve()
                exists = destination.is_file()
                destination_hash = self._sha256(destination) if exists else ""
                record["destinations"].append(
                    {
                        "revision": revision,
                        "path": str(destination),
                        "exists": exists,
                        "sha256": destination_hash,
                        "backup_required": exists
                        and destination_hash != record["source_sha256"],
                        "already_matches": exists
                        and destination_hash == record["source_sha256"],
                    }
                )
            results.append(record)
        return results

    def inspect_pair(self, pair):
        transaction_id = (
            "PUB_"
            + datetime.now().strftime("%Y%m%d_%H%M%S_")
            + pair["drawing_type"]
            + "_"
            + pair["identity"]
            + "_"
            + uuid.uuid4().hex[:8]
        )
        result = DryRunPair(
            transaction_id,
            pair["identity"],
            pair["drawing_type"],
            "CHECKING",
            backup_root=str(self.backup_root / transaction_id),
            staging_root=str(self.staging_root / transaction_id),
        )
        for mode in ("A", "00"):
            result.files.append(self._inspect_file(pair, mode, transaction_id))
        result.assets = self._inspect_assets(pair["drawing_type"])

        hashes = {item.actual_sha256 for item in result.files if item.actual_sha256}
        if len(hashes) != 2:
            result.errors.append("A_AND_00_HASHES_IDENTICAL_OR_MISSING")

        destinations = {item.destination for item in result.files}
        if len(destinations) != 2:
            result.errors.append("A_AND_00_DESTINATIONS_COLLIDE")

        for root in (self.backup_root, self.staging_root, self.reports_root):
            writable, detail = self._probe_directory(root)
            if not writable:
                result.errors.append(
                    f"CONTROL_DIRECTORY_NOT_WRITABLE:{root}:{detail}"
                )

        for item in result.files:
            result.errors.extend(
                item.output_revision + ":" + error for error in item.errors
            )
        for asset in result.assets:
            if asset.get("status") == "FAILED":
                result.errors.append(
                    "ASSET:" + asset["name"] + ":" + asset.get("error", "")
                )

        overwrite_count = sum(item.destination_exists for item in result.files)
        if overwrite_count:
            result.warnings.append(
                f"EXISTING_DWG_FILES_WILL_REQUIRE_BACKUP:{overwrite_count}"
            )
        result.status = (
            "DRY_RUN_READY_TO_PUBLISH"
            if not result.errors
            else "DRY_RUN_BLOCKED"
        )
        return result

    def inspect_many(self, pairs):
        return [self.inspect_pair(pair) for pair in pairs]

    @staticmethod
    def write_report(bundle_root, results):
        reports = Path(bundle_root).resolve() / "_GNI_REPORTS"
        reports.mkdir(parents=True, exist_ok=True)
        path = reports / "atomic_publish_dry_run_report.json"
        payload = {
            "schema": "GNI-Atomic-Publisher-Dry-Run-0.4C.2A",
            "version": DRY_RUN_VERSION,
            "mode": "DRY_RUN",
            "destination_modified": False,
            "generated_at": datetime.now().astimezone().isoformat(),
            "summary": {
                "pairs": len(results),
                "ready": sum(
                    item.status == "DRY_RUN_READY_TO_PUBLISH"
                    for item in results
                ),
                "blocked": sum(
                    item.status == "DRY_RUN_BLOCKED" for item in results
                ),
                "dwg_overwrites": sum(
                    file.destination_exists
                    for item in results
                    for file in item.files
                ),
                "dwg_backups_required": sum(
                    file.backup_required
                    for item in results
                    for file in item.files
                ),
            },
            "results": [asdict(item) for item in results],
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, path)
        return path
