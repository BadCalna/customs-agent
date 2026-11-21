"""Read-only, text-only Skill precheck. No factory/settings/dotenv/DB wiring.

Inject a pinned ReferenceBundle and a synchronous model.complete(dict). The
optional CommentaryReferenceSource is material retrieval, never ClassificationNote.
Build_request is a dry run: it calls only injected read-only catalog/P14/commentary
sources, not model/history. Optional explicit candidate_codes must be independently
selected, never supplier/history HS. History is read only after model validation.
Original-code comparison remains inherited and outside all model requests.

The input guard is deliberately conservative (10-digit strings in free facts
are refused); it is not a general-purpose secret detector. Historical business
locations/record IDs are anonymous handles in model packets, locally resolvable
against the caller-owned source, never raw business identifiers.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from copy import deepcopy
from pathlib import Path

from apps.api.schemas import EvidenceEntry, EvidenceStatus
from adapters.sources.declaration_elements import DeclarationElementsResult, ElementQuery, Sufficiency
from adapters.sources.history_search import HistoryQuery, HistorySearchResult
from adapters.sources.hs_lookup import HsLookupResult, HsQuery, QueryBasis, _parse_date
from .commentary_reference import CommentaryReferenceSource
from .mock import EngineTechnicalError
from .precheck import (
    ANCHORING_NOTICE, FACT_FIELDS, ModelRecommendation, PrecheckEngine,
    _PRIVATE, _issue, _safe_text, _strict_json, _worst_status,
)
from .protocols import Candidate, RecommendInput, RecommendOutput
from .reference_bundle import ReferenceBundle

ENGINE_VERSION = "skill-precheck-injected-0.1"
PACKET_VERSION = "p16.skill-precheck.1"
DEFAULT_SKILL_PATH = Path(__file__).resolve().parents[2] / "docs/skills/steel-hs-declaration/SKILL.md"
_PROMPT_PATH = Path(__file__).resolve().parent / "prompts/precheck/skill_adapter.md"
_FACT_CODE = re.compile(r"(?<![0-9])[0-9](?:[\s.\-]*[0-9]){9,12}(?![0-9])")
_PRIVATE_KEY = re.compile(r"(?:secret|password|token|api.?key|supplier.*hs|原码|原编码|海关编码|(?:^|_)id$|input_hash)", re.I)
# Conservative fact-only guard: never try to distinguish a filesystem path from
# a slash-delimited product string. Reject separators (including common Unicode
# variants), so POSIX roots, drive paths, UNC paths and URLs cannot enter facts.
# Source excerpts are checked separately and may legitimately contain slashes.
_FACT_PATH_SEPARATOR = re.compile(r"[\\/／＼∕⁄]")


def _safe_fact_text(value, hidden):
    text = _safe_text(value, hidden)
    if _FACT_PATH_SEPARATOR.search(text):
        raise ValueError("path-like fact")
    return text


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _dataset(value) -> dict:
    if value is None:
        return {"synthetic": None, "review_status": "unknown"}
    data = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    return {key: deepcopy(data[key]) for key in (
        "dataset_id", "source_type", "synthetic", "review_status", "authoritative", "authority",
    ) if key in data} | {"review_status": data.get("review_status", "unknown")}


def _section_rules(text: str, title: str, count: int) -> dict[int, str]:
    """Read one known, flat numbered section; do not guess Markdown boundaries."""
    parts = re.split(r"(?m)^ {0,3}##[ \t]+([^\n]*)$", text)
    ordinal = title.partition("、")[0] + "、"
    matches = [(heading, body) for heading, body in zip(parts[1::2], parts[2::2])
               if heading.strip().startswith(ordinal)]
    if len(matches) != 1 or matches[0][0].strip() != title:
        raise ValueError("missing or ambiguous section")

    rules = {}
    for line in matches[0][1].splitlines():
        if not line.strip():
            continue
        number = len(rules) + 1
        if line.startswith(f"{number}. ") and line.partition(". ")[2].strip():
            rules[number] = line
        elif rules and re.fullmatch(r" {3}(?![\s\d#`~])\S.*", line):
            # The checked-in Skill continues rule 7 with indented prose.
            rules[number - 1] += "\n" + line
        else:
            raise ValueError("unexpected rule boundary or numbering")
    if len(rules) != count:
        raise ValueError("unexpected section length")
    return rules


def _load_skill(path: Path) -> dict:
    try:
        raw = path.read_bytes()
        if len(raw) > 200000:
            raise ValueError("oversized")
        text = raw.decode("utf-8").replace("\r\n", "\n")
        # Fail closed on unfamiliar layouts rather than forwarding the whole Skill,
        # its front matter, expert workflow, images, Excel operations or references.
        common = _section_rules(text, "一、确认商品及两个页签的关联", 7)
        precheck = _section_rules(text, "二、业务员预审：独立推荐HS并比较", 9)
        picked = [common[2], common[3]]
        for number in (1, 2, 4, 7):
            rule = precheck[number]
            if number == 2:
                # Keep source priority/provenance, not local workbook or website
                # execution instructions. Runtime uses supplied packets only.
                marker = "读取`references`"
                if rule.count(marker) != 1:
                    raise ValueError("missing or ambiguous source boundary")
                rule = rule.partition(marker)[0] + (
                    "仅消费 supplied reference_packet，不读取本地Excel、不上网。"
                    "历史成功记录和映射快照仅作佐证，不单独证明本批归类或当期税率；"
                    "历史HS仅在独立判断完成后由模型外程序核对。记录实际来源、日期、版本与查询状态。")
            elif number == 7:
                marker = "在映射底表`申报要素明细`取完整字段及必填标记"
                if rule.count(marker) != 1:
                    raise ValueError("missing or ambiguous elements boundary")
                rule = rule.replace(marker, "仅从supplied P14 packet取得完整字段及必填标记")
            picked.append(rule)
        rules = "\n\n".join(picked)
        return {"name": "steel-hs-declaration", "stage": "precheck",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "selected_rules_sha256": _hash(rules), "selection_version": "text-candidates-packet-only-3",
                "rules": rules}
    except Exception:
        raise EngineTechnicalError("预审Skill读取或阶段规则校验失败") from None


class SkillPrecheckAdapter(PrecheckEngine):
    """Standalone adapter; inherited compare/run_precheck do not feed originals back.

    No candidate is silently seeded from history or supplier data. Explicit
    candidate_codes in build_request are caller-selected independent hypotheses;
    ordinary recommend first asks the model, then verifies its candidates via P14.
    """

    engine_version = ENGINE_VERSION
    prompt_version = "skill-adapter-json-1"
    rules_version = "skill-precheck-text-1"

    def __init__(self, *, model, reference_bundle: ReferenceBundle,
                 commentary_source: CommentaryReferenceSource | None = None, skill_path=None):
        if not isinstance(reference_bundle, ReferenceBundle):
            raise EngineTechnicalError("预审需要显式ReferenceBundle")
        if commentary_source is not None and not isinstance(commentary_source, CommentaryReferenceSource):
            raise EngineTechnicalError("预审注释来源契约无效")
        self.bundle = reference_bundle
        self.model = model
        self.hs_source = reference_bundle.hs_source
        self.elements_source = reference_bundle.elements_source
        self.history_source = reference_bundle.history_source
        self.commentary_source = commentary_source
        if self.commentary_source is None and reference_bundle.commentary_source is not None:
            self.commentary_source = CommentaryReferenceSource(source=reference_bundle.commentary_source)
        self.skill = _load_skill(Path(skill_path) if skill_path is not None else DEFAULT_SKILL_PATH)
        try:
            self._prompt = _PROMPT_PATH.read_text(encoding="utf-8")
        except Exception:
            raise EngineTechnicalError("预审提示词读取失败") from None

    def _input(self, payload):
        try:
            snapshot = RecommendInput.model_validate(payload.model_dump()).model_copy(deep=True)
            context = snapshot.context.model_dump()
            hidden = [v for k, v in context.items() if isinstance(v, str) and v
                      and (k.endswith("_id") or k == "input_hash")]
            for mapping in (snapshot.attributes, snapshot.query_basis):
                hidden.extend(v for k, v in mapping.items()
                              if _PRIVATE_KEY.search(k) and isinstance(v, str) and v)
            hidden = tuple(hidden)
            attributes = {k: _safe_fact_text(v, hidden) for k, v in snapshot.attributes.items() if k in FACT_FIELDS}
            facts = {"name": _safe_fact_text(snapshot.name, hidden), "attributes": attributes,
                     "elements_text": _safe_fact_text(snapshot.elements_text, hidden),
                     "images": "not_connected", "anchoring_risk": ANCHORING_NOTICE}
            if any(_FACT_CODE.search(v) for v in (facts["name"], facts["elements_text"], *attributes.values())):
                raise ValueError("code in facts")
            # Free-form basis note is not needed by the model or source queries.
            basis = QueryBasis(**{k: _safe_fact_text(snapshot.query_basis[k], hidden)
                                  for k in ("declaration_date", "tariff_version") if k in snapshot.query_basis})
            if (basis.declaration_date and _parse_date(basis.declaration_date) is None
                    or basis.tariff_version and _FACT_CODE.search(basis.tariff_version)):
                raise ValueError("non-basis content")
            return snapshot, facts, basis, hidden
        except Exception:
            raise EngineTechnicalError("预审输入包含不允许的敏感内容或契约无效") from None

    def _check_text(self, value, hidden):
        # Inspect every selected provenance/text field, not arbitrary full source
        # objects (which can include reviewer names, paths, query echoes or IDs).
        if isinstance(value, str):
            # Material budget is independent from the 10k fact guard. Inspect
            # the WHOLE string, not chunks, so sensitive markers cannot straddle
            # a chunk boundary. Commentary source already omits whole sections
            # over its total budget; this is a second bounded-input guard.
            limit = max(24000, self.commentary_source.max_total_chars) if self.commentary_source else 24000
            if (len(value) > limit or _PRIVATE.search(value)
                    or any(secret in value for secret in hidden)
                    or value.lstrip().startswith(("{", "["))):
                raise EngineTechnicalError("预审参考材料包含不允许的敏感或超限内容")
        elif isinstance(value, dict):
            for key, item in value.items():
                _safe_text(key, hidden)
                self._check_text(item, hidden)
        elif isinstance(value, list):
            for item in value:
                self._check_text(item, hidden)

    def _sanitize_path(self, path: str) -> str:
        # Detect POSIX paths, UNC paths, and Windows absolute paths
        if re.search(r"(?:/|\\)(?:[\w.-]+(?:/|\\)+)+[\w.-]*|^(?:[A-Za-z]:\\|\\\\|/)", path, re.IGNORECASE):
            return "anonymous_reference"
        return path

    def _entry(self, entry, dataset, version, *, scope):
        return {"evidence_id": entry.evidence_id, "source_type": entry.source_type,
                "document": self._sanitize_path(entry.document),
                "locator": self._sanitize_path(entry.locator),
                "excerpt": entry.excerpt, "retrieved_at": entry.retrieved_at,
                "effective_from": entry.effective_from, "effective_to": entry.effective_to,
                "version": version, "status": entry.status.value, "dataset": _dataset(dataset),
                "classification_support": False, "scope": scope}

    def _p14(self, code, facts, basis):
        packet = {"candidate_code": code, "classification_support": False}
        entries = []
        for kind, source, query, cls in (
            ("hs", self.hs_source, HsQuery(hs_code_raw=code, basis=basis.model_copy(deep=True)), HsLookupResult),
            ("elements", self.elements_source, ElementQuery(hs_code_raw=code, basis=basis.model_copy(deep=True),
                                                          declared_raw=facts["elements_text"]), DeclarationElementsResult),
        ):
            try:
                raw = source.lookup(query) if kind == "hs" else source.check(query)
                result = cls.model_validate(raw.model_dump())
                if result.query is None or result.query.model_dump() != query.model_dump():
                    raise ValueError("query mismatch")
                record = result.code if kind == "hs" else result.schema_record
                if record is not None and ((record.code if kind == "hs" else record.hs_code) != code
                                           or record.version != result.version.version):
                    raise ValueError("record mismatch")
                status = result.evidence_status if kind == "hs" else result.schema_status
                item = {"status": status.value if status else None,
                        "query_status": result.query_status.value,
                        "version": result.version.version, "version_verified": result.version.verified,
                        "dataset": _dataset(result.dataset), "authoritative": result.authoritative,
                        "classification_support": False}
                if kind == "hs":
                    item.update(declaration_ready=result.declaration_ready,
                                description=record.description if record else "")
                else:
                    item.update(sufficiency=result.sufficiency.value,
                                checks=[{k: c.model_dump(mode="json")[k] for k in
                                         ("key", "label", "requirement", "state", "declared_value")}
                                        for c in result.checks])
                evidence = [self._entry(e, result.dataset, result.version.version, scope=kind) for e in result.evidence]
                item["evidence_ids"] = [e["evidence_id"] for e in evidence]
                entries.extend(evidence)
                packet[kind] = item
            except Exception:
                packet[kind] = {"status": "query_failed", "query_status": "source_error",
                                "classification_support": False, "evidence_ids": [],
                                "reason": "P14来源调用或响应校验失败；不等于查无或资料不足"}
        return packet, entries

    @staticmethod
    def _tokens(text):
        text = unicodedata.normalize("NFKC", text).casefold()
        tokens = set()
        for run in re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+", text):
            if re.fullmatch(r"[\u3400-\u9fff]+", run):
                tokens.update(run[i:i + 2] for i in range(len(run) - 1))
            elif len(run) >= 2 and not run.isdigit():
                tokens.add(run)
        return tokens

    def _catalog_recall(self, facts):
        packet = {"status": "no_match", "algorithm_version": "name-alias-token-overlap-1",
                  "retrieval_only": True, "classification_support": False, "exhaustive": False,
                  "version": self.bundle.version, "limit": 5, "truncated": False,
                  "scanned_count": 0, "matched_count": 0, "omitted_count": 0, "records": []}
        try:
            attrs = facts["attributes"]
            fields = {"primary_name": attrs.get("primary_name") or facts["name"],
                      "alias": attrs.get("alias", ""),
                      "specification": " ".join(attrs.get(k, "") for k in (
                          "specification", "规格型号", "manufacturer_model", "model", "型号"))}
            tokens = {k: self._tokens(v) for k, v in fields.items()}
            snapshot = self.bundle.tariff_store.load()
            if snapshot.dataset.synthetic is not False:
                packet["status"] = "origin_unverified"
                return packet
            packet["dataset"] = _dataset(snapshot.dataset)
            matches = []
            for record in snapshot.codes:
                packet["scanned_count"] += 1
                if record.version != self.bundle.version or not re.fullmatch(r"[0-9]{10}", record.code):
                    continue
                description_tokens = self._tokens(record.description)
                hits = {k: sorted(v & description_tokens) for k, v in tokens.items()}
                # Specification/model numbers alone never open a retrieval branch.
                if not (hits["primary_name"] or hits["alias"]):
                    continue
                score = 4 * len(hits["primary_name"]) + 3 * len(hits["alias"]) + len(hits["specification"])
                matches.append({"code": record.code, "description": record.description,
                                "version": record.version, "matched_tokens": hits,
                                "retrieval_score": score, "retrieval_only": True,
                                "classification_support": False})
            matches.sort(key=lambda r: (-r["retrieval_score"], r["code"], r["version"]))
            # Bundle source parser guarantees (version, code) uniqueness.
            packet.update(status="matched" if matches else "no_match", matched_count=len(matches),
                          omitted_count=max(0, len(matches) - 5), truncated=len(matches) > 5,
                          records=matches[:5])
            return packet
        except Exception:
            packet.update(status="query_failed", records=[], matched_count=0,
                          reason="固定目录召回失败；不等于没有适用编码，候选仍需独立判断")
            return packet

    def _history(self, facts):
        if self.history_source is None:
            return {"status": "not_configured", "reference_only": True, "records": []}, []
        try:
            raw = self.history_source.search(HistoryQuery(
                name=facts["name"], attributes=deepcopy(facts["attributes"]),
                material=facts["attributes"].get("material", facts["attributes"].get("材质", "")),
                spec=facts["attributes"].get("specification", facts["attributes"].get("规格型号", ""))))
            result = HistorySearchResult.model_validate(raw.model_dump())
            if result.dataset is None or result.dataset.synthetic is not False:
                return {"status": "origin_unverified", "reference_only": True, "records": []}, []
            records, entries = [], []
            for candidate in result.candidates:
                if candidate.needs_review:
                    continue
                # Business record/document IDs are never model-visible, even
                # through evidence_id or source locator; hashes are lookup handles.
                identity = _hash(candidate.record_id)
                entry = {"evidence_id": "history-ref:" + identity,
                         "source_type": "history", "document": "history-document:" + _hash(candidate.source.document),
                         "locator": "history-location:" + _hash(candidate.source.locator),
                         "location_kind": "anonymous_local_reference",
                         "version": candidate.hs_version, "status": "version_unverified",
                         "dataset": _dataset(result.dataset), "classification_support": False}
                entries.append(entry)
                records.append({"evidence_id": entry["evidence_id"], "historical_hs": candidate.historical_hs,
                                "historical_name": candidate.historical_name,
                                "matched_keys": candidate.matched_keys, "diff_keys": candidate.diff_keys,
                                "unverified_keys": candidate.unverified_keys,
                                "key_attribute_conflict": candidate.key_attribute_conflict,
                                "version": candidate.hs_version, "status": "version_unverified",
                                "reference_only": True, "classification_support": False})
            return {"status": result.evidence_status.value if result.evidence_status else None,
                    "reference_only": True, "dataset": _dataset(result.dataset), "records": records}, entries
        except Exception:
            return {"status": "query_failed", "reference_only": True, "records": [],
                    "reason": "历史来源调用或响应校验失败；不等于未匹配"}, []

    def build_request(self, payload: RecommendInput, *, candidate_codes=None, commentary_keys=(),
                      commentary_keyword=None) -> dict:
        """Detached supplied-packet request; never model/history/compare/persistence.

        None recalls up to five catalog hypotheses by name/alias; () disables
        recall; explicit codes must be supplier/history-independent hypotheses.
        Recall never limits the model's candidate set or establishes support.
        """
        _, facts, basis, hidden = self._input(payload)
        try:
            if candidate_codes is None:
                recall = self._catalog_recall(facts)
                codes = [r["code"] for r in recall["records"]]
            else:
                if isinstance(candidate_codes, (str, bytes)) or len(candidate_codes) > 5:
                    raise ValueError("codes")
                codes = list(dict.fromkeys(candidate_codes))
                recall = {"status": "explicit_hypotheses" if codes else "disabled", "records": [],
                          "retrieval_only": True, "classification_support": False, "exhaustive": False,
                          "version": self.bundle.version, "truncated": False}
            if any(not isinstance(c, str) or not re.fullmatch(r"[0-9]{10}", c) for c in codes):
                raise ValueError("codes")
            history = {"status": "deferred_until_after_independent_recommendation",
                       "reference_only": True, "records": []}
            entries = []
            p14 = []
            for code in codes:
                item, evidence = self._p14(code, facts, basis)
                p14.append(item)
                entries.extend(evidence)
            commentary = {"status": "not_configured", "classification_support": False,
                          "applicability": "not_assessed", "records": [], "evidence": []}
            if self.commentary_source is not None:
                keyword = facts["name"][:200] if commentary_keyword is None else _safe_fact_text(commentary_keyword, hidden)
                metadata = self.bundle.source_metadata.get("tariff_commentary", {})
                raw = self.commentary_source.retrieve(candidate_codes=codes, keys=deepcopy(commentary_keys),
                                                     keyword=keyword, expected_version=metadata.get("version", ""))
                # No exception reasons, URLs, source query echoes, reviewer IDs.
                commentary = {k: deepcopy(raw[k]) for k in (
                    "query_status", "evidence_status", "material_retrieved", "classification_support",
                    "applicability", "records", "evidence", "truncated", "limits", "versions",
                ) if k in raw}
                # Re-render diagnostics from status, never include arbitrary source
                # exception text. Preserve no-hit version uncertainty explicitly.
                commentary["queries"] = [{k: deepcopy(q[k]) for k in (
                    "mode", "key", "query_status", "evidence_status", "version", "version_check",
                    "material_retrieved", "classification_support",
                ) if k in q} | {"reason": (
                    "注释来源查询失败，不等于查无资料" if q.get("evidence_status") == "query_failed" else
                    "注释快照版本未核实；未取到资料不等于目标版本没有注释" if q.get("evidence_status") == "version_unverified" else
                    "本注释快照未收录匹配资料，不等于不存在注释" if q.get("evidence_status") == "not_found" else
                    "取得注释阅读材料；候选适用性未审核"
                )} for q in raw.get("queries", [])]
                commentary["classification_support"] = False
                for collection in ("records", "evidence"):
                    for item in commentary.get(collection, []):
                        item.pop("url", None)
                        item["classification_support"] = False
                        item["source_type"] = item.get("dataset", {}).get("source_type", "official_tariff")
                        if "document" in item:
                            item["document"] = self._sanitize_path(item["document"])
                        if "locator" in item:
                            item["locator"] = self._sanitize_path(item["locator"])
                entries.extend(deepcopy(commentary.get("evidence", [])))
            index = {}
            for entry in entries:
                key = entry["evidence_id"]
                if key in index and index[key] != entry:
                    raise ValueError("duplicate evidence")
                index[key] = entry
            packet = {"schema_version": PACKET_VERSION, "stage": "precheck",
                      "images": "not_connected", "expert_review": "not_executed",
                      "bundle": {"version": self.bundle.version, "fingerprint": self.bundle.fingerprint},
                      "query_basis": basis.model_dump(mode="json"),
                      "catalog_recall": recall, "p14_status": "queried" if codes else "not_queried",
                      "p14": p14, "history": history, "commentary": commentary,
                      "evidence_index": list(index.values())}
            self._check_text({"facts": facts, "reference_packet": packet}, hidden)
            return {"system": self._prompt, "stage": "precheck", "skill": deepcopy(self.skill),
                    "facts": facts, "reference_packet": packet,
                    "response_schema": ModelRecommendation.model_json_schema()}
        except Exception:
            raise EngineTechnicalError("预审参考输入装配失败") from None

    def recommend(self, payload: RecommendInput) -> RecommendOutput:
        snapshot, _, _, _ = self._input(payload)
        request = self.build_request(snapshot)
        try:
            raw = self.model.complete(deepcopy(request))
        except Exception:
            raise EngineTechnicalError("预审模型调用失败") from None
        answer = _strict_json(raw)
        index = {e["evidence_id"]: e for e in request["reference_packet"]["evidence_index"]}
        for candidate in answer.candidates:
            if any(ref not in index for ref in candidate.evidence_ids):
                raise EngineTechnicalError("模型引用了未知归类材料")
        _, facts, basis, hidden = self._input(snapshot)
        gaps = [_issue("IMAGES_NOT_CONNECTED", "图像未接入，本次仅使用文本事实", field="images"),
                _issue("EXPERT_REVIEW_NOT_EXECUTED", "专家审核未执行；AI辅助结果待人工审核"),
                _issue("TAX_REGULATORY_NOT_CHECKED", "退税及监管核验未接入本适配器，不得视为已核")]
        evidence, statuses = [], []
        packet = request["reference_packet"]
        history, history_entries = self._history(facts)
        try:
            self._check_text({"history": history, "evidence": history_entries}, hidden)
        except Exception:
            raise EngineTechnicalError("预审参考输入装配失败") from None
        history_status = history["status"]
        recall_status = packet["catalog_recall"]["status"]
        if recall_status in ("query_failed", "origin_unverified"):
            statuses.append(EvidenceStatus.QUERY_FAILED if recall_status == "query_failed" else EvidenceStatus.VERSION_UNVERIFIED)
            gaps.append(_issue("CATALOG_RECALL_PENDING", "目录召回失败或来源待核；不等于没有适用编码", role="operator"))
        for retrieved in packet["p14"]:
            for kind, prefix in (("hs", "HS"), ("elements", "ELEMENTS")):
                status = retrieved[kind].get("status")
                if status in {s.value for s in EvidenceStatus}:
                    statuses.append(EvidenceStatus(status))
                if status == "query_failed":
                    gaps.append(_issue(prefix + "_QUERY_FAILED", "P14来源查询失败，不等于查无或供应商缺料", role="operator"))
                elif status in ("version_unverified", "not_found"):
                    gaps.append(_issue(prefix + "_BASIS_PENDING", "目录召回编码或要素的版本/资料待核"))
        for record in history.get("records", []):
            gaps.append(_issue("HISTORY_CROSSCHECK_PENDING",
                               "独立判断后取得历史参考；编码" + ("相同" if record["historical_hs"] == answer.suggested_hs else "不同或未定码")
                               + "不证明当前归类正确；差异字段：" + ("、".join(record["diff_keys"]) or "未列差异")
                               + "；关键属性冲突：" + ("有" if record["key_attribute_conflict"] else "未标记")
                               + "；未核字段：" + ("、".join(record["unverified_keys"]) or "未列")
                               + "；历史版本及适用性仍待核"))
        commentary_status = packet["commentary"].get("evidence_status")
        if history_status == "query_failed":
            statuses.append(EvidenceStatus.QUERY_FAILED)
        if commentary_status in {s.value for s in EvidenceStatus}:
            statuses.append(EvidenceStatus(commentary_status))
        if commentary_status in ("query_failed", "version_unverified"):
            gaps.append(_issue("COMMENTARY_QUERY_FAILED" if commentary_status == "query_failed" else "COMMENTARY_VERSION_PENDING",
                               "注释来源查询失败或快照版本待核；不等于没有注释或归类支持", role="operator"))
        if history_status in ("query_failed", "origin_unverified"):
            gaps.append(_issue("HISTORY_QUERY_FAILED" if history_status == "query_failed" else "HISTORY_ORIGIN_UNVERIFIED",
                               "历史来源失败或来源待核，不等于未匹配", role="operator"))
        for entry in [*index.values(), *history_entries]:
            if entry.get("status") in {s.value for s in EvidenceStatus}:
                statuses.append(EvidenceStatus(entry["status"]))
                evidence.append(EvidenceEntry(
                    evidence_id=entry["evidence_id"], source_type=entry["source_type"],
                    title="P14候选后验核验" if entry.get("scope") in ("hs", "elements") else "预审参考材料（非候选归类支持）",
                    status=EvidenceStatus(entry["status"]),
                    document=entry.get("document"), locator=entry.get("locator"), excerpt=entry.get("excerpt"),
                    retrieved_at=entry.get("retrieved_at"), effective_from=entry.get("effective_from"),
                    effective_to=entry.get("effective_to")))
        candidates = []
        for candidate in answer.candidates:
            p14, entries = self._p14(candidate.hs, facts, basis)
            try:
                self._check_text({"p14": p14, "evidence": entries}, hidden)
            except Exception:
                raise EngineTechnicalError("预审参考输入装配失败") from None
            for kind, prefix in (("hs", "HS"), ("elements", "ELEMENTS")):
                item = p14[kind]
                if item.get("status") in {s.value for s in EvidenceStatus}:
                    statuses.append(EvidenceStatus(item["status"]))
                if item.get("status") == "query_failed":
                    gaps.append(_issue(prefix + "_QUERY_FAILED", "P14来源查询失败，不等于查无或供应商缺料", role="operator"))
                real = item.get("dataset", {}).get("synthetic") is False
                ready = item.get("declaration_ready") if kind == "hs" else item.get("sufficiency") == Sufficiency.SUFFICIENT.value
                if not (real and ready and item.get("authoritative") and item.get("version_verified")):
                    gaps.append(_issue(prefix + "_BASIS_PENDING", "候选编码/要素的真实来源、权威、版本或充分性待核"))
            evidence.extend(EvidenceEntry(
                evidence_id=e["evidence_id"], source_type=e["source_type"], title="P14候选后验核验",
                status=EvidenceStatus(e["status"]), document=e["document"], locator=e["locator"],
                excerpt=e["excerpt"], retrieved_at=e["retrieved_at"], effective_from=e["effective_from"],
                effective_to=e["effective_to"]) for e in entries)
            gaps.append(_issue("CLASSIFICATION_SUPPORT_PENDING", "注释仅为阅读材料，未执行专家适用性审核，候选10位归类支持待核", field="candidates"))
            candidates.append(Candidate(hs=candidate.hs, reason="文本独立候选；P14后验核验与归类待核事项另列", confidence=None))
        if not candidates:
            gaps.append(_issue("NO_INDEPENDENT_CANDIDATE", "未形成独立候选，需专家核查"))
        elif answer.suggested_hs is None:
            gaps.append(_issue("CANDIDATE_UNDECIDED", "独立候选未确定，需专家核查"))
        gaps.extend(_issue("MODEL_FACTS_PENDING", "待核事实需人工确认，不自动派发补料", field=f) for f in answer.missing_facts)
        unique = {}
        for entry in evidence:
            if entry.evidence_id in unique and unique[entry.evidence_id] != entry:
                raise EngineTechnicalError("预审来源证据标识冲突")
            unique[entry.evidence_id] = entry
        return RecommendOutput(candidates=candidates, suggested_hs=answer.suggested_hs, confidence=None,
                               confidence_note="未经真实评测与校准，不提供概率。" + ANCHORING_NOTICE,
                               gaps=list({json.dumps(g.model_dump(), sort_keys=True): g for g in gaps}.values()),
                               evidence=list(unique.values()), evidence_status=_worst_status(statuses))


__all__ = ["SkillPrecheckAdapter", "DEFAULT_SKILL_PATH", "PACKET_VERSION", "ENGINE_VERSION"]
