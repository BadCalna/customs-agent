"""Explicit, read-only P23/P14/P15 reference assembly for P16.

No dotenv, settings, engine factory, network, database setup or sample fallback.
P23 *releases* contain release.json/files, NOT the candidate's manifest.json:
its files index and release_fingerprint are the published snapshot manifest.
The fixed mapping below is taken from P23 DatasetId.filename (not guessed).
Hashes establish local consistency, not authenticity or professional review.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from adapters.sources import declaration_elements as elements
from adapters.sources import history_search as history
from adapters.sources import hs_lookup as tariff
from adapters.sources import tariff_commentary as commentary
from adapters.sources import tax_lookup as tax

ENV_RELEASE_DIR = "CUSTOMS_REFERENCE_RELEASE_DIR"
ENV_PUBLISHED_DIR = "CUSTOMS_REFERENCE_PUBLISHED_DIR"
ENV_HISTORY_PATH = "CUSTOMS_HISTORY_RECORDS_PATH"

# Published P23 names. regulatory_codes is only a ledger, never regulatory facts.
P23_FILE_MAP = {
    "p14_tariff": "tariff.json",
    "p14_elements": "elements.json",
    "p17_tax_rates": "tax_rates.json",
    "tariff_commentary": "commentary.json",
    "regulatory_code_ledger": "regulatory_codes.json",
    "p17_regulatory": "regulatory.json",
}
_REQUIRED = frozenset({"p14_tariff", "p14_elements", "p17_tax_rates"})
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_RELEASE_ID = re.compile(r"rel-[A-Za-z0-9_-]+-[0-9a-f]{12}\Z")


class ReferenceBundleError(ValueError):
    """Missing configuration, inconsistent release, or unsafe reference data."""


@dataclass(frozen=True)
class ReferenceBundleConfig:
    """Exactly one explicit release_dir or published_dir is required.

    from_env reads only the three named process variables; it never opens .env.
    Unreviewed real P23 releases are readable as technical reference inputs;
    require_reviewed=True refuses them. Authority/review flags are never upgraded.
    No synthetic opt-in exists.
    Optional stores must return the SAME snapshot as the validated release.
    history_source permits a caller-owned P15 PostgreSQL provider; we only read
    its injected store, never select a backend or create a connection ourselves.
    """

    release_dir: str | Path | None = None
    published_dir: str | Path | None = None
    history_path: str | Path | None = None
    require_reviewed: bool = False
    include_commentary: bool = False
    expected_version: str | None = None
    expected_fingerprint: str | None = None
    tariff_store: Any = None
    elements_store: Any = None
    tax_rate_store: Any = None
    commentary_store: Any = None
    history_source: history.HistorySearchSource | None = None
    history_store: Any = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **kwargs: Any) -> "ReferenceBundleConfig":
        env = os.environ if environ is None else environ
        values = {
            "release_dir": env.get(ENV_RELEASE_DIR, "").strip() or None,
            "published_dir": env.get(ENV_PUBLISHED_DIR, "").strip() or None,
            "history_path": env.get(ENV_HISTORY_PATH, "").strip() or None,
        }
        values.update(kwargs)
        return cls(**values)


class _FrozenStore:
    """Detached snapshots: current/file changes cannot silently replace a bundle."""

    def __init__(self, snapshot: Any):
        self._snapshot = copy.deepcopy(snapshot)

    def load(self) -> Any:
        return copy.deepcopy(self._snapshot)


def _fingerprint(value: Any) -> str:
    # P23 canonical_json: UTF-8, sorted keys, compact separators, ensure_ascii=False.
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        if path.is_symlink() or not path.is_file():
            raise ReferenceBundleError(f"Required ordinary JSON file missing: {path.name}")
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        if isinstance(exc, ReferenceBundleError):
            raise
        raise ReferenceBundleError(f"Unreadable JSON file: {path.name}") from exc
    if not isinstance(payload, dict):
        raise ReferenceBundleError(f"JSON object required: {path.name}")
    return payload, raw


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not tariff._strip_invisible(value):
        raise ReferenceBundleError(f"Nonempty {label} required")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ReferenceBundleError(f"Invalid {label}")
    return value


def _review_time(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.tzinfo is not None and parsed.utcoffset() is not None
    except ValueError:
        return False


def _plain_reference_path(path: Path) -> None:
    from adapters.sources.reference_import.publish import PublishError, require_plain_path
    try:
        require_plain_path(path)
    except PublishError as exc:
        raise ReferenceBundleError("Reference path must not contain a symlink, junction or reparse point") from exc


def _release_path(config: ReferenceBundleConfig) -> tuple[Path, bool]:
    if bool(config.release_dir) == bool(config.published_dir):
        raise ReferenceBundleError("Explicitly configure exactly one release_dir or published_dir; no default sample")
    if config.release_dir:
        path = Path(config.release_dir).expanduser()
        if os.name == "nt" and path.name == "current":
            # Windows deliberately has no current/ directory: resolve its sole
            # atomic current.json once, then validate the immutable release below.
            return _current_path(path.parent), True
        # A caller may explicitly pass published/current; freeze its target once.
        if path.is_symlink():
            if path.name != "current":
                raise ReferenceBundleError("release_dir must not be an arbitrary symlink")
            return _current_path(path.parent), True
        _plain_reference_path(path)
        return path.resolve(), False
    return _current_path(Path(config.published_dir).expanduser()), True


def _current_path(published: Path) -> Path:
    _plain_reference_path(published)
    if os.name == "nt":
        from adapters.sources.reference_import.publish import PublishError, published_release_dir
        try:
            path = published_release_dir(published)
        except PublishError as exc:
            raise ReferenceBundleError(f"Invalid published/current pointer: {exc}") from exc
        return path.resolve()
    try:
        # Do NOT resolve/read current.json through current a second time.
        target = os.readlink(published / "current")
        release_id = Path(target).name
        if not _RELEASE_ID.fullmatch(release_id) or target != f"releases/{release_id}":
            raise ReferenceBundleError("Invalid published/current pointer")
        pointer = published / "current.json"
        if pointer.is_symlink():
            if os.readlink(pointer) != "current/.current.json":
                raise ReferenceBundleError("Invalid derived current.json pointer")
        elif pointer.exists():
            raise ReferenceBundleError("Legacy independent current.json must be repaired explicitly")
        path = published / "releases" / release_id
        _plain_reference_path(path)
        if path.is_symlink():
            raise ReferenceBundleError("Release directory must not be a symlink")
        return path.resolve()
    except OSError as exc:
        raise ReferenceBundleError("Missing or unreadable published/current pointer") from exc


def _dataset(payload: Mapping[str, Any], name: str, require_reviewed: bool) -> dict[str, Any]:
    meta = payload.get("dataset")
    if not isinstance(meta, dict) or meta.get("synthetic") is not False:
        raise ReferenceBundleError(f"{name}: real dataset requires synthetic is False")
    _text(meta.get("dataset_id"), f"{name}.dataset_id")
    allowed_types = {
        "p14_tariff": ("official_tariff",),
        "p14_elements": ("official_declaration", "official_tariff"),
        "p17_tax_rates": ("official_tax", "official_finance"),
        "tariff_commentary": ("official_tariff", "official_declaration"),
        "regulatory_code_ledger": ("official_regulatory_codes_only",),
        "p17_regulatory": ("official_regulatory", "official_dual_use"),
    }
    if meta.get("source_type") not in allowed_types[name]:
        raise ReferenceBundleError(f"{name}: official source_type required")
    if meta.get("review_status") not in ("reviewed", "unreviewed"):
        raise ReferenceBundleError(f"{name}: explicit review_status required")
    if require_reviewed and meta["review_status"] != "reviewed":
        raise ReferenceBundleError(f"{name}: unreviewed data refused by require_reviewed=True")
    return meta


def _validate_release(path: Path, *, current: bool, config: ReferenceBundleConfig) -> tuple[dict[str, Any], dict[str, Any]]:
    release, _ = _read(path / "release.json")
    if release.get("schema_version") != "p23.release.1":
        raise ReferenceBundleError("Unsupported release schema_version")
    release_id = release.get("release_id")
    if not isinstance(release_id, str) or not _RELEASE_ID.fullmatch(release_id) or release_id != path.name:
        raise ReferenceBundleError("release_id does not match release directory")
    business = _digest(release.get("business_fingerprint"), "business_fingerprint")
    fingerprint = _digest(release.get("release_fingerprint"), "release_fingerprint")
    snapshot = _text(release.get("snapshot_id"), "snapshot_id")
    if release_id != f"rel-{snapshot.split('-')[-1]}-{fingerprint[:12]}":
        raise ReferenceBundleError("release_id/snapshot_id/fingerprint mismatch")
    if release.get("dataset_kind") != "real":
        raise ReferenceBundleError("Release dataset_kind must be real, never synthetic")
    _text(release.get("converter_version"), "converter_version")
    review = release.get("review")
    status = "reviewed" if review is not None else "unreviewed"
    if release.get("review_status") != status:
        raise ReferenceBundleError("Release review_status inconsistent with review record")
    if review is not None:
        if not isinstance(review, dict) or review.get("decision") != "approved":
            raise ReferenceBundleError("Release review must be approved")
        _text(review.get("reviewed_by"), "reviewed_by")
        if not _review_time(review.get("reviewed_at")) or not isinstance(review.get("scope"), list):
            raise ReferenceBundleError("Release review time/scope invalid")
    pointer = path / ".current.json"
    if current or pointer.exists() or pointer.is_symlink():
        meta, _ = _read(pointer)
        for field in ("release_id", "release_fingerprint", "business_fingerprint", "review_status"):
            if meta.get(field) != release.get(field):
                raise ReferenceBundleError(f".current.json {field} mismatch")
    files = release.get("files")
    required = _REQUIRED | ({"tariff_commentary"} if config.include_commentary else set())
    if not isinstance(files, dict) or not required.issubset(files):
        raise ReferenceBundleError("Release files manifest missing required datasets")
    payloads, output_fingerprints = {}, {}
    for name, info in files.items():
        if name not in P23_FILE_MAP or not isinstance(info, dict) or info.get("file") != P23_FILE_MAP[name]:
            raise ReferenceBundleError(f"Invalid explicit P23 file mapping: {name}")
        payload, raw = _read(path / info["file"])
        if type(info.get("bytes")) is not int or len(raw) != info["bytes"] or hashlib.sha256(raw).hexdigest() != info.get("sha256"):
            raise ReferenceBundleError(f"{name}: files manifest SHA-256/bytes mismatch")
        meta = _dataset(payload, name, config.require_reviewed)
        covered = review is not None and (not review["scope"] or name in review["scope"])
        expected = "reviewed" if covered else "unreviewed"
        if meta["review_status"] != expected or info.get("review_status") != expected:
            raise ReferenceBundleError(f"{name}: review_status mismatch")
        if covered and (meta.get("reviewed_by") != review["reviewed_by"] or meta.get("reviewed_at") != review["reviewed_at"]):
            raise ReferenceBundleError(f"{name}: reviewer/time mismatch")
        payloads[name] = payload
        output_fingerprints[name] = _fingerprint(payload)
    for name, filename in P23_FILE_MAP.items():
        if name not in files and ((path / filename).exists() or (path / filename).is_symlink()):
            raise ReferenceBundleError(f"Undeclared P23 dataset file: {name}")
    calculated = _fingerprint({
        "snapshot_business_fingerprint": business,
        "converter_version": release["converter_version"],
        "review": review,
        "outputs": output_fingerprints,
    })
    if calculated != fingerprint:
        raise ReferenceBundleError("release_fingerprint does not match published content")
    if config.expected_fingerprint is not None and config.expected_fingerprint != fingerprint:
        raise ReferenceBundleError("Expected release fingerprint mismatch")
    return release, payloads


def _parse(parser: Any, payload: Any, label: str, injected: Any = None) -> _FrozenStore:
    try:
        snapshot = parser(payload, source_name=P23_FILE_MAP[label])
        if injected is not None:
            supplied = injected.load()
            if supplied != snapshot:
                raise ReferenceBundleError(f"{label}: injected store does not match release snapshot")
            snapshot = supplied
        return _FrozenStore(snapshot)
    except ReferenceBundleError:
        raise
    except Exception as exc:
        raise ReferenceBundleError(f"{label}: snapshot contract invalid ({type(exc).__name__})") from exc


def _history_source(config: ReferenceBundleConfig) -> tuple[history.HistorySearchSource | None, dict[str, Any] | None]:
    choices = [config.history_path is not None, config.history_source is not None, config.history_store is not None]
    if sum(choices) > 1:
        raise ReferenceBundleError("Configure only one history_path/history_source/history_store")
    if not any(choices):
        return None, None
    source = config.history_source
    try:
        if config.history_path is not None:
            from adapters.sources.history_publish import resolve_history_file

            # P15 Windows public paths may retain legacy flat bytes. Pin the
            # authoritative generation once before reading the frozen bundle.
            payload, _ = _read(resolve_history_file(Path(config.history_path).expanduser()))
            if payload.get("schema_version") != history.SCHEMA_VERSION:
                raise ReferenceBundleError("Explicit P15 history schema_version required")
            if not isinstance(payload.get("dataset"), dict) or payload["dataset"].get("synthetic") is not False:
                raise ReferenceBundleError("History dataset requires synthetic is False")
            snapshot = history._snapshot_from_payload(payload, source_name="explicit-history")
        else:
            if source is not None and not isinstance(source, history.HistorySearchSource):
                raise ReferenceBundleError("history_source must be an injected P15 HistorySearchSource")
            # P15 exposes no public snapshot accessor. Never call its no-arg ctor:
            # that path reads settings and may select the default synthetic fixture.
            store = source._store if source is not None else config.history_store
            snapshot = store.load()
        if not isinstance(snapshot, history.HistorySnapshot) or snapshot.dataset.synthetic is not False:
            raise ReferenceBundleError("History store must return a real P15 snapshot")
        _text(snapshot.dataset.dataset_id, "history.dataset_id")
        material = {"dataset": snapshot.dataset.model_dump(mode="json"),
                    "records": [record.model_dump(mode="json") for record in snapshot.records]}
        metadata = {"dataset_id": snapshot.dataset.dataset_id, "synthetic": False,
                    "reference_only": True, "fingerprint": _fingerprint(material),
                    "record_count": len(snapshot.records)}
        options = {}
        if source is not None:
            options = {key: getattr(source, key) for key in ("limit", "min_score", "stale_days", "include_needs_review")}
            options["clock"] = source._clock
        return history.HistorySearchSource(store=_FrozenStore(snapshot), **options), metadata
    except ReferenceBundleError:
        raise
    except Exception as exc:
        raise ReferenceBundleError(f"Explicit history source unavailable ({type(exc).__name__})") from exc


@dataclass(frozen=True)
class ReferenceBundle:
    """One pinned release and optional independently versioned history snapshot.

    version is the shared P14 version, NOT the tax or commentary version:
    P23 currently publishes 2026 / 2026C / 2026-11-17T10-20 respectively.
    fingerprint is the P23 release fingerprint; history fingerprint is separate
    in source_metadata. Metadata contains no private record/reviewer/path data.
    """

    bundle_id: str
    version: str
    fingerprint: str
    source_metadata: Mapping[str, Any]
    hs_source: tariff.HsLookupSource
    elements_source: elements.DeclarationElementsSource
    tax_source: tax.TaxRateSource
    history_source: history.HistorySearchSource | None
    commentary_source: commentary.TariffCommentarySource | None
    tariff_store: Any
    elements_store: Any
    tax_rate_store: Any
    commentary_store: Any = None

    @classmethod
    def load(cls, config: ReferenceBundleConfig | None = None) -> "ReferenceBundle":
        config = ReferenceBundleConfig.from_env() if config is None else config
        if not isinstance(config, ReferenceBundleConfig):
            raise ReferenceBundleError("ReferenceBundleConfig required")
        if type(config.require_reviewed) is not bool or type(config.include_commentary) is not bool:
            raise ReferenceBundleError("require_reviewed/include_commentary must be bool")
        if config.commentary_store is not None and not config.include_commentary:
            raise ReferenceBundleError("commentary_store requires include_commentary=True")
        path, current = _release_path(config)
        release, payloads = _validate_release(path, current=current, config=config)
        ts = _parse(tariff._snapshot_from_payload, payloads["p14_tariff"], "p14_tariff", config.tariff_store)
        es = _parse(elements._snapshot_from_payload, payloads["p14_elements"], "p14_elements", config.elements_store)
        xs = _parse(tax._snapshot_from_payload, payloads["p17_tax_rates"], "p17_tax_rates", config.tax_rate_store)
        tariff_snapshot, element_snapshot, tax_snapshot = ts.load(), es.load(), xs.load()
        if tariff_snapshot.versions != element_snapshot.versions:
            # Source/labels may legitimately differ across official datasets.
            left = [(v.version, v.effective_from, v.effective_to) for v in tariff_snapshot.versions]
            right = [(v.version, v.effective_from, v.effective_to) for v in element_snapshot.versions]
            if sorted(left) != sorted(right):
                raise ReferenceBundleError("Tariff/elements version windows inconsistent")
        versions = [v.version for v in tariff_snapshot.versions]
        if len(versions) != 1:
            raise ReferenceBundleError("A bundle requires one explicit P14 version")
        version = versions[0]
        if config.expected_version is not None and config.expected_version != version:
            raise ReferenceBundleError("Expected P14 version mismatch")
        cs = None
        if "tariff_commentary" in payloads:
            # Validate commentary even if its consumption is explicitly disabled.
            cs = _parse(commentary._snapshot_from_payload, payloads["tariff_commentary"], "tariff_commentary", config.commentary_store)
            if not cs.load().version:
                raise ReferenceBundleError("Commentary version required")
        metadata: dict[str, Any] = {}
        for name, payload in payloads.items():
            meta = payload["dataset"]
            metadata[name] = {"dataset_id": meta["dataset_id"], "synthetic": False,
                              "source_type": meta["source_type"], "review_status": meta["review_status"],
                              "sha256": release["files"][name]["sha256"],
                              "file": P23_FILE_MAP[name], "schema_version": payload.get("schema_version")}
        for name, snapshot in (("p14_tariff", tariff_snapshot),
                               ("p14_elements", element_snapshot),
                               ("p17_tax_rates", tax_snapshot)):
            metadata[name]["authoritative"] = snapshot.dataset.authoritative
        if cs is not None:
            metadata["tariff_commentary"]["authoritative"] = cs.load().dataset.authoritative
        metadata["p14_tariff"]["versions"] = versions
        metadata["p14_elements"]["versions"] = [v.version for v in element_snapshot.versions]
        metadata["p17_tax_rates"]["versions"] = [v.version for v in tax_snapshot.versions]
        if cs is not None:
            metadata["tariff_commentary"]["version"] = cs.load().version
        hs, hm = _history_source(config)
        metadata["history"] = hm
        metadata["release"] = {key: release[key] for key in (
            "release_id", "snapshot_id", "business_fingerprint", "release_fingerprint", "review_status", "converter_version")}
        return cls(
            bundle_id=release["release_id"], version=version,
            fingerprint=release["release_fingerprint"], source_metadata=metadata,
            hs_source=tariff.HsLookupSource(ts), elements_source=elements.DeclarationElementsSource(es),
            tax_source=tax.TaxRateSource(xs), history_source=hs,
            commentary_source=commentary.TariffCommentarySource(cs) if config.include_commentary and cs is not None else None,
            tariff_store=ts, elements_store=es, tax_rate_store=xs,
            commentary_store=cs if config.include_commentary else None,
        )
