"""Snapshot-pinned filtering, projections and evidence resolution (no business I/O)."""
from dataclasses import dataclass
import re

from django.conf import settings

from adapters.sources.tariff_commentary import _normalize_key, _search_keyword

from .errors import FORBIDDEN, INVALID, NOT_FOUND, SNAPSHOT_REQUIRED, UNAUTHORIZED
from .permissions import (VIEW_CATALOG, VIEW_HISTORY, VIEW_HISTORY_DETAIL, actor_can,
                          authenticated, require_permission, scope_allows)
from .store import ANCHOR, HS_CODE, KINDS, MATCH_LEVELS, ROW_STATES, as_json, load_generation, safe_key

NOTE_SORTS = {"tariff_no": ("tariff_no", False), "title": ("title", False),
              "text_length": ("text_length", True), "updated_at": ("updated_at", True)}
HS_SORTS = {"code": ("code", False), "rate_percent": ("rate_percent", True),
            "effective_from": ("effective_from", True), "record_id": ("record_id", False)}
HISTORY_SORTS = {"recorded_at": ("recorded_at", True), "record_id": ("record_id", False)}
HISTORY_REASONS = {"reference_review", "source_verification", "quality_review"}
PAGE_FIELDS = {"snapshot_id", "page", "page_size", "sort"}
UNAVAILABLE = {"rejected", "not_imported"}
SUCCESS = {"approved", "passed", "放行", "通过"}


class Query:
    """Reject duplicate/unknown or unbounded input; never reflect it in errors/logs."""

    def __init__(self, params):
        self.params = params

    def get(self, key, default="", limit=256):
        values = self.params.getlist(key)
        if not values:
            return default
        if len(values) != 1 or len(values[0]) > limit or any(ord(c) < 32 for c in values[0]):
            raise INVALID()
        return values[0].strip()

    def fields(self, *allowed):
        if self.params.keys() - set(allowed):
            raise INVALID()
        for key in self.params:
            self.get(key)

    def choice(self, key, choices, default=""):
        value = self.get(key, default)
        if (value or default) and value not in choices:
            raise INVALID()
        return value

    def boolean(self, key):
        return {"true": True, "false": False}.get(self.choice(key, {"true", "false"}))

    def pagination(self):
        raw_page = self.get("page", "1")
        if not re.fullmatch(r"[1-9][0-9]{0,8}", raw_page):
            raise INVALID()
        size = self.choice("page_size", {"20", "50", "100"}, "20")
        return int(raw_page), int(size)


def _require_kind(user, kind, *, detail=False):
    if not authenticated(user):
        raise UNAUTHORIZED()
    if kind == "history":
        require_permission(user, VIEW_HISTORY)
        if detail:
            require_permission(user, VIEW_HISTORY_DETAIL)
    elif kind in {"note", "hs"}:
        require_permission(user, VIEW_CATALOG)
    else:
        if not (actor_can(user, VIEW_CATALOG) or actor_can(user, VIEW_HISTORY)):
            raise FORBIDDEN()
        raise INVALID()


@dataclass(frozen=True)
class PinnedSnapshot:
    generation: object
    snapshot: object
    user: object

    def scope(self, *, dataset_key=None, record_id=None):
        return scope_allows(self.user, kind=self.snapshot.kind, snapshot_id=self.snapshot.snapshot_id,
                            dataset_key=dataset_key, record_id=record_id)

    def visible(self, record):
        key = record.payload.get("dataset_key")
        return (key is None or self.scope(dataset_key=key)) and self.scope(dataset_key=key, record_id=record.record_id)

    def dataset_items(self):
        result = []
        for key, data in self.snapshot.datasets.items():
            if self.scope(dataset_key=key):
                count = sum(row.payload["dataset_key"] == key and self.visible(row)
                            for row in self.snapshot.records.values())
                result.append({**as_json(data), "record_count": count})
        return result

    def summary(self):
        result = as_json(self.snapshot.metadata)
        if self.snapshot.kind == "history" or getattr(settings, "REFERENCE_BROWSER_SCOPE_POLICY", None) is not None:
            # Global fixture metadata may include counts/lineage about hidden rows or datasets.
            result.pop("source_manifest", None)
            result["statistics"] = {"record_count": sum(self.visible(row) for row in self.snapshot.records.values())}
        return result

    def envelope(self, **data):
        summary = self.summary()
        return {"snapshot": summary, "snapshot_id": self.snapshot.snapshot_id,
                "release_id": summary["release_id"], "data_status": self.snapshot.state, **data}

    def record(self, record_id):
        row = self.snapshot.records.get(record_id)
        if row is None or not self.visible(row):
            raise NOT_FOUND()
        return row

    def evidence(self, row, anchor=None):
        return self.generation.evidence.get((self.snapshot.kind, self.snapshot.snapshot_id, row.record_id, anchor))


def _pin(user, query, kind):
    snapshot_id = query.get("snapshot_id")
    if not snapshot_id:
        raise SNAPSHOT_REQUIRED()
    if not safe_key(snapshot_id):
        raise INVALID()
    if not scope_allows(user, kind=kind, snapshot_id=snapshot_id):
        raise NOT_FOUND()
    generation = load_generation()
    snapshot = generation.snapshots.get((snapshot_id, kind))
    if snapshot is None or snapshot.withdrawn:
        raise NOT_FOUND()
    return PinnedSnapshot(generation, snapshot, user)


def snapshots(user, query):
    if not authenticated(user):
        raise UNAUTHORIZED()
    kinds = ({"note", "hs"} if actor_can(user, VIEW_CATALOG) else set())
    if actor_can(user, VIEW_HISTORY):
        kinds.add("history")
    if not kinds:
        raise FORBIDDEN()
    query.fields("kind")
    kind = query.choice("kind", KINDS)
    if kind:
        kinds &= {kind}
    generation = load_generation()
    visible = [PinnedSnapshot(generation, snapshot, user) for snapshot in generation.snapshots.values()
               if snapshot.kind in kinds and not snapshot.withdrawn and scope_allows(
                   user, kind=snapshot.kind, snapshot_id=snapshot.snapshot_id)]
    return {"items": [bound.summary() for bound in visible]}


def _sort(rows, key, options):
    # Sort secondarily by record_id asc even when primary order is desc, NULLs last.
    field, reverse = options[key]
    rows.sort(key=lambda row: row["record_id"])
    present = [row for row in rows if row.get(field) is not None]
    missing = [row for row in rows if row.get(field) is None]
    present.sort(key=lambda row: row[field], reverse=reverse)
    return present + missing


def _page(bound, rows, pagination, **extra):
    page, size = pagination
    start = (page - 1) * size
    state = bound.snapshot.state
    outcome = ("matched" if rows else "no_match") if state == "provided" else state
    return bound.envelope(items=rows[start:start + size], page=page, page_size=size,
                          total=len(rows), has_next=start + size < len(rows), outcome=outcome, **extra)


def _text(value, needle):
    return not needle or _search_keyword(needle).casefold() in _search_keyword(value or "").casefold()


def _core(row):
    core = {"record_id": row.record_id, "status": row.status, "reason": row.reason}
    if row.status not in UNAVAILABLE:
        core["source_locator"] = row.source_locator
    return core


def _catalog_row(bound, row, *, detail=False):
    core = _core(row)
    if row.status in UNAVAILABLE:
        return core
    p = row.payload
    entry = bound.evidence(row)
    core["evidence_ref"] = entry.reference.to_dict() if entry else None
    if bound.snapshot.kind == "note":
        result = {**core, **{key: p[key] for key in ("tariff_no", "level", "kind", "title", "chapter",
                                                    "text_length", "updated_at")}, "section_count": len(p["sections"])}
        if detail:
            result.update({**as_json(p), **result})
            result["sections"] = [{**as_json(section), "text_length": len(section["text"]),
                                   "evidence_ref": bound.evidence(row, section["anchor"]).reference.to_dict()}
                                  for section in p["sections"]]
        return result
    tax = p["tax_rate"]
    result = {**core, **{key: p[key] for key in ("code", "name", "version", "state", "effective_from", "effective_to",
                                                 "match_level", "needs_review", "element_state", "tax_state")},
              "rate_percent": tax["rate_percent"] if tax else None}
    return {**as_json(p), **result} if detail else result


def _catalog_kind(user, query):
    require_permission(user, VIEW_CATALOG)
    tab = query.choice("tab", {"note", "hs"})
    kind = query.choice("kind", {"note", "hs"})
    if tab and kind and tab != kind:
        raise INVALID()
    return tab or kind or "note"


def _note_filter(query):
    query.fields(*PAGE_FIELDS, "tab", "kind", "query", "key", "tariff_no", "title", "chapter", "level", "keyword", "label", "status")
    key = query.get("key") or query.get("tariff_no")
    if key:
        key, _, problem = _normalize_key(key)
        if problem:
            raise INVALID()
    keyword, label, title, chapter, general = [query.get(name) for name in ("keyword", "label", "title", "chapter", "query")]
    status = query.choice("status", ROW_STATES)
    level = query.choice("level", {"heading", "chapter_note", "section_note", "other_note"})

    def matches(row):
        p = row.payload
        if status and row.status != status:
            return False
        if row.status in UNAVAILABLE:
            return not any((key, keyword, label, title, chapter, general, level))
        if (key and key != p["tariff_no"]) or (level and level != p["level"]):
            return False
        if not _text(p["title"], title) or not _text(p["chapter"], chapter):
            return False
        if general and not any(_text(p[field], general) for field in ("tariff_no", "title", "chapter", "text_all")):
            return False
        if label:
            return any(section["label"] == label and _text(section["text"], keyword) for section in p["sections"])
        return _text(p["text_all"], keyword)
    return matches


def _hs_filter(query):
    query.fields(*PAGE_FIELDS, "tab", "kind", "query", "hs_code", "code", "prefix", "match", "name", "version", "status", "state", "match_level", "needs_review")
    code = query.get("code") or query.get("hs_code")
    prefix = query.get("prefix")
    mode = query.choice("match", {"exact", "prefix"}, "exact")
    general = query.get("query")
    if not code and not prefix and re.fullmatch(r"[0-9]+", general):
        code, general = general, ""
    if code and mode == "prefix":
        if prefix and prefix != code:
            raise INVALID()
        prefix, code = code, ""
    if (code and not HS_CODE.fullmatch(code)) or (prefix and not re.fullmatch(r"[0-9]{1,10}", prefix)):
        raise INVALID()
    name, version = query.get("name"), query.get("version")
    status = query.choice("status", ROW_STATES | {"active", "expired", "merged", "unknown", "not_found"})
    state = query.choice("state", {"active", "expired", "merged", "unknown", "not_found"})
    match_level = query.choice("match_level", MATCH_LEVELS)
    needs_review = query.boolean("needs_review")

    def matches(row):
        p = row.payload
        if row.status in UNAVAILABLE:
            return (not status or status == row.status) and not any((code, prefix, name, general, version, state,
                                                                     match_level, needs_review is not None))
        return ((not code or code == p["code"]) and (not prefix or p["code"].startswith(prefix))
                and _text(p["name"], name) and _text(p["name"], general) and (not version or version == p["version"])
                and (not status or status in {row.status, p["state"]}) and (not state or state == p["state"])
                and (not match_level or match_level == p["match_level"])
                and (needs_review is None or needs_review == p["needs_review"]))
    return matches, "prefix" if prefix else "exact" if code else None


def _sort_query(query, options, default):
    raw = query.get("sort", default)
    aliases = {f"{key} {'desc' if desc else 'asc'}": key for key, (_, desc) in options.items()}
    aliases.update({f"{key}_{'desc' if desc else 'asc'}": key for key, (_, desc) in options.items()})
    key = aliases.get(raw, raw)
    if key not in options:
        raise INVALID()
    return key


def catalog(user, query):
    kind = _catalog_kind(user, query)
    if kind == "note":
        matches = _note_filter(query)
        sort_options, default = NOTE_SORTS, "tariff_no"
        mode = None
    else:
        matches, mode = _hs_filter(query)
        sort_options, default = HS_SORTS, "code"
    sort = _sort_query(query, sort_options, default)
    pagination = query.pagination()
    bound = _pin(user, query, kind)
    allowed = [row for row in bound.snapshot.records.values() if bound.visible(row)]
    rows = [_catalog_row(bound, row) for row in allowed if matches(row)]
    if kind == "hs":
        return _page(bound, _sort(rows, sort, sort_options), pagination, query_mode=mode)
    tree_rows = [row.payload for row in allowed if row.status not in UNAVAILABLE]
    keys = {row["tariff_no"] for row in tree_rows}
    tree = [{**{key: row[key] for key in ("tariff_no", "level", "chapter", "title")},
             "parent_key": row["parent_key"] if row["parent_key"] in keys else None}
            for row in sorted(tree_rows, key=lambda row: row["tariff_no"])]
    return _page(bound, _sort(rows, sort, sort_options), pagination, tree=tree)


def catalog_detail(user, query, kind, record_id):
    require_permission(user, VIEW_CATALOG)
    if kind not in {"note", "hs"} or not safe_key(record_id):
        raise INVALID()
    query.fields("snapshot_id")
    bound = _pin(user, query, kind)
    return bound.envelope(record=_catalog_row(bound, bound.record(record_id), detail=True))


def hs_detail(user, query, code):
    require_permission(user, VIEW_CATALOG)
    query.fields("snapshot_id", "version")
    if not HS_CODE.fullmatch(code) or not query.get("version"):
        raise INVALID()
    bound = _pin(user, query, "hs")
    if bound.snapshot.version != query.get("version"):
        raise NOT_FOUND()
    for row in bound.snapshot.records.values():
        if row.status not in UNAVAILABLE and row.payload["code"] == code and bound.visible(row):
            return bound.envelope(record=_catalog_row(bound, row, detail=True))
    raise NOT_FOUND()


def _history_row(bound, row, *, detail=False):
    core = _core(row)
    if row.status in UNAVAILABLE:
        return core
    p = row.payload
    visible_keys = ("dataset_key", "revision", "historical_hs", "historical_name", "recorded_at", "source_system",
                    "needs_review", "blocking_issue_codes", "version_status", "outcome")
    if detail:
        visible_keys += ("specification", "material", "elements", "scope", "effective_from", "effective_to")
    success = (p["outcome"] or "").strip().casefold() in SUCCESS
    entry = bound.evidence(row, p["history_anchor"]) if actor_can(bound.user, VIEW_HISTORY_DETAIL) else None
    return {**core, **{key: as_json(p[key]) for key in visible_keys}, "reference_only": True,
            "success_candidate": success, "success_exclusion_reason": None if success else "outcome_not_explicitly_successful",
            "source_locator": row.source_locator, "locator_summary": row.source_locator,
            "evidence_ref": entry.reference.to_dict() if entry else None}


def history(user, query):
    _require_kind(user, "history")
    query.fields(*PAGE_FIELDS, "query", "dataset_key", "dataset", "name", "hs_code", "historical_hs", "specification", "material",
                 "element_key", "element_value", "state", "status", "needs_review", "year", "recorded_at", "outcome", "success_only")
    pagination = query.pagination()
    sort = _sort_query(query, HISTORY_SORTS, "recorded_at")
    dataset = query.get("dataset_key") or query.get("dataset")
    hs_code = query.get("historical_hs") or query.get("hs_code")
    element_key, element_value = query.get("element_key"), query.get("element_value")
    year = query.get("year")
    if ((dataset and not safe_key(dataset)) or (hs_code and not HS_CODE.fullmatch(hs_code))
            or (year and not re.fullmatch(r"[0-9]{4}", year)) or (element_value and not element_key)):
        raise INVALID()
    status = query.choice("status", ROW_STATES) or query.choice("state", ROW_STATES)
    needs_review = query.boolean("needs_review")
    success_only = query.boolean("success_only")
    general, name, specification, material, recorded_at, outcome = [query.get(key) for key in (
        "query", "name", "specification", "material", "recorded_at", "outcome")]
    bound = _pin(user, query, "history")
    if dataset and (not bound.scope(dataset_key=dataset) or dataset not in bound.snapshot.datasets):
        raise NOT_FOUND()
    rows = []
    for row in bound.snapshot.records.values():
        if not bound.visible(row) or (status and status != row.status):
            continue
        p = row.payload
        if dataset and dataset != p["dataset_key"]:
            continue
        if row.status in UNAVAILABLE:
            if not any((general, name, specification, material, hs_code, year, recorded_at, outcome, element_key,
                        needs_review is not None, success_only is not None)):
                rows.append(_history_row(bound, row))
            continue
        if (hs_code and hs_code != p["historical_hs"]) or (year and not (p["recorded_at"] or "").startswith(year)):
            continue
        if (recorded_at and recorded_at != p["recorded_at"]) or (outcome and outcome != p["outcome"]):
            continue
        if element_key and (element_key not in p["elements"] or element_value and element_value != p["elements"][element_key]):
            continue
        if needs_review is not None and needs_review != p["needs_review"]:
            continue
        if not (_text(p["historical_name"], name) and _text(p["specification"], specification) and _text(p["material"], material)):
            continue
        if general and not any(_text(p[key], general) for key in ("historical_name", "historical_hs", "specification", "material")):
            continue
        summary = _history_row(bound, row)
        if success_only is None or success_only == summary["success_candidate"]:
            rows.append(summary)
    return _page(bound, _sort(rows, sort, HISTORY_SORTS), pagination, datasets=bound.dataset_items(), reference_only=True)


def history_detail(user, query, record_id):
    _require_kind(user, "history", detail=True)
    query.fields("snapshot_id", "revision", "reason")
    if not safe_key(record_id):
        raise INVALID()
    revision = query.get("revision")
    if revision and not re.fullmatch(r"[1-9][0-9]{0,8}", revision):
        raise INVALID()
    query.choice("reason", HISTORY_REASONS, "reference_review")
    bound = _pin(user, query, "history")
    row = bound.record(record_id)
    if revision and int(revision) != row.payload["revision"]:
        raise NOT_FOUND()
    return bound.envelope(record=_history_row(bound, row, detail=True), reference_only=True)


def evidence(user, query):
    if not authenticated(user):
        raise UNAUTHORIZED()
    if not (actor_can(user, VIEW_CATALOG) or actor_can(user, VIEW_HISTORY)):
        raise FORBIDDEN()
    kind = query.get("kind")
    _require_kind(user, kind, detail=True)
    query.fields("kind", "snapshot_id", "record_id", "anchor", "label", "reason")
    record_id, anchor = query.get("record_id"), query.get("anchor") or None
    if not safe_key(record_id) or (anchor is not None and not safe_key(anchor, ANCHOR)):
        raise INVALID()
    query.choice("reason", HISTORY_REASONS, "reference_review")
    bound = _pin(user, query, kind)
    row = bound.record(record_id)
    result = bound.evidence(row, anchor)
    label = query.get("label")
    if result is None or (label and result.reference.label != label):
        raise NOT_FOUND()
    record = _history_row(bound, row, detail=True) if kind == "history" else _catalog_row(bound, row, detail=True)
    section = next((part for part in record.get("sections", ()) if part["anchor"] == anchor), None)
    return bound.envelope(evidence_ref=result.reference.to_dict(), source_locator=result.source_locator,
                          record=record, **({"section": section} if section is not None else {}))


def history_export(user, query):
    _require_kind(user, "history", detail=True)
    raise FORBIDDEN()  # No export provider is shipped, even when a user can view detail.
