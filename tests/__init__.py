from __future__ import annotations

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
SCHEMA_ROOT = REPO_ROOT / "schemas"

for search_root in (REPO_ROOT, SRC_ROOT):
    search_path = str(search_root)
    if search_path in sys.path:
        sys.path.remove(search_path)
    sys.path.insert(0, search_path)

__all__ = ("REPO_ROOT", "SCHEMA_ROOT", "SRC_ROOT")
