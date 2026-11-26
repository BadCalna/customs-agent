"""Pin P23 and C metadata without querying a business table or enabling history."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from adapters.engines.reference_bundle import ReferenceBundle, ReferenceBundleConfig, ReferenceBundleError
from adapters.sources.reference_import.publish import PublishError, require_plain_path

from .access import catalog_visible, setting
from .contracts import digest
from .errors import DebugError

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}\Z")
# The display label is defined by the module; never publish arbitrary map text.
LABELS = {"note": "注释段落", "hs": "税则目录条目"}


@dataclass(frozen=True)
class Materials:
    bundle: ReferenceBundle
    snapshot_id: str
    index: dict
    map_digest: str


def _source_map(release_id, snapshot_id):
    root = setting("OPS_DEBUG_EVIDENCE_MAP_ROOT")
    if not root:
        return {}, ""
    path = Path(root) / release_id / "evidence-map.v1.jsonl"
    try:
        require_plain_path(path)
        if not path.is_file():
            return {}, ""
        with path.open("rb") as stream:
            raw = stream.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError
        index, ambiguous = {}, set()
        for line in raw.decode("utf-8").splitlines():
            def unique_pairs(pairs):
                item = dict(pairs)
                if len(item) != len(pairs):
                    raise ValueError
                return item

            row = json.loads(line, object_pairs_hook=unique_pairs)
            if set(row) != {"kind", "snapshot_id", "source_locator", "record_id", "anchor", "label"}:
                raise ValueError
            if row["snapshot_id"] != snapshot_id or row["kind"] not in (*LABELS, "history"):
                raise ValueError
            if row["kind"] == "history":
                continue
            if not _SAFE_ID.fullmatch(row["record_id"]) or row["record_id"].lower() != row["record_id"]:
                raise ValueError
            locator = row["source_locator"]
            anchor = row["anchor"]
            # C anchors may contain Chinese text/section markers, but never paths,
            # controls, query syntax, or unbounded source-provided labels.
            if not _locator(locator) or (anchor is not None and not _locator(anchor)):
                raise ValueError
            if row["kind"] == "note" and anchor is None:
                raise ValueError
            key = row["kind"], locator
            if key in index or key in ambiguous:
                index.pop(key, None)
                ambiguous.add(key)
                continue
            index[key] = {"kind": row["kind"], "snapshot_id": snapshot_id, "record_id": row["record_id"],
                          "anchor": anchor, "label": LABELS[row["kind"]]}
        return index, digest(raw)
    except (PublishError, ValueError, OSError, TypeError):
        # A malformed or missing map removes navigation, not access control. Its
        # availability is frozen alongside the release and checked on reread.
        return {}, ""


def _locator(value):
    return (isinstance(value, str) and 1 <= len(value) <= 192 and value == value.strip()
            and all(c.isprintable() and c not in "/\\?&=%" for c in value))


def live_materials(user, *, snapshot=None, approval_review=False):
    published = setting("OPS_DEBUG_PUBLISHED_DIR")
    if not published:
        raise DebugError("REFERENCE_NOT_READY")
    pinned_id = snapshot.get("bundle_id") if snapshot else None
    pinned_fp = snapshot.get("bundle_fingerprint") if snapshot else None
    if pinned_id and not re.fullmatch(r"rel-[A-Za-z0-9_-]+-[0-9a-f]{12}", pinned_id):
        raise DebugError("REFERENCE_NOT_READY")
    config = ReferenceBundleConfig(
        release_dir=Path(published) / "releases" / pinned_id if pinned_id else None,
        published_dir=None if pinned_id else published, expected_fingerprint=pinned_fp,
        include_commentary=True,
    )
    try:
        bundle = ReferenceBundle.load(config)
        snapshot_id = bundle.source_metadata["release"]["snapshot_id"]
        if not _SAFE_ID.fullmatch(snapshot_id) or bundle.commentary_source is None:
            raise ValueError
        if not (approval_review or catalog_visible(user, snapshot_id, bundle.fingerprint)):
            raise DebugError("forbidden", 403)
        index, map_digest = _source_map(bundle.bundle_id, snapshot_id)
        if snapshot and (snapshot_id != snapshot.get("snapshot_id") or map_digest != snapshot.get("map_digest")):
            raise DebugError("PREVIEW_STALE")
        return Materials(bundle, snapshot_id, index, map_digest)
    except (ReferenceBundleError, ValueError, KeyError, OSError, TypeError):
        raise DebugError("REFERENCE_NOT_READY") from None


def reference_url(ref):
    query = {key: ref[key] for key in ("kind", "snapshot_id", "record_id", "anchor") if ref[key] is not None}
    return "/ops/data/evidence?" + urlencode(query)


def evidence_rows(materials, request, cited, output, *, sent):
    """Use exact source section anchors; do not map by name, HS code, or similarity."""
    index = materials.index if materials else {}
    if request.get("stage") == "expert_check":
        # P18 sends status summaries only; raw source records are never model citations.
        # The existing C map contract only permits hs/note. Other sources stay unlocated.
        for entry in output.evidence if output else ():
            key = entry.locator or ""
            # P14 elements may legitimately share official_tariff source_type.
            # Use the trusted adapter's identity scope, never that broad source label.
            scope = entry.evidence_id.partition(":")[0]
            version = materials.bundle.version if materials else ""
            hs_identity = f"hs:{version}:{output.target_hs}"
            ref = (index.get(("hs", key)) if scope == "hs" and entry.evidence_id == hs_identity
                   and entry.source_type == "official_tariff" else None)
            kind = scope if scope in {"hs", "elements", "tax", "reg"} else "source"
            yield "postcheck_only", kind, digest(entry.evidence_id), "located" if ref else "unverified", ref
        return
    reference = request["reference_packet"]
    notes = {r["evidence_id"]: r for r in reference["commentary"].get("records", [])}
    sent_ids = set()
    for item in reference["evidence_index"]:
        eid = item["evidence_id"]
        note = notes.get(eid)
        kind = "note" if note else "hs"
        # The exact anchor is already supplied by the same source snapshot;
        # never guess a locator by appending an ordinal to another record.
        keys = [s["anchor"] for s in note["sections"]] if note else [item.get("locator", "")]
        for key in keys or [""]:
            ref = index.get((kind, key))
            status = "located" if ref else "unverified"
            yield "retrieved", kind, digest(key or eid), status, ref
            if sent and (not note or keys):
                sent_ids.add(eid)
                yield "sent_to_model", kind, digest(key), status, ref
                if eid in cited:
                    yield "cited_by_model", kind, digest(key), status, ref
    for entry in output.evidence if output else ():
        if entry.evidence_id not in sent_ids and entry.evidence_id not in notes:
            key = entry.locator or ""
            ref = index.get(("hs", key))
            yield "postcheck_only", "hs", digest(key or entry.evidence_id), "located" if ref else "unverified", ref
