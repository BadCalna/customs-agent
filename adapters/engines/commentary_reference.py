"""P16's local, deterministic P23 material reader, NOT classification support.

Explicitly inject a ``source`` or a ``store`` (including EnvCommentaryStore).
``retrieve(candidate_codes=..., keys=..., keyword=..., expected_version=...)``
returns a JSON-ready Skill evidence packet. Candidate prefixes select reading
material only: they neither establish existence of that code nor applicability
of a note. No ClassificationNote is created, no network/DB/default sample used.

Only complete source sections are included within the shared body-character
budget. An omitted section is reported, never returned as a seemingly complete
substring. P23's already shortened EvidenceEntry.excerpt is deliberately unused.
Version verification checks an explicitly requested snapshot label only, NOT
policy effective dates or the applicability of commentary to a declaration.
"""
from __future__ import annotations

import re
from typing import Any, Protocol, Sequence

from adapters.sources.tariff_commentary import (
    CommentaryLookupResult, CommentaryStore, TariffCommentarySource,
)

PACKET_VERSION = "p16.commentary-reference.1"
_BOUNDARY = (
    "Retrieved commentary is reading material only; no candidate 10-digit code "
    "is established, validated or supported. Applicability requires separate expert assessment."
)
# HS sections I-XXI, expressed as inclusive chapter ranges. Chapter 77 is
# reserved; chapters 98/99 are national special provisions, not guessed here.
_SECTION_RANGES = (
    (1, 5), (6, 14), (15, 15), (16, 24), (25, 27), (28, 38),
    (39, 40), (41, 43), (44, 46), (47, 49), (50, 63), (64, 67),
    (68, 70), (71, 71), (72, 83), (84, 85), (86, 89), (90, 92),
    (93, 93), (94, 96), (97, 97),
)


class CommentarySource(Protocol):
    """The existing P23 interface; implementations must be local/read-only."""

    def lookup(self, query: Any) -> CommentaryLookupResult: ...


def _key(raw: str) -> tuple[str, str]:
    if not isinstance(raw, str):
        raise ValueError("commentary keys must be strings")
    key = raw.strip()
    if re.fullmatch(r"[0-9]{4}", key):
        return key, "heading"
    match = re.fullmatch(r"(CH|CA|OT)([0-9]+)", key.upper())
    if match:
        prefix, number = match.groups()
        n = int(number)
        if n > 0 and ((prefix == "CH" and n <= 99) or
                      (prefix == "CA" and n <= 21) or prefix == "OT"):
            return f"{'ot' if prefix == 'OT' else prefix}{n}", {
                "CH": "chapter_note", "CA": "section_note", "OT": "other_note",
            }[prefix]
    raise ValueError("only definite 4-digit/CHn/CAn/otn keys are accepted; never 8/10-digit keys")


def candidate_commentary_keys(code: str) -> list[str]:
    """Syntactic prefix mapping, never a validity or classification assertion."""
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{10}", code):
        raise ValueError("candidate code must be an explicit 10-digit ASCII string")
    chapter = int(code[:2])
    if chapter == 0:
        raise ValueError("candidate has no definite chapter key")
    keys = [code[:4], f"CH{chapter}"]
    if chapter != 77:
        for section, (start, end) in enumerate(_SECTION_RANGES, start=1):
            if start <= chapter <= end:
                keys.append(f"CA{section}")
                break
    return keys


def _status(queries: list[dict]) -> tuple[str, str | None]:
    for status in ("source_error", "source_unreadable", "source_unconfigured", "invalid_input"):
        if any(q["query_status"] == status for q in queries):
            return status, None if status == "invalid_input" else "query_failed"
    for status in ("query_failed", "version_unverified", "obtained"):
        if any(q["evidence_status"] == status for q in queries):
            return "ok", status
    return "ok", "not_found"


class CommentaryReferenceSource:
    """Explicitly assembled material source; never implements classification search."""

    def __init__(self, source: CommentarySource | None = None, *,
                 store: CommentaryStore | None = None, max_total_chars: int = 24000,
                 max_queries: int = 40, keyword_limit: int = 10):
        if (source is None) == (store is None):
            raise ValueError("inject exactly one commentary source or store; no implicit fallback")
        for name, value, ceiling in (("max_total_chars", max_total_chars, 100000),
                                     ("max_queries", max_queries, 200),
                                     ("keyword_limit", keyword_limit, 200)):
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError(f"{name} must be an integer between 1 and {ceiling}")
        self.source = source if source is not None else TariffCommentarySource(store)
        self.max_total_chars = max_total_chars
        self.max_queries = max_queries
        self.keyword_limit = keyword_limit

    def lookup(self, key: str, *, expected_version: str = "") -> dict:
        """Only explicit hierarchy keys; candidate codes belong to retrieve()."""
        return self.retrieve(keys=[key], expected_version=expected_version)

    def retrieve(self, candidate_codes: Sequence[str] = (), *, keys: Sequence[str] = (),
                 keyword: str = "", expected_version: str = "") -> dict:
        packet: dict[str, Any] = {
            "schema_version": PACKET_VERSION, "material_retrieved": False,
            "classification_support": False, "applicability": "not_assessed",
            "boundary": _BOUNDARY, "candidate_mappings": [], "queries": [],
            "records": [], "evidence": [], "datasets": [], "versions": [],
            "truncated": False,
            "limits": {"max_total_chars": self.max_total_chars,
                       "text_characters_included": 0, "text_characters_omitted": 0,
                       "max_queries": self.max_queries, "keyword_limit": self.keyword_limit,
                       "keyword_discovery_exhaustive": False,
                       "counting": "sum of included sections[].text characters; no duplicated excerpts",
                       "policy": "whole sections only; omissions are explicit"},
        }
        try:
            if isinstance(candidate_codes, (str, bytes)) or isinstance(keys, (str, bytes)):
                raise ValueError("candidate_codes and keys must be sequences, not scalar strings")
            if not isinstance(keyword, str) or len(keyword) > 500:
                raise ValueError("keyword must be a string of at most 500 characters")
            if not isinstance(expected_version, str) or len(expected_version) > 200:
                raise ValueError("expected_version must be an explicit snapshot label")
            requests = []
            for code in candidate_codes:
                mapped = candidate_commentary_keys(code)
                packet["candidate_mappings"].append({
                    "candidate_code": code, "keys": mapped, "relation": "prefix_material_only",
                    "classification_support": False,
                })
                requests.extend(mapped)
            requests.extend(_key(key)[0] for key in keys)
            requests = list(dict.fromkeys(requests))
            if not requests and not keyword.strip():
                raise ValueError("provide candidate codes, hierarchy keys or a discovery keyword")
            if len(requests) + bool(keyword.strip()) > self.max_queries:
                raise ValueError("query count exceeds explicit limit; no partial query was executed")
        except (ValueError, TypeError) as exc:
            packet["queries"] = [{"query_status": "invalid_input", "evidence_status": None,
                                  "reason": str(exc), "material_retrieved": False,
                                  "classification_support": False}]
            packet["query_status"], packet["evidence_status"] = "invalid_input", None
            return packet

        work = [(key, "hierarchy", key) for key in requests]
        if keyword.strip():
            work.append(({"keyword": keyword, "limit": self.keyword_limit}, "keyword_discovery", ""))
        seen = set()
        for query, mode, key in work:
            entry: dict[str, Any] = {"mode": mode, "key": key,
                                     "keyword": keyword if mode == "keyword_discovery" else "",
                                     "classification_support": False, "material_retrieved": False}
            try:
                raw = self.source.lookup(query)
                result = CommentaryLookupResult.model_validate(
                    raw.model_dump() if isinstance(raw, CommentaryLookupResult) else raw)
                query_status = result.query_status.value
                evidence_status = result.evidence_status.value if result.evidence_status else None
                # P23 handles store errors internally, embedding the original exception
                # in reason/issues. Never forward those untrusted diagnostics to Skill.
                reason = {
                    "source_unconfigured": "Commentary source is not configured; no fallback was used.",
                    "source_unreadable": "Commentary source is unreadable; not a not-found result.",
                    "source_error": "Commentary source failed; not a not-found result.",
                    "invalid_input": "Commentary source rejected the hierarchy query.",
                }.get(query_status, "Commentary material retrieved; applicability has not been assessed.")
                if query_status == "ok" and evidence_status == "not_found":
                    reason = "本注释快照未收录匹配资料；不等于该品目或章不存在注释。"
                entry.update(query_status=query_status, evidence_status=evidence_status,
                             reason=reason, issues=[])
                if query_status != "ok":
                    entry["issues"] = [{"code": f"COMMENTARY_{query_status.upper()}",
                                        "message": reason, "field": "commentary_source",
                                        "suggested_role": "operator"}]
                    packet["queries"].append(entry)
                    continue
                entry["version"] = result.version
                verified = bool(expected_version and result.version == expected_version)
                entry["version_check"] = {
                    "expected": expected_version, "actual": result.version,
                    "status": "verified" if verified else "unverified",
                    "scope": "snapshot_label_only; not policy dates or candidate applicability",
                }
                if not result.records and not verified:
                    entry["evidence_status"] = "version_unverified"
                    entry["reason"] = "注释快照版本未核实；本次未取到资料，不等于目标版本没有注释。"
                dataset = None
                if result.dataset is not None:
                    d = result.dataset
                    # In particular do not pass reviewer identity to a model.
                    authoritative = bool(d.authoritative and d.synthetic is False)
                    dataset = {"dataset_id": d.dataset_id, "source_type": d.source_type.value,
                               "authority": d.authority.value, "authoritative": authoritative,
                               "review_status": d.review_status.value, "synthetic": d.synthetic,
                               "verification_status": "verified" if authoritative else "unverified",
                               "verification_reason": (
                                   "real_source_reviewed" if authoritative else
                                   "synthetic_status_undeclared" if d.synthetic is None else
                                   "synthetic_material" if d.synthetic is True else
                                   "authority_or_review_unverified")}
                # Validate the entire response BEFORE including any of its text.
                prepared = []
                for record in result.records:
                    canonical, level = _key(record.tariff_no)
                    if canonical != record.tariff_no or record.level is None or record.level.value != level:
                        raise ValueError("source record hierarchy conflicts with its definite key")
                    if key and canonical != key:
                        raise ValueError("source returned a different hierarchy key")
                    evidence = [item for item in result.evidence
                                if item.query.get("tariff_no") == canonical]
                    if len(evidence) != 1 or not evidence[0].evidence_id.strip():
                        raise ValueError("source record lacks unique locatable evidence")
                    ev = evidence[0]
                    if ev.status.value not in ("obtained", "version_unverified"):
                        raise ValueError("retrieved material carries failed or missing evidence")
                    if not ev.document or not ev.locator or ev.document != record.source.document or ev.locator != record.source.locator:
                        raise ValueError("source evidence and record location disagree")
                    status = ("obtained" if verified and result.evidence_status.value == "obtained"
                              and ev.status.value == "obtained" else "version_unverified")
                    prepared.append((record, level, ev, status))
                if prepared and dataset is None:
                    raise ValueError("source records lack dataset provenance")
                if prepared:
                    entry["material_retrieved"] = True
                    entry["evidence_status"] = ("version_unverified" if any(p[3] == "version_unverified" for p in prepared)
                                                else "obtained")
                if dataset is not None and dataset not in packet["datasets"]:
                    packet["datasets"].append(dataset)
                if result.version and result.version not in packet["versions"]:
                    packet["versions"].append(result.version)
                for record, level, ev, status in prepared:
                    identity = (result.version, record.tariff_no)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    included, omitted = [], []
                    for section in record.sections:
                        remaining = self.max_total_chars - packet["limits"]["text_characters_included"]
                        if len(section.text) <= remaining:
                            included.append(section.model_dump(mode="json"))
                            packet["limits"]["text_characters_included"] += len(section.text)
                        else:
                            omitted.append({"label": section.label, "anchor": section.anchor,
                                            "text_length": len(section.text), "reason": "total_text_limit"})
                            packet["limits"]["text_characters_omitted"] += len(section.text)
                    evidence_packet = {
                        "evidence_id": ev.evidence_id, "document": ev.document, "locator": ev.locator,
                        "url": ev.url, "retrieved_at": ev.retrieved_at, "version": result.version,
                        "status": status, "source_status": ev.status.value,
                        "level": level, "key": record.tariff_no, "dataset": dataset,
                        "material_retrieved": True, "classification_support": False,
                        "truncated": bool(omitted),
                    }
                    packet["evidence"].append(evidence_packet)
                    packet["records"].append({
                        "key": record.tariff_no, "level": level, "kind": record.kind,
                        "sections": included, "omitted_sections": omitted,
                        "text_sha256": record.text_sha256, "original_text_length": record.text_length,
                        "original_section_char_total": record.section_char_total,
                        "evidence_id": ev.evidence_id, "version": result.version, "status": status,
                        "document": ev.document, "locator": ev.locator, "dataset": dataset,
                        "material_retrieved": True, "classification_support": False,
                        "truncated": bool(omitted), "complete": not omitted,
                    })
                    packet["truncated"] = packet["truncated"] or bool(omitted)
            except Exception:
                # Neither exception messages nor custom exception class names are safe.
                entry.update(query_status="source_error", evidence_status="query_failed",
                             reason="Commentary source response failed; not a not-found result.",
                             issues=[{"code": "COMMENTARY_SOURCE_ERROR",
                                      "message": "Commentary source response failed; not a not-found result.",
                                      "field": "commentary_source", "suggested_role": "operator"}],
                             material_retrieved=False)
            packet["queries"].append(entry)
        packet["material_retrieved"] = any(q["material_retrieved"] for q in packet["queries"])
        packet["query_status"], packet["evidence_status"] = _status(packet["queries"])
        return packet


__all__ = ["CommentarySource", "CommentaryReferenceSource", "candidate_commentary_keys", "PACKET_VERSION"]
