import json
import tempfile
import unittest
from pathlib import Path

from config.settings import Settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "nested" / "settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_returns_defaults(self):
        self.assertEqual(Settings.load(self.path).destination_bundle_name, "Nederwiek_Bundle")

    def test_round_trip_persists_contractor_mapping(self):
        saved = Settings(source_folder="C:/src", contractor_doc_from="X1", contractor_doc_to="Y2", workers=6)
        saved.save(self.path)
        loaded = Settings.load(self.path)
        self.assertEqual(loaded.source_folder, "C:/src")
        self.assertEqual(loaded.contractor_doc_from, "X1")
        self.assertEqual(loaded.contractor_doc_to, "Y2")
        self.assertEqual(loaded.workers, 6)

    def test_workers_defaults_to_four_and_survives_bad_values(self):
        self.assertEqual(Settings.load(self.path).workers, 4)
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"workers": "many"}), encoding="utf-8")
        self.assertEqual(Settings.load(self.path).workers, 4)

    def test_unknown_keys_are_ignored_instead_of_wiping_settings(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps({"source_folder": "C:/src", "legacy_key": 1}), encoding="utf-8"
        )
        self.assertEqual(Settings.load(self.path).source_folder, "C:/src")

    def test_invalid_json_returns_defaults(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(Settings.load(self.path).source_folder, "")

    def test_non_object_json_returns_defaults(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(Settings.load(self.path).source_folder, "")


if __name__ == "__main__":
    unittest.main()
