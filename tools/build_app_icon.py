"""Build ``resources/app_icon.ico`` from the supplied artwork.

The source PNG is a flat image with a white matte around the rounded-square
logo. Windows title bars and taskbars need transparency, so this script removes
the connected white border, crops to the logo, and writes a multi-resolution
ICO (PNG-compressed entries, 16-256 px). The same 256 px tile is saved as
``resources/app_icon.png`` for non-Windows use.

Run it with the project venv after replacing the artwork:

    .venv\\Scripts\\python.exe tools\\build_app_icon.py
"""

from __future__ import annotations

import struct
from collections import deque
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QRect, Qt
from PySide6.QtGui import QImage

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "resources" / "app_icon_source.png"
ICON = ROOT / "resources" / "app_icon.ico"
PREVIEW = ROOT / "resources" / "app_icon.png"

# Windows asks for these sizes across 100-400 % display scaling. 256 is the
# large-icon ceiling; 16/20/24 are what the title bar actually uses most.
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
# Everything at or above this channel value connected to the border is matte.
WHITE = 238


def _remove_matte(image: QImage) -> QImage:
    """Flood-fill the near-white border to transparent.

    Only the connected border is cleared, so the white drawing sheet inside the
    logo keeps its alpha. Pixels are read through ``Format_RGBA8888`` so the
    channel order is explicit and platform independent.
    """
    image = image.convertToFormat(QImage.Format_RGBA8888)
    width, height = image.width(), image.height()
    stride = image.bytesPerLine()
    view = memoryview(image.bits())

    def offset(x: int, y: int) -> int:
        return y * stride + x * 4

    def is_matte(x: int, y: int) -> bool:
        o = offset(x, y)
        return view[o] >= WHITE and view[o + 1] >= WHITE and view[o + 2] >= WHITE

    seen = bytearray(width * height)
    pending: deque[tuple[int, int]] = deque()
    for x in range(width):
        for y in (0, height - 1):
            if not seen[y * width + x] and is_matte(x, y):
                seen[y * width + x] = 1
                pending.append((x, y))
    for y in range(height):
        for x in (0, width - 1):
            if not seen[y * width + x] and is_matte(x, y):
                seen[y * width + x] = 1
                pending.append((x, y))

    while pending:
        x, y = pending.popleft()
        o = offset(x, y)
        view[o] = view[o + 1] = view[o + 2] = view[o + 3] = 0
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < width and 0 <= ny < height:
                index = ny * width + nx
                if not seen[index] and is_matte(nx, ny):
                    seen[index] = 1
                    pending.append((nx, ny))
    return image


def _cropped(image: QImage) -> QImage:
    """Crop to the opaque logo, keeping it square and centred."""
    width, height = image.width(), image.height()
    stride = image.bytesPerLine()
    view = memoryview(image.bits())
    min_x, min_y, max_x, max_y = width, height, -1, -1
    for y in range(height):
        row = y * stride
        for x in range(width):
            if view[row + x * 4 + 3] > 8:
                min_x, max_x = min(min_x, x), max(max_x, x)
                min_y, max_y = min(min_y, y), max(max_y, y)
    if max_x < 0:
        raise SystemExit("app_icon_source.png has no opaque pixels")
    side = max(max_x - min_x + 1, max_y - min_y + 1)
    center_x = (min_x + max_x) // 2
    center_y = (min_y + max_y) // 2
    left = max(0, min(center_x - side // 2, width - side))
    top = max(0, min(center_y - side // 2, height - side))
    return image.copy(QRect(left, top, side, side))


def _png(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise SystemExit("could not encode a PNG tile")
    return bytes(buffer.data())


def _write_ico(tiles: list[tuple[int, bytes]]) -> None:
    """Write an ICO whose entries are PNG streams (Windows Vista and newer)."""
    header = struct.pack("<HHH", 0, 1, len(tiles))
    offset = 6 + 16 * len(tiles)
    entries = []
    for size, data in tiles:
        dimension = 0 if size >= 256 else size
        entries.append(
            struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(data), offset)
        )
        offset += len(data)
    ICON.write_bytes(header + b"".join(entries) + b"".join(data for _, data in tiles))


def main() -> None:
    source = QImage(str(SOURCE))
    if source.isNull():
        raise SystemExit(f"could not read {SOURCE}")
    cutout = _cropped(_remove_matte(source))
    tiles = [
        (size, _png(cutout.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
        for size in SIZES
    ]
    _write_ico(tiles)
    preview = cutout.scaled(256, 256, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    if not preview.save(str(PREVIEW), "PNG"):
        raise SystemExit(f"could not write {PREVIEW}")
    print(f"{ICON} ({ICON.stat().st_size} bytes), sizes: {', '.join(map(str, SIZES))}")
    print(PREVIEW)


if __name__ == "__main__":
    main()
