import json
import tempfile
import unittest
from pathlib import Path

from revision.atomic_publisher import AtomicPublishError, AtomicPublisher
from revision.atomic_publisher import PublishFile, PublishTransaction


def _transaction(root, files):
    stage = Path(root) / "stage"
    stage.mkdir(parents=True, exist_ok=True)
    transaction = PublishTransaction(
        "PUB_TEST", "ID-1", "GA", "COMMITTING",
        str(stage), str(Path(root) / "backup"), str(stage / "journal.json"),
    )
    transaction.files = list(files)
    return transaction


class RollbackCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        bundle = self.root / "BUNDLE"
        bundle.mkdir()
        self.publisher = AtomicPublisher(bundle, self.root)
        self.destination_dir = bundle / "00" / "3. GA" / "CAD"
        self.destination_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _file(self, destination, temporary_exists=True):
        temporary = Path(str(destination) + ".gni-publishing-PUB_TEST")
        if temporary_exists:
            temporary.write_bytes(b"in flight")
        return PublishFile(
            output_revision="00",
            source=str(self.root / "src.dwg"),
            destination=str(destination),
            validated_sha256="hash",
            destination_existed=False,
            temporary_path=str(temporary),
        )

    def test_uncommitted_temporary_is_removed(self):
        destination = self.destination_dir / "ID-1_00.dwg"
        item = self._file(destination)
        self.assertTrue(Path(item.temporary_path).exists())
        transaction = _transaction(self.root, [item])
        self.publisher._rollback(transaction)
        self.assertFalse(Path(item.temporary_path).exists())
        self.assertFalse(destination.exists())

    def test_committed_temporary_is_also_removed(self):
        # A committed file normally has no temporary left, but a partial commit
        # could leave one; rollback used to skip cleanup for committed items.
        destination = self.destination_dir / "ID-1_00.dwg"
        destination.write_bytes(b"published")
        item = self._file(destination)
        item.committed = True
        transaction = _transaction(self.root, [item])
        self.publisher._rollback(transaction)
        self.assertFalse(Path(item.temporary_path).exists())

    def test_no_temporary_path_is_not_an_error(self):
        destination = self.destination_dir / "ID-1_00.dwg"
        item = self._file(destination, temporary_exists=False)
        transaction = _transaction(self.root, [item])
        self.publisher._rollback(transaction)
        self.assertEqual(transaction.status, "PUBLISH_FAILED_ROLLED_BACK")

    def test_rollback_status_reflects_success(self):
        destination = self.destination_dir / "ID-1_00.dwg"
        transaction = _transaction(self.root, [self._file(destination)])
        self.publisher._rollback(transaction)
        self.assertEqual(transaction.status, "PUBLISH_FAILED_ROLLED_BACK")


if __name__ == "__main__":
    unittest.main()
