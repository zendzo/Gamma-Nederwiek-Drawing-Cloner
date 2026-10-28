import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from revision.atomic_publish_dry_run import AtomicPublisherDryRun


def _pair(identity, drawing_type="GA"):
    return {
        "identity": identity,
        "drawing_type": drawing_type,
        "writer": {
            "outputs": [
                {"output_revision": mode, "working_dwg": "C:/missing.dwg"}
                for mode in ("A", "00")
            ]
        },
        "validation": {
            "outputs": [
                {"output_revision": mode, "sha256_after": "hash-" + mode}
                for mode in ("A", "00")
            ]
        },
    }


class AssetCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.bundle = root / "BUNDLE"
        self.bundle.mkdir()
        self.app = root / "APP"
        self.app.mkdir()
        self.publisher = AtomicPublisherDryRun(self.bundle, self.app)

    def tearDown(self):
        self.tmp.cleanup()

    def test_assets_are_inspected_once_per_drawing_type(self):
        with mock.patch.object(
            self.publisher, "_inspect_assets_uncached", wraps=self.publisher._inspect_assets_uncached
        ) as uncached:
            for _ in range(5):
                self.publisher._inspect_assets("GA")
            self.assertEqual(uncached.call_count, 1)

    def test_different_types_are_cached_separately(self):
        with mock.patch.object(
            self.publisher, "_inspect_assets_uncached", wraps=self.publisher._inspect_assets_uncached
        ) as uncached:
            self.publisher._inspect_assets("GA")
            self.publisher._inspect_assets("SP")
            self.publisher._inspect_assets("GA")
            self.publisher._inspect_assets("sp")
            self.assertEqual(uncached.call_count, 2)

    def test_inspect_many_reuses_assets_across_pairs(self):
        pairs = [_pair("ID-%d" % index) for index in range(6)]
        with mock.patch.object(
            self.publisher, "_inspect_assets_uncached", wraps=self.publisher._inspect_assets_uncached
        ) as uncached:
            results = self.publisher.inspect_many(pairs)
        # Two reference PNGs hashed once, not once per pair (x6).
        self.assertEqual(uncached.call_count, 1)
        self.assertEqual(len(results), 6)

    def test_cached_assets_keep_their_shape(self):
        assets = self.publisher._inspect_assets("GA")
        self.assertEqual(len(assets), 2)
        for record in assets:
            self.assertIn("destinations", record)
            # Serialisable: write_report does asdict() on the pairs.
            json.dumps(record)

    def test_assets_come_from_the_pairs_own_type_folder(self):
        # A GA pair must hash GA rasters and an SP pair SP rasters; the old
        # shared folder could not tell a type-specific logo from a stale one.
        resources = self.app / "resources" / "reference_images"
        for drawing_type in ("SP", "AS", "GA"):
            folder = resources / drawing_type
            folder.mkdir(parents=True, exist_ok=True)
            for name in ("SEAtrium_Ge_.PNG", "Tennet LogO New.PNG"):
                (folder / name).write_bytes(drawing_type.encode() + b"-" + name.encode())
        for drawing_type in ("SP", "AS", "GA"):
            with self.subTest(type=drawing_type):
                records = self.publisher._inspect_assets(drawing_type.lower())
                self.assertEqual(len(records), 2)
                for record in records:
                    source = Path(record["source"])
                    self.assertEqual(source.parent.name, drawing_type)
                    self.assertTrue(source.is_file())
                    self.assertEqual(
                        [Path(item["path"]).parent.name for item in record["destinations"]],
                        ["CAD", "CAD"],
                    )
                    self.assertEqual(
                        [Path(item["path"]).parent.parent.name for item in record["destinations"]],
                        [self.publisher.TYPE_FOLDERS[drawing_type]] * 2,
                    )

    def test_missing_type_folder_marks_only_that_type_failed(self):
        resources = self.app / "resources" / "reference_images"
        for drawing_type in ("AS", "GA"):
            folder = resources / drawing_type
            folder.mkdir(parents=True, exist_ok=True)
            for name in ("SEAtrium_Ge_.PNG", "Tennet LogO New.PNG"):
                (folder / name).write_bytes(b"x")
        self.assertEqual(
            {record["status"] for record in self.publisher._inspect_assets("GA")},
            {"READY"},
        )
        failed = self.publisher._inspect_assets("SP")
        self.assertEqual({record["status"] for record in failed}, {"FAILED"})
        self.assertEqual(
            {record["error"] for record in failed}, {"REFERENCE_ASSET_NOT_FOUND"}
        )


if __name__ == "__main__":
    unittest.main()
