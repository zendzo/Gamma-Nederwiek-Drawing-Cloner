"""Mode names shared by settings, the UI, and the General Cloner engine."""

from __future__ import annotations

STRUCTURAL_MODE = "STRUCTURAL_SHOP_DRAWING_CLONER"
GENERAL_MODE = "GENERAL_CLONER"

CLONER_MODES = (
    (STRUCTURAL_MODE, "Structural Shop Drawing Cloner"),
    (GENERAL_MODE, "General Cloner"),
)

# The same three policies the supporting-file clone already exposes.  Reusing
# the names keeps the meaning of an existing setting obvious to the operator.
GENERAL_POLICIES = ("BACKUP_OVERWRITE", "SKIP_DIFFERENT", "KEEP_BOTH")
