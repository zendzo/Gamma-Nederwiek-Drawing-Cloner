import unittest
from datetime import date

from revision.write_plan import RevisionWritePlanBuilder


class PlanHelperTests(unittest.TestCase):
    def test_normalization_collapses_whitespace_and_uppercases(self):
        self.assertEqual(RevisionWritePlanBuilder._norm(" a\t b\n c "), "A B C")

    def test_migration_date_defaults_to_today(self):
        self.assertEqual(RevisionWritePlanBuilder._date(None), date.today())

    def test_migration_date_parses_iso(self):
        self.assertEqual(RevisionWritePlanBuilder._date("2026-10-27"), date(2026, 10, 27))

    def test_header_alias_lookup(self):
        self.assertEqual(
            RevisionWritePlanBuilder._header_field("rev. date"), "REVISION_DATE"
        )
        self.assertIsNone(RevisionWritePlanBuilder._header_field("not a header"))

    def test_cluster_y_groups_nearby_rows(self):
        groups = RevisionWritePlanBuilder._cluster_y(
            [{"_y": 0.0}, {"_y": 0.1}, {"_y": 5.0}]
        )
        self.assertEqual([len(group) for group in groups], [2, 1])


if __name__ == "__main__":
    unittest.main()
