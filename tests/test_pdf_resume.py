"""PDF stage resume + report truthfulness.

The PDF publisher keeps no checkpoint: `pdf_publish_report.json` doubles as the
resume record via `config_signature` plus per-output hashes. These tests pin the
four cases that decide whether a pair is re-plotted, and that a cancelled sweep
cannot masquerade as a complete run.
"""

import json
import tempfile
import unittest
from pathlib import Path

from revision.pdf_publisher import PdfPair, PdfOutput, PdfPublisher


def _pdf_bytes():
    return b"%PDF-1.4\n1 0 obj\n<< /Type /Page >>\nendobj\n%%EOF\n"


class PdfResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bundle = self.root / "BUNDLE"
        (self.bundle / "_GNI_REPORTS").mkdir(parents=True)
        # The constructor validates the console path up front; _plot is replaced
        # below, so this file is never executed.
        self.core = self.root / "accoreconsole"
        self.core.write_bytes(b"")
        # The publisher resolves lisp/ and revision/pdf_settings.json from the
        # application root, so point that at the repository and keep the bundle
        # and staging folders inside the temp dir.
        self.app = Path(__file__).resolve().parents[1]
        self.publisher = PdfPublisher(self.core, self.bundle, self.app)
        # The publisher resolves its bundle path; Windows temp dirs use 8.3 short
        # names, so the fixture must compare against the same resolved path or
        # the resume check sees a different destination and replots everything.
        self.bundle = self.publisher.bundle
        self.type_folder = self.publisher.TYPE_FOLDERS["GA"]
        self.dwgs = {}
        for revision in ("A", "00"):
            path = self.bundle / revision / self.type_folder / ("ID-1_" + revision + ".dwg")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"DWG-" + revision.encode())
            self.dwgs[revision] = path
        self.plots = []

    def tearDown(self):
        self.tmp.cleanup()

    def _existing_pdfs(self):
        """Pretend a previous run published both PDFs."""
        outputs = {}
        for revision in ("A", "00"):
            dest = self.bundle / revision / self.type_folder / "PDF" / f"ID-1_{revision}.pdf"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(_pdf_bytes())
            outputs[(revision)] = str(dest)
        return outputs

    def _prime(self, signature=None):
        pdfs = self._existing_pdfs()
        self.publisher.previous = {}
        for revision, dest in pdfs.items():
            dwg = self.dwgs[revision]
            self.publisher.previous[("ID-1", revision)] = {
                "source_sha256": self.publisher._sha(dwg),
                "destination_pdf": dest,
                "pdf_sha256": self.publisher._sha(Path(dest)),
                "page_count": 1,
            }
        self.publisher.previous_ok = True
        self.publisher.previous_signature = signature or self.publisher.config_signature
        return pdfs

    def _transaction(self):
        return {
            "identity": "ID-1",
            "drawing_type": "GA",
            "files": [
                {"output_revision": rev, "destination": str(self.dwgs[rev])}
                for rev in ("A", "00")
            ],
        }

    def _plot(self, identity, drawing_type, revision, source, stage, source_sha=None):
        self.plots.append(revision)
        out = Path(stage) / f"{identity}_{revision}.pdf"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(_pdf_bytes())
        dest = self.bundle / revision / self.publisher.TYPE_FOLDERS[drawing_type] / "PDF" / f"{identity}_{revision}.pdf"
        return PdfOutput(revision, str(source), str(dest), "PDF_PLOTTED", self.publisher._sha(out),
                         out.stat().st_size, 1, source_sha256=source_sha), out

    def _run(self):
        self.publisher._plot = self._plot
        return self.publisher._publish_pair(self._transaction())

    def test_unchanged_source_and_signature_replots_nothing(self):
        self._prime()
        pair = self._run()
        self.assertEqual(self.plots, [])
        self.assertEqual(pair.status, "PDF_PAIR_PUBLISHED")
        self.assertTrue(all(o.status == "PDF_UNCHANGED" for o in pair.outputs))

    def test_changed_source_is_replotted(self):
        self._prime()
        self.dwgs["A"].write_bytes(b"DWG-A-EDITED")
        self._run()
        self.assertIn("A", self.plots)

    def test_changed_config_signature_forces_a_full_replot(self):
        self._prime(signature="(DIFFERENT-PLOTTER)")
        self._run()
        self.assertEqual(sorted(self.plots), ["00", "A"])

    def test_missing_pdf_on_disk_is_replotted(self):
        pdfs = self._prime()
        Path(pdfs["00"]).unlink()
        self._run()
        self.assertIn("00", self.plots)


class PdfReportTruthfulnessTests(unittest.TestCase):
    def test_cancelled_report_is_marked_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "pdf_publish_report.json"
            report.write_text(json.dumps({"results": [], "summary": {"pairs": 1}}), encoding="utf-8")
            PdfPublisher.mark_report_incomplete(report, reason="CANCELLED")
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertIs(payload["complete"], False)
            self.assertIs(payload["cancelled"], True)
            self.assertEqual(payload["cancel_reason"], "CANCELLED")
            # The original content survives.
            self.assertEqual(payload["summary"]["pairs"], 1)

    def test_incomplete_marker_tolerates_a_missing_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(PdfPublisher.mark_report_incomplete(Path(tmp) / "nope.json"))

    def test_failed_batch_is_not_marked_complete(self):
        # write_report() used to omit the flag, so a run with failures still
        # read as DONE and the primary button offered "Open Reports".
        with tempfile.TemporaryDirectory() as tmp:
            publisher = PdfPublisher.__new__(PdfPublisher)
            publisher.reports = Path(tmp)
            publisher.config_signature = "sig"
            ok = PdfPair("T1", "ID-OK", "GA", "PDF_PAIR_PUBLISHED")
            bad = PdfPair("T2", "ID-BAD", "GA", "PDF_PUBLISH_FAILED_ROLLED_BACK")

            path = publisher.write_report([ok])
            self.assertIs(json.loads(path.read_text(encoding="utf-8"))["complete"], True)

            path = publisher.write_report([ok, bad])
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIs(payload["complete"], False)
            self.assertEqual(payload["summary"]["failed"], 1)


class PdfFailureVisibilityTests(unittest.TestCase):
    def test_failures_are_written_to_reports(self):
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication([])
        import app as legacy

        saved = {name: getattr(QMessageBox, name)
                 for name in ("information", "warning", "critical", "exec")}
        warnings = []
        for name in ("information", "critical"):
            setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.Ok))
        QMessageBox.warning = staticmethod(lambda *a, **k: warnings.append(a[2]) or QMessageBox.Ok)
        QMessageBox.exec = lambda self, *a, **k: 1
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                main = legacy.Main()
                main.src.setText(str(root))
                main.dst.setText(str(root))
                main.bundle.setText("BUNDLE")

                class Pair:
                    def __init__(self, identity, status):
                        self.identity, self.status, self.errors = identity, status, ["PLOT_FAILED"]
                        self.outputs = []

                results = [Pair(f"ID-{i}", "PDF_PUBLISH_FAILED_ROLLED_BACK") for i in range(7)]
                results.append(Pair("OK-1", "PDF_PAIR_PUBLISHED"))
                report = root / "pdf_publish_report.json"
                report.write_text("{}", encoding="utf-8")
                main._on_pdf_complete(results, report)

                self.assertEqual(len(warnings), 1, "a failed run must not report success")
                self.assertIn("... 2 more", warnings[0])
                payload = json.loads((root / "BUNDLE" / "_GNI_REPORTS" / "failures.json").read_text(encoding="utf-8"))
                self.assertEqual(payload["count"], 7)
                self.assertEqual(payload["failures"][0]["identity"], "ID-0")
                self.assertEqual(payload["failures"][0]["errors"], ["PLOT_FAILED"])
                main.close()
                main.deleteLater()
        finally:
            for name, value in saved.items():
                setattr(QMessageBox, name, value)


if __name__ == "__main__":
    unittest.main()
