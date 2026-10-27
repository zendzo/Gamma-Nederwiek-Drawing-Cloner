from __future__ import annotations

import traceback

from PySide6.QtCore import QThread, Signal

from revision.pdf_publisher import PdfPublisher


class PdfPublishWorker(QThread):
    """Qt adapter around PdfPublisher so the publisher core stays importable without Qt."""

    progress = Signal(int, int, str, str)
    completed = Signal(list, str)
    failed = Signal(str)

    def __init__(self, core, bundle, app, workers=4):
        super().__init__()
        self.core = core
        self.bundle = bundle
        self.app = app
        self.workers = max(1, int(workers or 4))
        self._cancel_requested = False

    def cancel(self):
        self._cancel_requested = True

    def run(self):
        try:
            publisher = PdfPublisher(self.core, self.bundle, self.app)
            transactions = publisher.load_published_pairs(self.bundle)
            results = publisher.publish_many(
                transactions,
                lambda a, b, c, d: self.progress.emit(a, b, c, d),
                workers=self.workers,
                should_cancel=lambda: self._cancel_requested,
            )
            report = publisher.write_report(results)
            self.completed.emit(results, str(report))
        except Exception:
            self.failed.emit(traceback.format_exc())
