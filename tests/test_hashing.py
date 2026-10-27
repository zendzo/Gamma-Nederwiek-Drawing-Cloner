import hashlib
import tempfile
import unittest
from pathlib import Path

from runtime.hashing import hash_text, sha256_file


class Sha256FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_file(self):
        path = self.root / "empty.bin"
        path.write_bytes(b"")
        self.assertEqual(sha256_file(path), hashlib.sha256(b"").hexdigest())

    def test_matches_reference_digest(self):
        path = self.root / "data.bin"
        payload = b"GNI atomic publish"
        path.write_bytes(payload)
        self.assertEqual(sha256_file(path), hashlib.sha256(payload).hexdigest())

    def test_streams_across_chunk_boundaries(self):
        # Same content, different chunk sizes must give the same digest.
        path = self.root / "big.bin"
        payload = bytes(range(256)) * 4096  # 1 MiB
        path.write_bytes(payload)
        expected = hashlib.sha256(payload).hexdigest()
        for chunk in (1, 7, 4096, 4 * 1024 * 1024):
            with self.subTest(chunk=chunk):
                self.assertEqual(sha256_file(path, chunk=chunk), expected)

    def test_accepts_string_path(self):
        path = self.root / "data.bin"
        path.write_bytes(b"abc")
        self.assertEqual(sha256_file(str(path)), hashlib.sha256(b"abc").hexdigest())

    def test_missing_file_raises_oserror(self):
        with self.assertRaises(OSError):
            sha256_file(self.root / "nope.bin")

    def test_content_change_changes_digest(self):
        path = self.root / "data.bin"
        path.write_bytes(b"one")
        first = sha256_file(path)
        path.write_bytes(b"two")
        self.assertNotEqual(first, sha256_file(path))


class HashTextTests(unittest.TestCase):
    def test_matches_utf8_reference(self):
        self.assertEqual(hash_text("abc"), hashlib.sha256(b"abc").hexdigest())

    def test_non_ascii(self):
        self.assertEqual(
            hash_text("Nederwiek \u2013 \u00e9"), hashlib.sha256("Nederwiek \u2013 \u00e9".encode("utf-8")).hexdigest()
        )


class SharedHasherWiringTests(unittest.TestCase):
    def test_every_engine_uses_the_shared_hasher(self):
        from revision.atomic_publish_dry_run import AtomicPublisherDryRun
        from revision.atomic_publisher import AtomicPublisher
        from revision.pdf_publisher import PdfPublisher
        from revision.reopen_validator import StableReopenValidator
        from revision.stable_external_writer import StableExternalActionWriter
        from runtime.background_controller import PipelineWorker

        pairs = [
            (StableExternalActionWriter._sha256, "stable writer"),
            (StableReopenValidator._sha256, "validator"),
            (AtomicPublisherDryRun._sha256, "dry run"),
            (AtomicPublisher._sha256, "publisher"),
            (PdfPublisher._sha, "pdf publisher"),
            (PipelineWorker._sha, "pipeline worker"),
        ]
        for function, label in pairs:
            with self.subTest(engine=label):
                self.assertIs(function, sha256_file)

    def test_clone_engine_sha256_returns_reference_digest(self):
        from supporting.clone_engine import SupportingCloneEngine

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "f.bin"
            path.write_bytes(b"clone")
            self.assertEqual(
                SupportingCloneEngine.sha256(path), hashlib.sha256(b"clone").hexdigest()
            )


if __name__ == "__main__":
    unittest.main()
