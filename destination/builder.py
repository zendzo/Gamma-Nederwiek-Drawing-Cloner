from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
import json
import os
import shutil
import uuid
from typing import List


@dataclass
class DestinationBuildResult:
    status: str
    bundle_root: str
    created_folders: List[str] = field(default_factory=list)
    existing_folders: List[str] = field(default_factory=list)
    copied_assets: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    manifest_path: str = ""
    message: str = ""


class DestinationBuilder:
    """Build and validate the canonical Gamma/Nederwiek destination tree."""

    FOLDERS = (
        "A/1. SP/CAD",
        "A/1. SP/PDF",
        "A/2. AS/CAD",
        "A/2. AS/PDF",
        "A/3. GA/CAD",
        "A/3. GA/PDF",
        "00/1. SP/CAD",
        "00/1. SP/PDF",
        "00/2. AS/CAD",
        "00/2. AS/PDF",
        "00/3. GA/CAD",
        "00/3. GA/PDF",
        "00/4. PART LIST",
        "00/5. NC1 & DXF",
        "_GNI_REPORTS",
        "_GNI_BACKUP",
    )

    CAD_FOLDERS = tuple(item for item in FOLDERS if item.endswith("/CAD"))
    REQUIRED_ASSETS = ("SEAtrium_Ge_.PNG", "Tennet LogO New.PNG")

    def __init__(self, destination_root, bundle_name, application_root):
        self.destination_root = Path(destination_root).expanduser().resolve()
        self.bundle_name = self._validate_bundle_name(bundle_name)
        self.application_root = Path(application_root).resolve()
        self.bundle_root = self.destination_root / self.bundle_name
        self.asset_root = self.application_root / "resources" / "reference_images"

    RESERVED_NAMES = frozenset(
        {"con", "prn", "aux", "nul"}
        | {"com%s" % index for index in range(1, 10)}
        | {"lpt%s" % index for index in range(1, 10)}
    )

    @classmethod
    def _validate_bundle_name(cls, value):
        value = str(value or "").strip()
        if not value:
            raise ValueError("Destination bundle name is empty")
        if any(char in value for char in '<>:"/\\|?*'):
            raise ValueError("Destination bundle name contains a forbidden Windows character")
        if any(ord(char) < 32 for char in value):
            raise ValueError("Destination bundle name contains a control character")
        if value.endswith(".") or value.endswith(" "):
            raise ValueError("Destination bundle name cannot end with dot or space")
        # "." / ".." would silently resolve to an ancestor of the destination root.
        if value.strip(".") == "":
            raise ValueError("Destination bundle name cannot be a relative path segment")
        # Windows rejects CON, NUL, COM1, and friends even when they carry an
        # extension ("CON.txt"), and the failure only surfaces when the tree is
        # being written - long after the user pressed the button.
        stem = value.split(".")[0].strip().casefold()
        if stem in cls.RESERVED_NAMES:
            raise ValueError(
                "Destination bundle name is a reserved Windows device name: " + value
            )
        return value

    @staticmethod
    def _is_same_or_child(candidate: Path, parent: Path):
        try:
            candidate.relative_to(parent)
            return True
        except ValueError:
            return False

    def validate_source_separation(self, source_root):
        source = Path(source_root).expanduser().resolve()
        if source == self.bundle_root:
            raise ValueError("Source and destination bundle are the same folder")
        if self._is_same_or_child(self.bundle_root, source):
            raise ValueError("Destination bundle cannot be inside the source tree")
        if self._is_same_or_child(source, self.bundle_root):
            raise ValueError("Source folder cannot be inside the destination bundle")

    def _validate_assets(self):
        assets = []
        missing = []
        for name in self.REQUIRED_ASSETS:
            asset = self.asset_root / name
            if asset.is_file() and asset.stat().st_size > 0:
                assets.append(asset)
            else:
                missing.append(name)
        if missing:
            raise FileNotFoundError(
                "Embedded raster assets missing: " + ", ".join(missing)
                + "; expected folder: " + str(self.asset_root)
            )
        return assets

    @staticmethod
    def _files_equal(left: Path, right: Path):
        if not left.is_file() or not right.is_file():
            return False
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as a, right.open("rb") as b:
            while True:
                block_a = a.read(1024 * 1024)
                block_b = b.read(1024 * 1024)
                if block_a != block_b:
                    return False
                if not block_a:
                    return True

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
                raise OSError("Temporary raster-copy size mismatch")
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _test_write_access(self):
        self.destination_root.mkdir(parents=True, exist_ok=True)
        probe = self.destination_root / (".gni-write-test-" + uuid.uuid4().hex)
        try:
            probe.write_text("GNI", encoding="ascii")
            if probe.read_text(encoding="ascii") != "GNI":
                raise OSError("Destination write verification failed")
        finally:
            if probe.exists():
                probe.unlink()

    def _write_manifest(self, result):
        report_folder = self.bundle_root / "_GNI_REPORTS"
        report_folder.mkdir(parents=True, exist_ok=True)
        manifest = report_folder / "destination_manifest.json"
        result.manifest_path = str(manifest)
        payload = {
            "schema": "GNI-Destination-Manifest-1.2",
            "generated_at": datetime.now().astimezone().isoformat(),
            "bundle_root": str(self.bundle_root),
            "required_folders": list(self.FOLDERS),
            "embedded_assets": list(self.REQUIRED_ASSETS),
            "result": asdict(result),
        }
        temporary = manifest.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, manifest)

    def build(self, source_root, copy_assets_to_all_cad=True):
        self.validate_source_separation(source_root)
        assets = self._validate_assets()
        self._test_write_access()

        self.bundle_root.mkdir(parents=True, exist_ok=True)
        result = DestinationBuildResult("PROCESSING", str(self.bundle_root))

        for relative in self.FOLDERS:
            folder = self.bundle_root / Path(relative)
            existed = folder.is_dir()
            folder.mkdir(parents=True, exist_ok=True)
            if not folder.is_dir():
                raise OSError(f"Failed to create destination folder: {folder}")
            if existed:
                result.existing_folders.append(str(folder))
            else:
                result.created_folders.append(str(folder))

        if copy_assets_to_all_cad:
            for relative in self.CAD_FOLDERS:
                cad_folder = self.bundle_root / Path(relative)
                for asset in assets:
                    destination = cad_folder / asset.name
                    if not self._files_equal(asset, destination):
                        self._atomic_copy(asset, destination)
                        result.copied_assets.append(str(destination))

        missing = []
        wrong_type = []
        for relative in self.FOLDERS:
            folder = self.bundle_root / Path(relative)
            if not folder.exists():
                missing.append(relative)
            elif not folder.is_dir():
                wrong_type.append(relative)

        if wrong_type:
            raise OSError(
                "Destination path exists but is not a folder: " + ", ".join(wrong_type)
            )
        if missing:
            raise OSError(
                "Missing destination folders after create attempt: " + ", ".join(missing)
            )

        result.status = "SUCCESS"
        result.message = (
            f"Destination structure ready: {len(self.FOLDERS)} folders; "
            f"{len(result.copied_assets)} raster copies written"
        )
        self._write_manifest(result)
        return result
