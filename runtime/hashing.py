"""Single implementation of file/text hashing.

Seven modules each carried a byte-identical streaming sha256 helper for the
checkpoint and publish guards. The guards are only as trustworthy as the
implementation being the same everywhere, so keep one copy.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

__all__ = ["hash_text", "sha256_file"]

CHUNK_SIZE = 4 * 1024 * 1024


def sha256_file(path, chunk=CHUNK_SIZE):
    """Return the sha256 hex digest of *path*, streamed so large DWGs fit."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def hash_text(text):
    """Return the sha256 hex digest of *text* encoded as UTF-8."""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()
