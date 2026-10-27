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


if __name__ == "__main__":
    unittest.main()
