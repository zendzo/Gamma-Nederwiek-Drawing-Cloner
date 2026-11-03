"""Windows long-path helper for filesystem I/O.

Python 3.12 still inherits the Win32 MAX_PATH limit unless the machine has
``LongPathsEnabled`` set. The General Cloner must read drawings from deep network
shares (the production Gamma tree is far beyond 260 characters), so every file
operation on a user path goes through this helper. Logical paths kept for
reports/UI stay normal; only the I/O boundary is prefixed.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["long_path"]


def long_path(path):
    """Return *path* with the Windows extended-length prefix when needed.

    ``\\\\?\\C:\\...`` is used for local paths and ``\\\\?\\UNC\\server\\...``
    for UNC shares. On non-Windows platforms the path is returned unchanged.
    The function never touches the filesystem, so it is safe for paths that do
    not exist yet.
    """
    if os.name != "nt":
        return Path(path)
    text = str(path)
    if not text:
        return Path(text)
    if text.startswith("\\\\?\\"):
        return Path(text)
    try:
        text = os.path.abspath(text)
    except (OSError, ValueError):
        return Path(text)
    if text.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + text[2:])
    return Path("\\\\?\\" + text)
