"""Load one sealed, immutable generation of strictly synthetic local reference data.

No user input becomes a path; no ORM, live reference adapter, or network is used.
Existing source DTOs validate fixture payloads but do not choose their versions.
"""
from dataclasses import dataclass
from functools import cache
from hashlib import sha256
import json
from pathlib import Path
import re
from types import MappingProxyType

from django.conf import settings
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from adapters.sources.declaration_elements import ElementSchema
from adapters.sources.hs_lookup import TariffCode
from adapters.sources.tariff_commentary import CommentaryRecord
from adapters.sources.tax_lookup import RateKind, TaxRateRecord

APP_ROOT = Path(__file__).resolve().parent
ID = re.compile(r"[a-z0-9][a-z0-9._:-]{0,191}\Z")
LOCATOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:#-]{0,191}\Z")
ANCHOR = re.compile(r"[^/\\\s\x00-\x1f\x7f]{1,192}\Z")
HS_CODE = re.compile(r"(?:[0-9]{8}|[0-9]{10})\Z")
PHONE = re.compile(r"(?<![0-9])1[3-9][0-9]{9}(?![0-9])")
FILE_EXT = re.compile(r"\.(?:xlsx?|xlsm|csv|jsonl?|pdf|docx?|txt|jpe?g|png|zip)(?:$|[:#])", re.I)
KINDS = frozenset({"note", "hs", "history"})
DATA_STATES = frozenset({"provided", "not_provided", "unsupported", "input_missing"})
ROW_STATES = frozenset({"imported", "duplicate", "conflict", "pending", "rejected", "not_imported"})
MATCH_LEVELS = frozenset({"exact_10", "fallback_8", "exact_8"})


class SnapshotUnreadable(Exception):
    """Input is missing, malformed, inconsistent or failed its checksum."""


def safe_key(value, pattern=ID):
    return (isinstance(value, str) and pattern.fullmatch(value) is not None
            and ".." not in value and not PHONE.search(value) and not FILE_EXT.search(value)
            and not re.match(r"[a-zA-Z]:", value))


def check(condition):
    if not condition:
        raise SnapshotUnreadable()


def freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze(item) for item in value)
    return value


def as_json(value):
    if isinstance(value, MappingProxyType):
        return {key: as_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [as_json(item) for item in value]
    return value


class HistoricalPayload(BaseModel):
    """Only pre-redacted fields are allowed; original source material is not loaded."""
    model_config = ConfigDict(extra="forbid", strict=True)

    dataset_key: str
    revision: int = Field(ge=1)
    historical_hs: str | None
    historical_name: str | None
    recorded_at: str | None
    source_system: str
    needs_review: bool
    blocking_issue_codes: list[str]
    version_status: str
    outcome: str | None
    specification: str | None
    material: str | None
    elements: dict[str, str]
    scope: str | None
    effective_from: str | None
    effective_to: str | None


@dataclass(frozen=True, slots=True)
class ReferenceRecord:
    record_id: str
    source_locator: str
    status: str
    reason: str | None
    label: str
    payload: MappingProxyType


@dataclass(frozen=True, slots=True)
class DataSnapshot:
    snapshot_id: str
    kind: str
    version: str
    state: str
    withdrawn: bool
    metadata: MappingProxyType
    records: MappingProxyType
    datasets: MappingProxyType


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    kind: str
    snapshot_id: str
    record_id: str
    anchor: str | None
    label: str

    def to_dict(self):
        return {"kind": self.kind, "snapshot_id": self.snapshot_id, "record_id": self.record_id,
                "anchor": self.anchor, "label": self.label}


@dataclass(frozen=True, slots=True)
class EvidenceMapEntry:
    reference: EvidenceRef
    source_locator: str

    def to_dict(self):
        return {**self.reference.to_dict(), "source_locator": self.source_locator}


@dataclass(frozen=True, slots=True)
class Generation:
    snapshots: MappingProxyType
    evidence: MappingProxyType


EMPTY_GENERATION = Generation(freeze({}), freeze({}))


def _note_payload(raw):
    dto = CommentaryRecord.model_validate(raw["note"])
    check(dto.level is not None)
    check(len({item.anchor for item in dto.sections}) == len(dto.sections))
    check(all(safe_key(item.anchor, ANCHOR) for item in dto.sections))
    check(safe_key(dto.source.locator, LOCATOR))
    payload = dto.model_dump(mode="json")
    return {**payload, "level": dto.level.value, "parent_key": raw.get("parent_key"),
            "updated_at": raw.get("updated_at"), "source_id": raw["source_id"]}


def _hs_payload(raw):
    dto = TariffCode.model_validate(raw["tariff"])
    check(HS_CODE.fullmatch(dto.code) and safe_key(dto.source.locator, LOCATOR))
    elements = ElementSchema.model_validate(raw["element_schema"]) if raw["element_schema"] is not None else None
    if elements:
        check((elements.hs_code, elements.version) == (dto.code, dto.version))
        check(safe_key(elements.source.locator, LOCATOR))
    rate = TaxRateRecord.model_validate(raw["tax_rate"]) if raw["tax_rate"] is not None else None
    match_level = raw["match_level"]
    check(match_level in MATCH_LEVELS if rate else match_level is None)
    if rate:
        expected_code = dto.code[:8] if match_level == "fallback_8" else dto.code
        expected_level = ("exact_10" if len(dto.code) == 10 else "exact_8")
        check((rate.hs_code, rate.version) == (expected_code, dto.version))
        check(match_level == expected_level or (match_level == "fallback_8" and len(dto.code) == 10))
        check(safe_key(rate.source.locator, LOCATOR))
        if rate.rate_kind == RateKind.ZERO_RATE:
            check(re.fullmatch(r"0(?:\.0+)?\s*[%％]?", rate.rate_raw.strip()))
    units = raw["units"]
    check(isinstance(units, list) and all(unit is None or isinstance(unit, str) for unit in units))
    ledger = raw["regulatory_code_ledger"]
    check(ledger is None or isinstance(ledger, str))
    return {**dto.model_dump(mode="json"), "name": dto.description, "units": units,
            "regulatory_code_ledger": ledger, "regulatory_measures": {"state": "not_provided"},
            "import_duty": {"state": "not_provided"}, "import_vat": {"state": "not_provided"},
            "elements": [row.model_dump(mode="json") for row in elements.ordered()] if elements else [],
            "element_source": elements.source.model_dump(mode="json") if elements else None,
            "element_state": raw["element_state"], "tax_state": raw["tax_state"],
            "tax_rate": ({**rate.model_dump(mode="json"), "conditions_state": rate.conditions_state.value,
                          "match_level": match_level, "verification_state": raw["tax_verification_state"]}
                         if rate else None),
            "match_level": match_level, "needs_review": raw["needs_review"],
            "tax_match_warning": raw.get("tax_match_warning"), "source_variants": raw.get("source_variants", []),
            "source_count": raw.get("source_count"), "source_id": raw["source_id"]}


def _read_record(kind, raw):
    record_id, locator, status, label = (raw[key] for key in ("record_id", "source_locator", "status", "label"))
    check(safe_key(record_id) and safe_key(locator, LOCATOR) and status in ROW_STATES)
    check(isinstance(label, str) and 0 < len(label) <= 120)
    if kind == "note":
        payload = _note_payload(raw)
    elif kind == "hs":
        payload = _hs_payload(raw)
    else:
        history = HistoricalPayload.model_validate(raw["history"])
        check(safe_key(history.dataset_key))
        check(history.version_status in {"version_unverified", "verified"})
        check(history.historical_hs is None or re.fullmatch(r"[0-9]{4,18}", history.historical_hs))
        payload = {**history.model_dump(mode="json"), "history_anchor": raw["history_anchor"]}
        check(safe_key(payload["history_anchor"], ANCHOR))
    return ReferenceRecord(record_id, locator, status, raw.get("reason"), label, freeze(payload))


def _read_snapshots(document):
    check(document["schema_version"] == "reference-browser.fixture.1" and document["synthetic"] is True)
    snapshots = {}
    active_kinds = set()
    for raw in document["snapshots"]:
        snapshot_id, kind, state = (raw[key] for key in ("snapshot_id", "kind", "data_status"))
        check(safe_key(snapshot_id) and kind in KINDS and state in DATA_STATES)
        check(raw["synthetic"] is True and type(raw["withdrawn"]) is bool)
        check(safe_key(raw["release_id"]) and type(raw["is_active_pointer"]) is bool)
        check((snapshot_id, kind) not in snapshots)
        if raw["is_active_pointer"]:
            check(kind not in active_kinds and not raw["withdrawn"])
            active_kinds.add(kind)
        records = {}
        datasets = {}
        for dataset in raw.get("datasets", []):
            key = dataset["dataset_key"]
            check(kind == "history" and safe_key(key) and key not in datasets and dataset["synthetic"] is True)
            datasets[key] = freeze(dataset)
        tariff_keys = set()
        for value in raw["records"]:
            record = _read_record(kind, value)
            check(record.record_id not in records and state == "provided")
            if kind == "history":
                dataset = datasets.get(record.payload["dataset_key"])
                check(dataset is not None and record.payload["revision"] <= dataset["revision"])
            else:
                key = record.payload["tariff_no" if kind == "note" else "code"]
                check(key not in tariff_keys)
                tariff_keys.add(key)
                if kind == "hs":
                    check(record.payload["version"] == raw["version"])
            records[record.record_id] = record
        if kind == "note":
            for record in records.values():
                parent = record.payload["parent_key"]
                check(parent is None or (parent in tariff_keys and parent != record.payload["tariff_no"]))
        metadata = {key: value for key, value in raw.items() if key not in {"records", "datasets", "withdrawn"}}
        snapshots[snapshot_id, kind] = DataSnapshot(snapshot_id, kind, raw["version"], state, raw["withdrawn"],
                                                     freeze(metadata), freeze(records), freeze(datasets))
    return snapshots


def expected_evidence(snapshots):
    """Materialize the offline manifest from record IDs and declared anchors; never infer IDs."""
    for snapshot in snapshots.values():
        if snapshot.withdrawn:
            continue
        for record in snapshot.records.values():
            if record.status in {"rejected", "not_imported"}:
                continue
            anchors = [(None, record.label, record.source_locator)]
            if snapshot.kind == "note":
                anchors += [(item["anchor"], item["label"], f"{record.source_locator}:{item['anchor']}")
                            for item in record.payload["sections"]]
            if snapshot.kind == "history":
                anchors.append((record.payload["history_anchor"], record.label, record.source_locator))
            for anchor, label, locator in anchors:
                check(safe_key(locator, LOCATOR))
                yield EvidenceMapEntry(EvidenceRef(snapshot.kind, snapshot.snapshot_id, record.record_id, anchor, label),
                                       locator)


def _load_evidence(lines, snapshots):
    expected = {_evidence_key(entry.reference): entry for entry in expected_evidence(snapshots)}
    loaded = {}
    source_keys = set()
    for line in lines.splitlines():
        raw = _decode(line)
        check(set(raw) == {"kind", "snapshot_id", "record_id", "anchor", "label", "source_locator"})
        ref = EvidenceRef(**{key: raw[key] for key in ("kind", "snapshot_id", "record_id", "anchor", "label")})
        key = _evidence_key(ref)
        source_key = (ref.kind, ref.snapshot_id, raw["source_locator"], ref.anchor)
        check(key not in loaded and source_key not in source_keys and key in expected)
        entry = expected[key]
        check(entry.reference == ref and entry.source_locator == raw["source_locator"])
        loaded[key] = entry
        source_keys.add(source_key)
    check(loaded.keys() == expected.keys())
    return loaded


def _evidence_key(ref):
    return ref.kind, ref.snapshot_id, ref.record_id, ref.anchor


def _decode(raw):
    def unique_object(pairs):
        members = dict(pairs)
        check(len(members) == len(pairs))
        return members
    return json.loads(raw, object_pairs_hook=unique_object)


def fixture_snapshots(raw):
    """Validate standalone payload used by the offline evidence-map generator."""
    return _read_snapshots(_decode(raw))


def parse_generation(manifest, payload, evidence):
    """Check both immutable files against the same generation before yielding any rows."""
    try:
        seals = _decode(manifest)
        check(seals["schema_version"] == "reference-browser.generation.1")
        check(sha256(payload).hexdigest() == seals["snapshots_sha256"])
        check(sha256(evidence).hexdigest() == seals["evidence_sha256"])
        snapshots = fixture_snapshots(payload)
        return Generation(freeze(snapshots), freeze(_load_evidence(evidence, snapshots)))
    except (ValueError, TypeError, KeyError, AttributeError, ValidationError):
        raise SnapshotUnreadable() from None


@cache
def _packaged_generation():
    try:
        return parse_generation((APP_ROOT / "fixtures/generation.v1.json").read_bytes(),
                                (APP_ROOT / "fixtures/snapshots.v1.json").read_bytes(),
                                (APP_ROOT / "evidence-map.v1.jsonl").read_bytes())
    except OSError:
        raise SnapshotUnreadable() from None


def load_generation():
    enabled = ((settings.DEBUG and getattr(settings, "REFERENCE_BROWSER_FIXTURE", False) is True)
               or getattr(settings, "REFERENCE_BROWSER_FIXTURE_TESTING", False) is True)
    return _packaged_generation() if enabled else EMPTY_GENERATION
