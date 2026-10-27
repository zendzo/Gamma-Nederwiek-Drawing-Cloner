import tempfile
import unittest
from pathlib import Path

from models import SourceFile
from scanner.universal import Scanner


def _file(root, name, drawing_type="GA", revision="02", warning=""):
    path = Path(root) / name
    return SourceFile(
        path=str(path), relative_path=name, filename=name, extension="DWG",
        size_bytes=1, modified_iso="2024-01-01T00:00:00+00:00", role="DWG",
        drawing_type=drawing_type, identity="2013J310015", revision=revision,
        warning=warning, selected=True, destination_relative="",
    )


class GroupWarningTests(unittest.TestCase):
    def setUp(self):
        self.scanner = Scanner(".")

    def test_multiple_warnings_are_all_retained(self):
        # A single-field join used to surface only one warning per pair.
        files = [
            _file(".", "2013J310015-GA-001_3.dwg", revision="03",
                  warning="NONSTANDARD_FILENAME_REVISION"),
            _file(".", "2013J310015-GA-001_02.dwg", revision="02",
                  warning="DUPLICATE_REVISION"),
        ]
        group = self.scanner.groups(files)[0]
        self.assertIn("NONSTANDARD_FILENAME_REVISION", group.warning)
        self.assertIn("DUPLICATE_REVISION", group.warning)
        self.assertEqual(group.warning.count(";"), 1)

    def test_drawing_type_conflict_is_flagged(self):
        files = [
            _file(".", "SP-2013J310015-GA-001_02.dwg", drawing_type="SP"),
            _file(".", "AS-2013J310015-GA-001_01.dwg", drawing_type="AS"),
        ]
        group = self.scanner.groups(files)[0]
        self.assertIn("DRAWING_TYPE_CONFLICT:", group.warning)
        self.assertIn("AS/SP", group.warning)

    def test_single_type_pair_has_no_conflict_warning(self):
        files = [
            _file(".", "2013J310015-GA-001_02.dwg"),
            _file(".", "2013J310015-GA-001_A.dwg", revision="A"),
        ]
        group = self.scanner.groups(files)[0]
        self.assertNotIn("DRAWING_TYPE_CONFLICT", group.warning)

    def test_unknown_type_does_not_create_a_conflict(self):
        files = [
            _file(".", "2013J310015-XX-001_02.dwg", drawing_type="UNKNOWN"),
            _file(".", "2013J310015-GA-001_01.dwg", drawing_type="GA"),
        ]
        group = self.scanner.groups(files)[0]
        self.assertEqual(group.drawing_type, "GA")
        self.assertNotIn("DRAWING_TYPE_CONFLICT", group.warning)

    def test_primary_selection_is_unchanged(self):
        files = [
            _file(".", "2013J310015-GA-001_02.dwg", revision="02"),
            _file(".", "2013J310015-GA-001_03.dwg", revision="03"),
            _file(".", "2013J310015-GA-001_A.dwg", revision="A"),
        ]
        group = self.scanner.groups(files)[0]
        self.assertTrue(group.primary_source.endswith("_03.dwg"))
        self.assertEqual(group.revision_a_donor, str(Path(".")/"2013J310015-GA-001_A.dwg"))


if __name__ == "__main__":
    unittest.main()
