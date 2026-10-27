from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
import hashlib
import json
import os
import shutil
import time
import uuid
from typing import Iterable, List


@dataclass
class SupportingCloneResult:
    source_path: str
    destination_path: str
    role: str
    extension: str
    status: str
    action: str
    collision_policy: str
    source_size: int = 0
    destination_size: int = 0
    source_sha256: str = ""
    destination_sha256: str = ""
    backup_path: str = ""
    elapsed_seconds: float = 0.0
    error: str = ""


class SupportingCloneEngine:
    """Safely clone XLS/XLSX, NC1, and DXF supporting files.

    Source files are never modified. Destination writes use a temporary file,
    flush + fsync, hash verification, then atomic replacement. Existing
    different files are backed up by default before replacement.
    """

    ALLOWED = {
        ".xls": ("PART_LIST", "00/4. PART LIST"),
        ".xlsx": ("PART_LIST", "00/4. PART LIST"),
        ".nc1": ("NC1", "00/5. NC1 & DXF"),
        ".dxf": ("DXF", "00/5. NC1 & DXF"),
    }
    POLICIES = (
        "BACKUP_OVERWRITE",
        "SKIP_DIFFERENT",
        "KEEP_BOTH",
    )

    def __init__(self, bundle_root, policy="BACKUP_OVERWRITE"):
        self.bundle_root = Path(bundle_root).expanduser().resolve()
        self.policy = str(policy).upper()
        if self.policy not in self.POLICIES:
            raise ValueError(f"Unsupported collision policy: {policy}")
        self.report_root = self.bundle_root / "_GNI_REPORTS"
        self.backup_root = self.bundle_root / "_GNI_BACKUP" / "Supporting Files"
        self.report_root.mkdir(parents=True, exist_ok=True)
        self.backup_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _atomic_copy(source: Path, destination: Path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            destination.name + ".gni-copying-" + uuid.uuid4().hex[:8]
        )
        try:
            with source.open("rb", buffering=0) as src, temporary.open(
                "wb", buffering=0
            ) as dst:
                shutil.copyfileobj(src, dst, 4 * 1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            if temporary.stat().st_size != source.stat().st_size:
                raise OSError("Temporary-copy size mismatch")
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _safe_suffix_name(destination: Path) -> Path:
        counter = 1
        while True:
            candidate = destination.with_name(
                f"{destination.stem}_COPY_{counter:02d}{destination.suffix}"
            )
            if not candidate.exists():
                return candidate
            counter += 1

    def _destination_for(self, source: Path):
        extension = source.suffix.lower()
        if extension not in self.ALLOWED:
            raise ValueError(f"Unsupported supporting extension: {source.suffix}")
        role, relative_folder = self.ALLOWED[extension]
        return role, self.bundle_root / relative_folder / source.name

    def _backup_existing(self, destination: Path) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        relative = destination.relative_to(self.bundle_root)
        backup = self.backup_root / stamp / relative
        self._atomic_copy(destination, backup)
        if self.sha256(destination) != self.sha256(backup):
            raise OSError("Backup hash verification failed")
        return backup

    def clone_one(self, source_path) -> SupportingCloneResult:
        started = time.time()
        source = Path(source_path).expanduser().resolve()
        result = SupportingCloneResult(
            str(source), "", "", source.suffix.upper().lstrip("."),
            "PROCESSING", "", self.policy,
        )
        try:
            if not source.is_file():
                raise FileNotFoundError(f"Source file not found: {source}")
            if source.stat().st_size <= 0:
                raise OSError(f"Source file is empty: {source}")

            role, destination = self._destination_for(source)
            result.role = role
            result.destination_path = str(destination)
            result.source_size = source.stat().st_size
            result.source_sha256 = self.sha256(source)

            if destination.is_file():
                current_hash = self.sha256(destination)
                if current_hash == result.source_sha256:
                    result.status = "SUCCESS"
                    result.action = "SKIP_IDENTICAL"
                    result.destination_size = destination.stat().st_size
                    result.destination_sha256 = current_hash
                    return self._finish(result, started)

                if self.policy == "SKIP_DIFFERENT":
                    result.status = "SKIPPED"
                    result.action = "SKIP_DIFFERENT"
                    result.destination_size = destination.stat().st_size
                    result.destination_sha256 = current_hash
                    return self._finish(result, started)

                if self.policy == "KEEP_BOTH":
                    destination = self._safe_suffix_name(destination)
                    result.destination_path = str(destination)
                    result.action = "KEEP_BOTH"
                else:
                    backup = self._backup_existing(destination)
                    result.backup_path = str(backup)
                    result.action = "BACKUP_OVERWRITE"
            else:
                result.action = "COPY_NEW"

            self._atomic_copy(source, destination)
            result.destination_size = destination.stat().st_size
            result.destination_sha256 = self.sha256(destination)
            if result.destination_size != result.source_size:
                raise OSError("Destination size verification failed")
            if result.destination_sha256 != result.source_sha256:
                raise OSError("Destination hash verification failed")
            if destination.suffix.lower() != source.suffix.lower():
                raise OSError("Destination extension changed unexpectedly")

            result.status = "SUCCESS"
            return self._finish(result, started)
        except Exception as exc:
            result.status = "FAILED"
            result.action = result.action or "NONE"
            result.error = repr(exc)
            return self._finish(result, started)

    @staticmethod
    def _finish(result, started):
        result.elapsed_seconds = round(time.time() - started, 3)
        return result

    def clone_many(self, sources: Iterable):
        return [self.clone_one(source) for source in sources]

    def write_report(self, results: List[SupportingCloneResult]):
        report = self.report_root / "supporting_file_clone_report.json"
        payload = {
            "schema": "GNI-Supporting-Clone-1.0",
            "generated_at": datetime.now().astimezone().isoformat(),
            "bundle_root": str(self.bundle_root),
            "collision_policy": self.policy,
            "summary": {
                "total": len(results),
                "success": sum(item.status == "SUCCESS" for item in results),
                "skipped": sum(item.status == "SKIPPED" for item in results),
                "failed": sum(item.status == "FAILED" for item in results),
                "part_list": sum(item.role == "PART_LIST" for item in results),
                "nc1": sum(item.role == "NC1" for item in results),
                "dxf": sum(item.role == "DXF" for item in results),
            },
            "results": [asdict(item) for item in results],
        }
        temporary = report.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, report)
        return report
