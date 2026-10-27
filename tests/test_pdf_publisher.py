import tempfile
import unittest
from pathlib import Path

from revision.pdf_publisher import PdfPublisher


class PdfValidationTests(unittest.TestCase):
    def _pdf(self, tmp, body):
        path = Path(tmp) / "test.pdf"
        path.write_bytes(body)
        return path

    def test_rejects_non_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            valid, pages = PdfPublisher._pdf_valid(self._pdf(tmp, b"not a pdf"), 1)
            self.assertEqual((valid, pages), (False, 0))

    def test_accepts_single_page_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = b"%PDF-1.4\n1 0 obj\n<< /Type /Page >>\nendobj\n%%EOF\n"
            self.assertEqual(PdfPublisher._pdf_valid(self._pdf(tmp, body), 1), (True, 1))

    def test_rejects_multipage_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = (
                b"%PDF-1.4\n1 0 obj\n<< /Type /Page >>\nendobj\n"
                b"2 0 obj\n<< /Type /Page >>\nendobj\n%%EOF\n"
            )
            valid, pages = PdfPublisher._pdf_valid(self._pdf(tmp, body), 1)
            self.assertEqual((valid, pages), (False, 2))

    def test_failure_reason_reads_plot_result_tsv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.tsv"
            path.write_text(
                "Status\tReason\tPlotter\tMedia\nFAILED\tMEDIA_NOT_AVAILABLE\t\t\n",
                encoding="utf-8",
            )
            self.assertEqual(PdfPublisher._failure_reason(path), "MEDIA_NOT_AVAILABLE")

    def test_failure_reason_missing_or_passing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(PdfPublisher._failure_reason(Path(tmp) / "missing.tsv"), "")
            path = Path(tmp) / "pass.tsv"
            path.write_text("Status\tReason\nPASS\tOK\n", encoding="utf-8")
            self.assertEqual(PdfPublisher._failure_reason(path), "")


if __name__ == "__main__":
    unittest.main()
