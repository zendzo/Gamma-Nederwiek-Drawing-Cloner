from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
import json
import os
import shutil
import tempfile
import uuid
from typing import Dict, List

from runtime.hashing import sha256_file
from runtime.parallel import run_pool

PUBLISHER_VERSION = "0.4C.2B-atomic-publisher-commit"


class AtomicPublishError(RuntimeError):
    pass


@dataclass
class PublishFile:
    output_revision: str
    source: str
    destination: str
    validated_sha256: str
    status: str = "PENDING"
    source_size: int = 0
    staged_path: str = ""
    temporary_path: str = ""
    backup_path: str = ""
    published_sha256: str = ""
    backup_sha256: str = ""
    destination_existed: bool = False
    committed: bool = False
    rolled_back: bool = False
    errors: List[str] = field(default_factory=list)


@dataclass
class PublishAsset:
    name: str
    source: str
    destination: str
    revision: str
    source_sha256: str
    status: str = "PENDING"
    staged_path: str = ""
    temporary_path: str = ""
    backup_path: str = ""
    destination_existed: bool = False
    committed: bool = False
    rolled_back: bool = False
    errors: List[str] = field(default_factory=list)


@dataclass
class PublishTransaction:
    transaction_id: str
    identity: str
    drawing_type: str
    status: str
    staging_root: str
    backup_root: str
    journal_path: str
    files: List[PublishFile] = field(default_factory=list)
    assets: List[PublishAsset] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    started_at: str = ""
    completed_at: str = ""


class AtomicPublisher:
    TYPE_FOLDERS = {"SP": "1. SP", "AS": "2. AS", "GA": "3. GA"}
    ASSET_NAMES = ("SEAtrium_Ge_.PNG", "Tennet LogO New.PNG")

    def __init__(self, bundle_root, application_root):
        self.bundle_root = Path(bundle_root).resolve()
        self.application_root = Path(application_root).resolve()
        if not self.bundle_root.is_dir():
            raise AtomicPublishError(f"BUNDLE_ROOT_NOT_FOUND:{self.bundle_root}")
        self.reports_root = self.bundle_root / "_GNI_REPORTS"
        self.staging_root = self.bundle_root / "_GNI_STAGING"
        self.backup_root = self.bundle_root / "_GNI_BACKUP"
        self.asset_root = self.application_root / "resources" / "reference_images"
        for path in (self.reports_root, self.staging_root, self.backup_root):
            path.mkdir(parents=True, exist_ok=True)

    # Shared hasher: see runtime/hashing.py.
    _sha256 = staticmethod(sha256_file)

    @staticmethod
    def _inside(path, parent):
        try:
            Path(path).resolve().relative_to(Path(parent).resolve())
            return True
        except ValueError:
            return False

    @staticmethod
    def _atomic_json(path, payload):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    @staticmethod
    def _copy_verified(source, destination, expected_hash=""):
        source = Path(source)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".copying-" + uuid.uuid4().hex[:8])
        with source.open("rb", buffering=0) as src, temporary.open("xb", buffering=0) as dst:
            shutil.copyfileobj(src, dst, 4 * 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        if temporary.stat().st_size != source.stat().st_size:
            temporary.unlink(missing_ok=True)
            raise AtomicPublishError("COPY_SIZE_MISMATCH")
        actual_hash = AtomicPublisher._sha256(temporary)
        wanted = expected_hash or AtomicPublisher._sha256(source)
        if actual_hash != wanted:
            temporary.unlink(missing_ok=True)
            raise AtomicPublishError("COPY_HASH_MISMATCH")
        os.replace(temporary, destination)
        return actual_hash

    @staticmethod
    def _lock_paths(destination):
        destination = Path(destination)
        return (destination.with_suffix(".dwl"), destination.with_suffix(".dwl2"))

    @classmethod
    def load_ready_transactions(cls, bundle_root, return_rejected=False):
        """Return publishable packages, dropping (not aborting on) rejected ones.

        Matches the writer/validator/dry-run gates: one pair that failed its dry
        run no longer blocks publishing every other pair. A rejected package is
        never published — it is only left out and reported.
        """
        reports = Path(bundle_root).resolve() / "_GNI_REPORTS"
        dry_path = reports / "atomic_publish_dry_run_report.json"
        validation_path = reports / "reopen_validation_report.json"
        writer_path = reports / "stable_external_writer_report.json"
        for path, code in (
            (dry_path, "DRY_RUN_REPORT_NOT_FOUND"),
            (validation_path, "VALIDATION_REPORT_NOT_FOUND"),
            (writer_path, "WRITER_REPORT_NOT_FOUND"),
        ):
            if not path.is_file():
                raise AtomicPublishError(f"{code}:{path}")
        dry = json.loads(dry_path.read_text(encoding="utf-8"))
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
        writer = json.loads(writer_path.read_text(encoding="utf-8"))
        if dry.get("destination_modified") is not False or dry.get("mode") != "DRY_RUN":
            raise AtomicPublishError("INVALID_DRY_RUN_REPORT")
        validations = {item.get("identity"): item for item in validation.get("results", [])}
        writers = {item.get("identity"): item for item in writer.get("results", [])}
        accepted = []
        rejected = []
        for item in dry.get("results", []):
            identity = item.get("identity", "")
            errors = []
            if item.get("status") != "DRY_RUN_READY_TO_PUBLISH":
                errors.append("DRY_RUN_NOT_READY")
            valid = validations.get(identity)
            written = writers.get(identity)
            if not valid or valid.get("status") != "VALIDATED":
                errors.append("VALIDATION_NOT_READY")
            if not written or written.get("status") != "LOCAL_PAIR_WRITTEN_NOT_VALIDATED":
                errors.append("WRITER_NOT_READY")
            if errors:
                rejected.append({"identity": identity, "errors": errors})
                continue
            accepted.append({"dry_run": item, "validation": valid, "writer": written})
        if not accepted:
            detail = ";".join(
                entry["identity"] + "=" + ",".join(entry["errors"]) for entry in rejected
            )
            raise AtomicPublishError(
                "PUBLISH_GATE_REJECTED:" + detail if rejected else "NO_READY_TRANSACTIONS"
            )
        if return_rejected:
            return accepted, rejected
        return accepted

    def _journal_payload(self, transaction, event):
        return {
            "schema": "GNI-Atomic-Publisher-Journal-0.4C.2B",
            "version": PUBLISHER_VERSION,
            "event": event,
            "updated_at": datetime.now().astimezone().isoformat(),
            "transaction": asdict(transaction),
        }

    def _journal(self, transaction, event):
        self._atomic_json(transaction.journal_path, self._journal_payload(transaction, event))

    def _preflight_recheck(self, package):
        dry = package["dry_run"]
        validation_outputs = {item["output_revision"]: item for item in package["validation"]["outputs"]}
        files = []
        for dry_file in dry.get("files", []):
            mode = dry_file["output_revision"]
            source = Path(dry_file["source"]).resolve()
            destination = Path(dry_file["destination"]).resolve()
            expected_root = self.bundle_root / ("A" if mode == "A" else "00")
            errors = []
            if not source.is_file():
                errors.append("SOURCE_NOT_FOUND")
            validated_hash = str(validation_outputs[mode].get("sha256_after", ""))
            actual_hash = self._sha256(source) if source.is_file() else ""
            if not validated_hash or actual_hash != validated_hash:
                errors.append("SOURCE_HASH_CHANGED_SINCE_VALIDATION")
            if actual_hash != str(dry_file.get("actual_sha256", "")):
                errors.append("SOURCE_HASH_CHANGED_SINCE_DRY_RUN")
            if not self._inside(destination, expected_root):
                errors.append("DESTINATION_OUTSIDE_EXPECTED_ROOT")
            if any(lock.exists() for lock in self._lock_paths(destination)):
                errors.append("DESTINATION_DWG_LOCKED")
            if destination.exists() != bool(dry_file.get("destination_exists")):
                errors.append("DESTINATION_EXISTENCE_CHANGED_SINCE_DRY_RUN")
            if destination.is_file():
                current_hash = self._sha256(destination)
                if current_hash != str(dry_file.get("destination_sha256", "")):
                    errors.append("DESTINATION_HASH_CHANGED_SINCE_DRY_RUN")
            if errors:
                raise AtomicPublishError(mode + ":" + ",".join(errors))
            files.append(
                PublishFile(
                    mode,
                    str(source),
                    str(destination),
                    validated_hash,
                    source_size=source.stat().st_size,
                    destination_existed=destination.is_file(),
                )
            )
        if {item.output_revision for item in files} != {"A", "00"}:
            raise AtomicPublishError("PAIR_FILES_INCOMPLETE")
        if len({item.validated_sha256 for item in files}) != 2:
            raise AtomicPublishError("A_AND_00_HASHES_IDENTICAL")
        return files

    def _build_assets(self, package):
        dry = package["dry_run"]
        results = []
        for asset_group in dry.get("assets", []):
            source = Path(asset_group["source"]).resolve()
            expected_hash = str(asset_group.get("source_sha256", ""))
            if not source.is_file() or self._sha256(source) != expected_hash:
                raise AtomicPublishError("ASSET_SOURCE_CHANGED:" + asset_group.get("name", ""))
            for target in asset_group.get("destinations", []):
                destination = Path(target["path"]).resolve()
                expected_root = self.bundle_root / target["revision"]
                if not self._inside(destination, expected_root):
                    raise AtomicPublishError("ASSET_DESTINATION_OUTSIDE_BUNDLE")
                exists = destination.is_file()
                if exists != bool(target.get("exists")):
                    raise AtomicPublishError("ASSET_DESTINATION_EXISTENCE_CHANGED")
                if exists and self._sha256(destination) != str(target.get("sha256", "")):
                    raise AtomicPublishError("ASSET_DESTINATION_HASH_CHANGED")
                if target.get("already_matches"):
                    continue
                results.append(
                    PublishAsset(
                        asset_group["name"], str(source), str(destination),
                        target["revision"], expected_hash,
                        destination_existed=exists,
                    )
                )
        return results

    def _backup_item(self, source, backup, expected_hash):
        backup_hash = self._copy_verified(source, backup, expected_hash)
        return backup_hash

    def _stage_transaction(self, transaction):
        transaction.status = "STAGING"
        self._journal(transaction, "staging_started")
        stage_root = Path(transaction.staging_root)
        for item in transaction.files:
            staged = stage_root / item.output_revision / Path(item.destination).name
            item.staged_path = str(staged)
            self._copy_verified(item.source, staged, item.validated_sha256)
            item.status = "STAGED"
            self._journal(transaction, "staged_" + item.output_revision)
        for asset in transaction.assets:
            staged = stage_root / "assets" / asset.revision / asset.name
            asset.staged_path = str(staged)
            self._copy_verified(asset.source, staged, asset.source_sha256)
            asset.status = "STAGED"
            self._journal(transaction, "asset_staged_" + asset.revision + "_" + asset.name)

    def _backup_transaction(self, transaction):
        transaction.status = "BACKING_UP"
        self._journal(transaction, "backup_started")
        backup_root = Path(transaction.backup_root)
        for item in transaction.files:
            destination = Path(item.destination)
            if destination.is_file():
                backup = backup_root / item.output_revision / destination.name
                item.backup_path = str(backup)
                item.backup_sha256 = self._backup_item(destination, backup, self._sha256(destination))
                item.status = "BACKED_UP"
                self._journal(transaction, "backed_up_" + item.output_revision)
        for asset in transaction.assets:
            destination = Path(asset.destination)
            if destination.is_file():
                backup = backup_root / "assets" / asset.revision / destination.name
                asset.backup_path = str(backup)
                self._backup_item(destination, backup, self._sha256(destination))
                asset.status = "BACKED_UP"
                self._journal(transaction, "asset_backed_up_" + asset.revision + "_" + asset.name)

    def _prepare_temporaries(self, transaction):
        transaction.status = "PREPARING_TEMPORARIES"
        self._journal(transaction, "temporary_prepare_started")
        for item in transaction.files:
            destination = Path(item.destination)
            temporary = destination.with_name(destination.name + ".gni-publishing-" + transaction.transaction_id)
            item.temporary_path = str(temporary)
            self._copy_verified(item.staged_path, temporary, item.validated_sha256)
            item.status = "TEMPORARY_READY"
            self._journal(transaction, "temporary_ready_" + item.output_revision)
        for asset in transaction.assets:
            destination = Path(asset.destination)
            temporary = destination.with_name(destination.name + ".gni-publishing-" + transaction.transaction_id)
            asset.temporary_path = str(temporary)
            self._copy_verified(asset.staged_path, temporary, asset.source_sha256)
            asset.status = "TEMPORARY_READY"
            self._journal(transaction, "asset_temporary_ready_" + asset.revision + "_" + asset.name)

    def _commit_path(self, temporary, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, destination)

    def _rollback(self, transaction):
        errors = []
        for asset in reversed(transaction.assets):
            # Drop the in-destination temporary whether or not it was committed:
            # after a successful os.replace the temporary is gone, but a failed
            # commit leaves a *.gni-publishing-* file next to the real drawing.
            if asset.temporary_path:
                Path(asset.temporary_path).unlink(missing_ok=True)
            if not asset.committed:
                continue
            try:
                destination = Path(asset.destination)
                if asset.destination_existed and asset.backup_path:
                    self._copy_verified(asset.backup_path, destination)
                else:
                    destination.unlink(missing_ok=True)
                asset.rolled_back = True
                asset.status = "ROLLED_BACK"
            except Exception as error:
                errors.append("ASSET:" + asset.name + ":" + repr(error))
        for item in reversed(transaction.files):
            if item.temporary_path:
                Path(item.temporary_path).unlink(missing_ok=True)
            if not item.committed:
                continue
            try:
                destination = Path(item.destination)
                if item.destination_existed and item.backup_path:
                    self._copy_verified(item.backup_path, destination, item.backup_sha256)
                else:
                    destination.unlink(missing_ok=True)
                item.rolled_back = True
                item.status = "ROLLED_BACK"
            except Exception as error:
                errors.append(item.output_revision + ":" + repr(error))
        transaction.status = "CRITICAL_ROLLBACK_FAILED" if errors else "PUBLISH_FAILED_ROLLED_BACK"
        transaction.errors.extend(errors)
        self._journal(transaction, "rollback_completed" if not errors else "rollback_failed")

    def _commit_transaction(self, transaction):
        transaction.status = "COMMITTING"
        self._journal(transaction, "commit_started")
        try:
            by_mode = {item.output_revision: item for item in transaction.files}
            for mode in ("A", "00"):
                item = by_mode[mode]
                self._commit_path(item.temporary_path, item.destination)
                item.committed = True
                item.status = "COMMITTED"
                item.published_sha256 = self._sha256(item.destination)
                if item.published_sha256 != item.validated_sha256:
                    raise AtomicPublishError("FINAL_HASH_MISMATCH:" + mode)
                item.status = "HASH_VERIFIED"
                self._journal(transaction, mode + "_committed")
            for asset in transaction.assets:
                self._commit_path(asset.temporary_path, asset.destination)
                asset.committed = True
                if self._sha256(asset.destination) != asset.source_sha256:
                    raise AtomicPublishError("ASSET_FINAL_HASH_MISMATCH:" + asset.name)
                asset.status = "COMMITTED"
                self._journal(transaction, "asset_committed_" + asset.revision + "_" + asset.name)
            transaction.status = "PUBLISHED"
            transaction.completed_at = datetime.now().astimezone().isoformat()
            self._journal(transaction, "transaction_committed")
        except Exception as error:
            transaction.errors.append(repr(error))
            self._journal(transaction, "commit_failed")
            self._rollback(transaction)

    def publish_package(self, package):
        dry = package["dry_run"]
        transaction_id = (
            "PUB_" + datetime.now().strftime("%Y%m%d_%H%M%S_")
            + dry["drawing_type"] + "_" + dry["identity"] + "_" + uuid.uuid4().hex[:8]
        )
        stage = self.staging_root / transaction_id
        backup = self.backup_root / transaction_id
        journal = stage / "journal.json"
        transaction = PublishTransaction(
            transaction_id, dry["identity"], dry["drawing_type"], "CREATED",
            str(stage), str(backup), str(journal),
            started_at=datetime.now().astimezone().isoformat(),
        )
        try:
            transaction.files = self._preflight_recheck(package)
            transaction.assets = self._build_assets(package)
            stage.mkdir(parents=True, exist_ok=False)
            self._journal(transaction, "transaction_created")
            self._stage_transaction(transaction)
            self._backup_transaction(transaction)
            self._prepare_temporaries(transaction)
            self._commit_transaction(transaction)
        except Exception as error:
            transaction.status = "PUBLISH_FAILED_DESTINATION_UNCHANGED"
            transaction.errors.append(repr(error))
            stage.mkdir(parents=True, exist_ok=True)
            self._journal(transaction, "precommit_failed")
        return transaction

    def publish_many(self, packages, workers=1, on_result=None, should_cancel=None):
        """Publish packages, optionally in parallel, returning them in input order.

        Same contract as ``inspect_many``: ``on_result`` fires in completion order
        and the returned list is re-sorted. Each package is an independent
        transaction (own staging, backup and journal), so parallel commits do not
        interleave.
        """
        return run_pool(
            self.publish_package, packages, workers, on_result, should_cancel
        )

    @staticmethod
    def write_report(bundle_root, transactions):
        reports = Path(bundle_root).resolve() / "_GNI_REPORTS"
        reports.mkdir(parents=True, exist_ok=True)
        path = reports / "atomic_publish_report.json"
        payload = {
            "schema": "GNI-Atomic-Publisher-0.4C.2B",
            "version": PUBLISHER_VERSION,
            "generated_at": datetime.now().astimezone().isoformat(),
            "summary": {
                "transactions": len(transactions),
                "published": sum(item.status == "PUBLISHED" for item in transactions),
                "rolled_back": sum(item.status == "PUBLISH_FAILED_ROLLED_BACK" for item in transactions),
                "failed_unchanged": sum(item.status == "PUBLISH_FAILED_DESTINATION_UNCHANGED" for item in transactions),
                "critical": sum(item.status == "CRITICAL_ROLLBACK_FAILED" for item in transactions),
            },
            "transactions": [asdict(item) for item in transactions],
        }
        temporary = path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return path
