import unittest

from destination.builder import DestinationBuilder


class BundleNameTests(unittest.TestCase):
    def test_valid_names_are_returned_unchanged(self):
        for name in ("NEDERWIEK_BUNDLE", "Nederwiek Beta", "B-001", "CONSOLE", "LPT10"):
            self.assertEqual(DestinationBuilder._validate_bundle_name(name), name.strip())

    def test_reserved_windows_devices_are_rejected(self):
        for name in ("CON", "con", "NUL", "PRN", "AUX", "COM1", "com9", "LPT1", "lpt9"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    DestinationBuilder._validate_bundle_name(name)

    def test_reserved_names_with_extension_are_rejected(self):
        # Windows rejects CON.txt too; the failure used to appear only when the
        # tree was written, long after the button was pressed.
        with self.assertRaises(ValueError):
            DestinationBuilder._validate_bundle_name("con.txt")

    def test_relative_path_segments_are_rejected(self):
        for name in (".", "..", "..."):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    DestinationBuilder._validate_bundle_name(name)

    def test_control_characters_are_rejected(self):
        with self.assertRaises(ValueError):
            DestinationBuilder._validate_bundle_name("bad\x01name")

    def test_existing_rules_still_apply(self):
        with self.assertRaises(ValueError):
            DestinationBuilder._validate_bundle_name("")
        with self.assertRaises(ValueError):
            DestinationBuilder._validate_bundle_name("bad/name")
        with self.assertRaises(ValueError):
            DestinationBuilder._validate_bundle_name("trailing.")


if __name__ == "__main__":
    unittest.main()
