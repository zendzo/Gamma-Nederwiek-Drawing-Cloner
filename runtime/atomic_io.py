"""Atomic file replacement that tolerates Windows/network-drive locking.

Core Console stages live on ``%TEMP%\\GNI`` (local), but the pipeline's real
bundles and checkpoints can sit on a network share (``\\\\server\\...``). On
such shares ``os.replace`` raises ``PermissionError`` (WinError 5) when an
antivirus, indexer or a parallel writer briefly holds the destination, and the
discovery checkpoint is written from worker threads, so two writers can also
collide. A short bounded retry turns a fatal ``PermissionError`` into a hiccup.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

__all__ = ["atomic_replace", "atomic_write_text", "atomic_write_json"]


def atomic_replace(source, destination, retries=8, initial_delay=0.05, factor=2.0):
    """``os.replace`` with bounded retry on ``PermissionError``.

    Returns the destination :class:`Path`. Raises the last error on failure.
    """
    source = Path(source)
    destination = Path(destination)
    delay = initial_delay
    last_error = None
    for attempt in range(retries):
        try:
            os.replace(source, destination)
            return destination
        except PermissionError as error:
            last_error = error
            if attempt == retries - 1:
                break
            time.sleep(delay)
            delay = min(delay * factor, 1.0)
    if last_error is not None:
        raise last_error
    return destination


def atomic_write_text(path, text, encoding="utf-8", retries=8):
    """Write *text* to *path* atomically, retrying on a locked destination."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    temporary.write_text(text, encoding=encoding)
    try:
        return atomic_replace(temporary, path, retries=retries)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path, payload, encoding="utf-8", retries=8, **dump_kwargs):
    import json
    text = json.dumps(payload, ensure_ascii=False, **dump_kwargs)
    return atomic_write_text(path, text, encoding=encoding, retries=retries)
