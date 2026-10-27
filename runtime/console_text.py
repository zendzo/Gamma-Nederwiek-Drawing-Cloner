"""Single implementation of the AutoCAD Core Console text decoder.

Every engine used to carry its own copy of this function. They drifted on one
detail that matters: Core Console writes UTF-16LE *without* a BOM, and the
strict ``utf-16`` codec refuses (or mis-decodes) such input, so the marker
strings the drivers grep for could silently fail to match.

Keep every engine aliased to this function rather than re-implementing it.
"""

from __future__ import annotations

__all__ = ["decode_console"]


def decode_console(value):
    """Decode Core Console stdout/stderr into text. Never raises.

    ``utf-16le`` is used explicitly: the console emits little-endian UTF-16 with
    no byte-order mark on Windows, and ``utf-16`` requires a BOM to pick an
    endianness. The interleave check below identifies that case before the
    UTF-8/cp1252 fallbacks, which would otherwise turn \"G\\x00N\\x00I\\x00\" into
    a string full of NULs and never match a marker.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    data = bytes(value)
    if not data:
        return ""
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16le", errors="replace")
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16be", errors="replace")
    # ASCII text in UTF-16LE has a NUL in every odd byte position.
    if len(data) > 4 and data[1:2] == b"\x00":
        return data.decode("utf-16le", errors="replace")
    for encoding in ("utf-8", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")
