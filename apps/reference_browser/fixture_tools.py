"""Offline maintenance: regenerate the packaged evidence map and generation seals.

Run with Django settings initialized, e.g. `manage.py shell -c 'from
apps.reference_browser.fixture_tools import regenerate; regenerate()'`. No runtime
view calls this code, and it never accepts external source paths or identities.
"""
import json
from hashlib import sha256

from .store import APP_ROOT, expected_evidence, fixture_snapshots


def regenerate():
    payload = (APP_ROOT / "fixtures/snapshots.v1.json").read_bytes()
    entries = expected_evidence(fixture_snapshots(payload))
    evidence = "".join(json.dumps(entry.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
                       for entry in entries).encode()
    manifest = {"schema_version": "reference-browser.generation.1",
                "snapshots_sha256": sha256(payload).hexdigest(), "evidence_sha256": sha256(evidence).hexdigest()}
    (APP_ROOT / "evidence-map.v1.jsonl").write_bytes(evidence)
    (APP_ROOT / "fixtures/generation.v1.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
