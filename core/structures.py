"""Structure snapshots — detect when a website changes its API response shape.

Every active source can have a snapshot file at
``core/structure_snapshots/{slug}.json`` containing the flattened dotted-path
keys of its first listing item (the "shape" of one job object). Comparing a
live response against the snapshot shows exactly which fields were added,
removed or renamed — so a silent API change never goes unnoticed.

Commands:
* ``manage.py capture_structure <slug>`` — write (or refresh) the snapshot
  from the live API.
* ``manage.py check_structure [<slug>]`` — compare the live response against
  the snapshot and report the diff (non-zero exit on change).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Version-controlled directory of per-source structure snapshots.
SNAPSHOT_DIR = Path(__file__).resolve().parent / "structure_snapshots"


def extract_structure(item: Any, prefix: str = "") -> list[str]:
    """Flatten one raw item into sorted dotted paths.

    Lists are represented by their first element (the shape of one row), so
    ``skill_requirements[0].skill.name`` becomes ``skill_requirements.skill.name``.
    Returns [] for empty/None payloads.
    """
    paths: list[str] = []
    if isinstance(item, dict):
        for key, value in item.items():
            dotted = f"{prefix}.{key}" if prefix else key
            if isinstance(value, list):
                if value and isinstance(value[0], (dict, list)):
                    paths.extend(extract_structure(value[0], dotted))
                else:
                    paths.append(dotted)
            elif isinstance(value, dict):
                paths.extend(extract_structure(value, dotted))
            else:
                paths.append(dotted)
    return sorted(paths)


def snapshot_path(slug: str) -> Path:
    """Filesystem path of the structure snapshot for a source slug."""
    return SNAPSHOT_DIR / f"{slug}.json"


def load_structure(slug: str) -> dict | None:
    """Read a source's structure snapshot (None when missing)."""
    path = snapshot_path(slug)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def compare_structures(
    current: list[str], stored: list[str]
) -> tuple[list[str], list[str]]:
    """Diff two structure field lists. Returns (added, removed)."""
    current_set, stored_set = set(current), set(stored)
    return sorted(current_set - stored_set), sorted(stored_set - current_set)
