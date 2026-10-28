import tempfile
import unittest
from pathlib import Path

from destination.builder import DestinationBuilder


class BundleNameTests(unittest.TestCase):
    def test_rejects_empty_and_windows_path_characters(self):
        for value in ("", "   ", "a/b", "a\\b", "a:b", "name."):
            with self.assertRaises(ValueError):
                DestinationBuilder._validate_bundle_name(value)

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(DestinationBuilder._validate_bundle_name(" TEST-1 "), "TEST-1")


class SourceSeparationTests(unittest.TestCase):
    def test_source_equal_to_bundle_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder = DestinationBuilder(tmp, "BUNDLE", tmp)
            with self.assertRaises(ValueError):
                builder.validate_source_separation(builder.bundle_root)

    def test_source_inside_bundle_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder = DestinationBuilder(tmp, "BUNDLE", tmp)
            with self.assertRaises(ValueError):
                builder.validate_source_separation(Path(builder.bundle_root) / "inner")

    def test_bundle_inside_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder = DestinationBuilder(tmp, "BUNDLE", tmp)
            with self.assertRaises(ValueError):
                builder.validate_source_separation(tmp)

    def test_separate_source_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            builder = DestinationBuilder(Path(tmp) / "destination", "BUNDLE", tmp)
            builder.validate_source_separation(source)


class AtomicCopyTests(unittest.TestCase):
    def test_atomic_copy_preserves_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.bin"
            destination = Path(tmp) / "sub" / "destination.bin"
            source.write_bytes(b"x" * 10000)
            DestinationBuilder._atomic_copy(source, destination)
            self.assertEqual(destination.read_bytes(), source.read_bytes())

    def test_files_equal_detects_differences(self):
        with tempfile.TemporaryDirectory() as tmp:
            left, right = Path(tmp) / "l.bin", Path(tmp) / "r.bin"
            left.write_bytes(b"abc")
            right.write_bytes(b"abd")
            self.assertFalse(DestinationBuilder._files_equal(left, right))
            right.write_bytes(b"abc")
            self.assertTrue(DestinationBuilder._files_equal(left, right))


class PerTypeReferenceTests(unittest.TestCase):
    """SP/AS/GA each get their rasters from their own source folder.

    Before 0.4C.14 one flat folder fed all six CAD folders, so a type-specific
    logo was impossible. These tests pin the routing and the failure message.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = self.root / "APP"
        self.assets = self.app / "resources" / "reference_images"
        self.source = self.root / "source"
        self.source.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_type_assets(self, drawing_type, seatrium=b"S", tennet=b"T"):
        folder = self.assets / drawing_type
        folder.mkdir(parents=True, exist_ok=True)
        (folder / DestinationBuilder.REQUIRED_ASSETS[0]).write_bytes(seatrium)
        (folder / DestinationBuilder.REQUIRED_ASSETS[1]).write_bytes(tennet)

    def _builder(self):
        return DestinationBuilder(self.root / "destination", "BUNDLE", self.app)

    def test_reference_folder_is_per_type(self):
        builder = self._builder()
        for drawing_type in ("SP", "AS", "GA"):
            with self.subTest(type=drawing_type):
                folder = builder.reference_dir(drawing_type)
                self.assertEqual(folder.parent, builder.asset_root)
                self.assertEqual(folder.name, drawing_type)
                self.assertEqual(folder, builder.reference_dir(drawing_type.lower()))
                self.assertEqual(
                    [asset.name for asset in builder.reference_assets(drawing_type)],
                    list(DestinationBuilder.REQUIRED_ASSETS),
                )

    def test_unknown_type_is_rejected(self):
        with self.assertRaises(ValueError):
            self._builder().reference_dir("XX")

    def test_each_cad_folder_receives_only_its_own_type(self):
        for drawing_type in ("SP", "AS", "GA"):
            self._write_type_assets(drawing_type, b"seatrium-" + drawing_type.encode(),
                                    b"tennet-" + drawing_type.encode())
        builder = self._builder()
        builder.build(self.source)

        expectations = {"1. SP": b"SP", "2. AS": b"AS", "3. GA": b"GA"}
        for revision in ("A", "00"):
            for type_folder, tag in expectations.items():
                cad = builder.bundle_root / revision / type_folder / "CAD"
                with self.subTest(revision=revision, type=type_folder):
                    self.assertEqual(
                        (cad / "SEAtrium_Ge_.PNG").read_bytes(), b"seatrium-" + tag
                    )
                    self.assertEqual(
                        (cad / "Tennet LogO New.PNG").read_bytes(), b"tennet-" + tag
                    )

    def test_missing_type_folder_names_the_gap(self):
        self._write_type_assets("SP")
        self._write_type_assets("AS")
        builder = self._builder()
        with self.assertRaises(FileNotFoundError) as raised:
            builder.build(self.source)
        message = str(raised.exception)
        self.assertIn("GA: SEAtrium_Ge_.PNG", message)
        self.assertIn("GA: Tennet LogO New.PNG", message)
        self.assertNotIn("SP:", message)

    def test_manifest_records_per_type_sources(self):
        for drawing_type in ("SP", "AS", "GA"):
            self._write_type_assets(drawing_type)
        builder = self._builder()
        builder.build(self.source)
        import json
        manifest = json.loads(
            (builder.bundle_root / "_GNI_REPORTS" / "destination_manifest.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["schema"], "GNI-Destination-Manifest-1.3")
        sections = manifest["reference_assets"]
        self.assertEqual(set(sections), {"SP", "AS", "GA"})
        self.assertEqual(
            sections["GA"]["source_folder"],
            "reference_images\\GA" if __import__("os").name == "nt" else "reference_images/GA",
        )
        self.assertEqual(
            sections["GA"]["cad_folders"], ["A/3. GA/CAD", "00/3. GA/CAD"]
        )
        self.assertNotIn("embedded_assets", manifest)

    def test_rebuild_only_rewrites_changed_type(self):
        for drawing_type in ("SP", "AS", "GA"):
            self._write_type_assets(drawing_type)
        builder = self._builder()
        builder.build(self.source)
        self._write_type_assets("SP", seatrium=b"S-NEW")
        result = builder.build(self.source)
        copied = [Path(item).parts[-3:] for item in result.copied_assets]
        self.assertEqual(
            sorted(copied), [("1. SP", "CAD", "SEAtrium_Ge_.PNG")] * 2
        )


if __name__ == "__main__":
    unittest.main()
