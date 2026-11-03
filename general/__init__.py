"""General Cloner mode.

The Structural Shop Drawing Cloner remains the default, title-block driven
pipeline.  This package adds an independent, file-oriented mode that recursively
clones every DWG under a source folder and applies the configured
find-and-replace mapping, preserving each drawing's relative subfolder.
"""

from general.constants import (
    CLONER_MODES,
    GENERAL_MODE,
    GENERAL_POLICIES,
    STRUCTURAL_MODE,
)

__all__ = [
    "CLONER_MODES",
    "GENERAL_MODE",
    "GENERAL_POLICIES",
    "STRUCTURAL_MODE",
]
