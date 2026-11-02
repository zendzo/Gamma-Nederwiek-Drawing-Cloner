"""The discovery TSV is raw tab-joined text, so the reader must not quote-parse.

Measured on the real source: one drawing carried a text value that was a single
double quote. `csv.DictReader` (default QUOTE_MINIMAL) treated it as the start of
a quoted field and swallowed the rest of the file into one value, so the drawing
lost its revision header and blocked as HIGH_CONFIDENCE_SPATIAL_ROW_MISSING.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from titleblock.discovery import TitleBlockDiscoveryEngine

HEADER = ("Drawing\tSpace\tEntity Type\tHandle\tLayer\tTag\tValue\t"
          "Insertion Point\tOwner Handle\tOwner Type\tBlock Name\n")


def _line(handle, value, point="0,0,0"):
    return (f"input.dwg\tMODEL\tTEXT\t{handle}\tText\t\t{value}\t{point}\t"
            "1F\tBLOCK_RECORD\t*Model_Space\n")


class TsvParsingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.engine = TitleBlockDiscoveryEngine.__new__(TitleBlockDiscoveryEngine)
        self.engine.settings = SimpleNamespace(
            project_from="", project_to="", drawing_from="", drawing_to="",
            long_from="", long_to="", short_from="", short_to="",
        )
        self.group = SimpleNamespace(identity="ID-1", drawing_type="GA")
        self.source = self.root / "src.dwg"
        self.source.write_bytes(b"DWG")

    def _parse(self, body):
        tsv = self.root / "title_block_discovery.tsv"
        tsv.write_text(HEADER + body, encoding="utf-8")
        return self.engine._parse(self.group, self.source, tsv)

    def test_a_quote_value_does_not_swallow_following_rows(self):
        fields = self._parse(
            _line("1", "BEFORE")
            + _line("2", '"')
            + _line("3", "Rev.")
            + _line("4", "00", "10,95,0")
        )
        self.assertEqual([field.value for field in fields],
                         ["BEFORE", '"', "Rev.", "00"])
        self.assertEqual([field.handle for field in fields], ["1", "2", "3", "4"])

    def test_quoted_looking_values_keep_their_text(self):
        fields = self._parse(_line("1", '"ISSUED FOR COMMENTS"'))
        self.assertEqual(fields[0].value, '"ISSUED FOR COMMENTS"')

    def test_normal_rows_are_unaffected(self):
        fields = self._parse(_line("1", "APPROVED FOR CONSTRUCTION", "1,2,0"))
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0].value, "APPROVED FOR CONSTRUCTION")
        self.assertEqual(fields[0].insertion_point, "1,2,0")


if __name__ == "__main__":
    unittest.main()
