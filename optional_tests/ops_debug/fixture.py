from __future__ import annotations

import hashlib
import json
from pathlib import Path
from shutil import copytree

from tests.p16_real.test_reference_bundle import release_fixture


def make_published(root: Path, *, reviewed: bool = True):
    """Create the exact offline P23 release shape and evidence map contract."""
    path, release = release_fixture(root, reviewed=reviewed)
    map_dir = root / "maps" / release["release_id"]
    map_dir.mkdir(parents=True, exist_ok=True)
    row = {"kind": "note", "snapshot_id": release["snapshot_id"], "source_locator": "0101#1",
           "record_id": "note-ch84", "anchor": "0101#1", "label": "ignored-by-B"}
    raw = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    (map_dir / "evidence-map.v1.jsonl").write_bytes(raw)
    return path, release, map_dir
