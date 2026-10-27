import unittest

from models import SourceFile
from scanner.universal import Scanner


class ScannerParseTests(unittest.TestCase):
    def test_revision_and_identity(self):
        self.assertEqual(Scanner.parse("SP-1001_A"), ("SP-1001", "A", ""))
        self.assertEqual(Scanner.parse("AS-2002_12"), ("AS-2002", "12", ""))

    def test_single_digit_revision_is_zero_padded_and_flagged(self):
        self.assertEqual(
            Scanner.parse("GA-1-GA-2_3"),
            ("GA-1-GA-2", "03", "NONSTANDARD_FILENAME_REVISION"),
        )

    def test_missing_revision_suffix(self):
        self.assertEqual(Scanner.parse("SP-1001"), ("SP-1001", "", ""))

    def test_drawing_type_detection(self):
        self.assertEqual(Scanner.dtype("SP-1.dwg"), "SP")
        self.assertEqual(Scanner.dtype("AS-1.dwg"), "AS")
        self.assertEqual(Scanner.dtype("x-GA-y.dwg"), "GA")
        self.assertEqual(Scanner.dtype("other.dwg"), "UNKNOWN")


class ScannerGroupTests(unittest.TestCase):
    @staticmethod
    def _file(name, revision, modified):
        return SourceFile(
            path=name,
            relative_path=name,
            filename=name,
            extension="DWG",
            size_bytes=1,
            modified_iso=modified,
            role="DWG",
            drawing_type="SP",
            identity="SP-1",
            revision=revision,
        )

    def test_primary_prefers_highest_numeric_revision_not_newest_file(self):
        files = [
            self._file("a_02.dwg", "02", "2026-01-01T00:00:00+00:00"),
            self._file("a_10.dwg", "10", "2025-01-01T00:00:00+00:00"),
            self._file("a_A.dwg", "A", "2024-01-01T00:00:00+00:00"),
        ]
        group = Scanner.groups(None, files)[0]
        self.assertTrue(group.primary_source.endswith("a_10.dwg"))
        self.assertTrue(group.revision_a_donor.endswith("a_A.dwg"))

    def test_supporting_destinations(self):
        files = [
            SourceFile("p.xlsx", "p.xlsx", "p.xlsx", "XLSX", 1, "", role="PART_LIST"),
            SourceFile("n.nc1", "n.nc1", "n.nc1", "NC1", 1, "", role="NC1"),
        ]
        self.assertEqual(
            [item.destination_relative for item in Scanner.supporting(files)],
            ["4. PART LIST/p.xlsx", "5. NC1 & DXF/n.nc1"],
        )


if __name__ == "__main__":
    unittest.main()
