"""File logging for a windowed (``--windowed``) build.

A windowed build has no console, so every status line and traceback that used to
scroll past in ``run_app.bat`` disappears. This installs a rotating log under
``data_root()/logs/app.log`` and turns an unhandled exception into a dialog that
tells the operator which file to send back.

The log also records the build label and the resolved roots, which is what makes
a support report from a domain PC actionable.
"""

from __future__ import annotations

import logging
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

__all__ = ["LOG_NAME", "install_excepthook", "log_path", "setup"]

LOG_NAME = "app.log"
_LOGGER_NAME = "gni"
_configured = False


def log_path() -> Path:
    from runtime import paths

    return paths.log_dir() / LOG_NAME


def setup(level: int = logging.INFO) -> Path | None:
    """Attach the rotating file handler once; never fatal on startup."""
    global _configured
    target = log_path()
    if _configured:
        return target

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            target, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
    except OSError:
        return None

    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    logger.addHandler(handler)
    logger.propagate = False
    _configured = True
    _record_environment(logger)
    return target


def _record_environment(logger: logging.Logger) -> None:
    from runtime import paths

    logger.info(
        "startup version=%s frozen=%s app_root=%s data_root=%s",
        paths.version(),
        paths.is_frozen(),
        paths.app_root(),
        paths.data_root(),
    )


def install_excepthook() -> None:
    """Log unhandled exceptions and tell the operator where the log lives."""

    def _hook(exc_type, exc_value, exc_tb):
        logger = logging.getLogger(_LOGGER_NAME)
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logger.error("unhandled exception\n%s", text)
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is None:
                return
            detail = f"\n\nDetails were written to:\n{log_path()}"
            QMessageBox.critical(
                None, "Gamma to Nederwiek - unexpected error", str(exc_value) + detail
            )
        except Exception:  # pragma: no cover - the dialog is best effort
            pass

    sys.excepthook = _hook
