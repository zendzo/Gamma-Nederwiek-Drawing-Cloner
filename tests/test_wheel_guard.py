"""Mouse-wheel scrolling must not change Setup values or load an old job."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication

import app as legacy


def _wheel_down(widget):
    position = QPointF(widget.rect().center())
    return QWheelEvent(
        position,
        position,
        QPoint(0, 0),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )


class WheelGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_cfg = legacy.CFG
        legacy.CFG = str(Path(self._tmp.name) / "settings.json")
        self.main = legacy.Main()

    def tearDown(self):
        self.main.close()
        self.main.deleteLater()
        legacy.CFG = self._saved_cfg
        self._tmp.cleanup()

    def test_wheel_over_recent_jobs_preserves_the_current_setup(self):
        old_job = str(Path(self._tmp.name) / "OLD_JOB")
        self.main.s.recent_jobs = [old_job]
        self.main._populate_recent_jobs()
        self.main.load_previous_job = Mock()
        self.main.src.setText("NEW SOURCE")
        self.main.bundle.setText("NEW_BUNDLE")

        event = _wheel_down(self.main.recent)
        QApplication.sendEvent(self.main.recent, event)

        self.assertFalse(event.isAccepted())  # the parent scroll area may use it
        self.assertEqual(self.main.recent.currentIndex(), 0)
        self.main.load_previous_job.assert_not_called()
        self.assertEqual(self.main.src.text(), "NEW SOURCE")
        self.assertEqual(self.main.bundle.text(), "NEW_BUNDLE")

        # An explicit dropdown selection still loads the chosen job.
        self.main.recent.setCurrentIndex(1)
        self.main._recent_selected(1)
        self.main.load_previous_job.assert_called_once_with(old_job)

    def test_wheel_does_not_change_collision_or_worker_values(self):
        controls = (self.main.policy, self.main.workers, self.main.batch)
        before = (self.main.policy.currentIndex(), self.main.workers.value(), self.main.batch.value())

        for control in controls:
            with self.subTest(control=type(control).__name__):
                event = _wheel_down(control)
                QApplication.sendEvent(control, event)
                self.assertFalse(event.isAccepted())

        after = (self.main.policy.currentIndex(), self.main.workers.value(), self.main.batch.value())
        self.assertEqual(after, before)

        # Keyboard, arrows and programmatic changes remain available.
        self.main.policy.setCurrentIndex(1)
        self.main.workers.setValue(7)
        self.main.batch.setValue(6)
        self.assertEqual(self.main.policy.currentIndex(), 1)
        self.assertEqual(self.main.workers.value(), 7)
        self.assertEqual(self.main.batch.value(), 6)


if __name__ == "__main__":
    unittest.main()
