"""Atomic P15 snapshot ingestion. Never touches the operational review tables.

The explicit allow_sqlite escape hatch is for isolated unit tests only; the command
never exposes it. Input is fully validated before a dataset/batch is written.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from django.db import connections, transaction

from adapters.sources.history_consolidated import CONSOLIDATED_IDENTITY_VERSION, FORMAT_CONSOLIDATED
from adapters.sources.history_import import (
    _business_fingerprint, _enforce_stored_hs_review,
    _merge_consolidated_records, _merge_records,
)
from adapters.sources.history_search import (
    SCHEMA_VERSION, HistoryStoreError, _blocking_issue_codes, _needs_review,
    _snapshot_from_payload,
)


class HistoryImportError(ValueError):
    """Invalid or incompatible input: no part of this batch was committed."""


SOURCE_KINDS = {
    "historical-template-import": False,
    "historical-consolidated-import": False,
    "synthetic-history-template": True,
    "synthetic-historical-consolidated": True,
}


def _validate_payload(payload, *, expected_count=None, require_real=False):
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise HistoryImportError("必须提供受支持版本的 P15 对象快照")
    metadata = payload.get("dataset")
    if not isinstance(metadata, dict) or type(metadata.get("synthetic")) is not bool:
        raise HistoryImportError("dataset.synthetic 必须明确声明 true/false，不接受未知来源")
    if not isinstance(metadata.get("dataset_id"), str) or not metadata["dataset_id"].strip():
        raise HistoryImportError("dataset.dataset_id 必须明确声明")
    synthetic = metadata["synthetic"]
    if require_real and synthetic:
        raise HistoryImportError("require_real 拒绝合成数据集")
    try:
        snapshot = _snapshot_from_payload(payload, source_name="import")
    except HistoryStoreError as exc:
        raise HistoryImportError(str(exc)) from exc
    if expected_count is not None and (type(expected_count) is not int or expected_count < 0 or len(snapshot.records) != expected_count):
        raise HistoryImportError("expected_count 与输入记录数不一致")
    if "record_count" in metadata and (type(metadata["record_count"]) is not int or metadata["record_count"] != len(snapshot.records)):
        raise HistoryImportError("dataset.record_count 与输入记录数不一致")
    ids = [record.record_id for record in snapshot.records]
    if len(set(ids)) != len(ids):
        raise HistoryImportError("重复 record_id：拒绝整批导入")
    declared_sources = metadata.get("source_files", [])
    if not isinstance(declared_sources, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("source_id"), str) or not item["source_id"].strip()
        for item in declared_sources
    ):
        raise HistoryImportError("source_files 来源清单不可用")
    source_ids = {item["source_id"] for item in declared_sources}
    if len(source_ids) != len(declared_sources):
        raise HistoryImportError("source_files 来源清单有重复身份")
    if not synthetic:
        declaration = metadata.get("human_declaration")
        if not source_ids or not isinstance(declaration, dict):
            raise HistoryImportError("真实数据必须保留来源清单及人工成功声明")
        if any(not isinstance(declaration.get(key), str) or not declaration[key].strip()
               for key in ("declared_by", "declared_at", "statement")):
            raise HistoryImportError("真实数据人工成功声明缺少声明人/时间/声明文本")
        scope = declaration.get("scope_source_ids")
        if not isinstance(scope, list) or any(not isinstance(item, str) for item in scope) or set(scope) != source_ids:
            raise HistoryImportError("人工成功声明必须覆盖全部来源且不得包含未知来源")
    formats = set()
    for record in snapshot.records:
        attributes = record.attributes
        if record.source.system not in SOURCE_KINDS or SOURCE_KINDS[record.source.system] != synthetic:
            raise HistoryImportError("记录来源未知或真实/合成来源混合")
        if "dataset_kind" in attributes and attributes["dataset_kind"] != ("synthetic" if synthetic else "real"):
            raise HistoryImportError("记录 dataset_kind 与数据集不一致")
        if source_ids and attributes.get("source_file_id") not in source_ids:
            raise HistoryImportError("记录不在声明的来源清单内")
        source_format = attributes.get("source_format", "legacy-template")
        if source_format not in {"legacy-template", FORMAT_CONSOLIDATED}:
            raise HistoryImportError("未知输入格式")
        formats.add(source_format)
        if source_format == FORMAT_CONSOLIDATED and attributes.get("identity_version") != CONSOLIDATED_IDENTITY_VERSION:
            raise HistoryImportError("记录汇总身份版本不兼容")
    declared_formats = metadata.get("input_formats")
    if declared_formats is not None:
        if not isinstance(declared_formats, list) or any(not isinstance(f, str) or f not in {"legacy-template", FORMAT_CONSOLIDATED} for f in declared_formats):
            raise HistoryImportError("input_formats 不可用")
        if not snapshot.records:
            formats = set(declared_formats)
        elif set(declared_formats) != formats:
            raise HistoryImportError("input_formats 与记录格式不一致")
    if len(formats) > 1:
        raise HistoryImportError("一个数据集不能混用汇总表与旧模板身份")
    if FORMAT_CONSOLIDATED in formats and metadata.get("identity_version") != CONSOLIDATED_IDENTITY_VERSION:
        raise HistoryImportError("数据集汇总身份版本不兼容")
    return snapshot, formats


def _fingerprint(payload):
    return hashlib.sha256(_business_fingerprint(payload).encode("utf-8")).hexdigest()


def _record_values(payload):
    record = _snapshot_from_payload({"records": [payload]}, source_name="database").records[0]
    return {
        "record_id": record.record_id, "historical_hs": record.historical_hs,
        "historical_name": record.historical_name, "needs_review": _needs_review(record),
        "blocking_issue_codes": _blocking_issue_codes(record),
        "source_document": record.source.document, "source_locator": record.source.locator,
        "source_system": record.source.system, "business_fingerprint": _fingerprint(payload),
        "payload": payload,
    }


def import_history_snapshot(path, dataset_key, replace_changed=False, expected_count=None,
                            require_real=False, *, using="default", allow_sqlite=False, dry_run=False):
    """Return a private audit report; defaults keep changed/missing historical facts.

    Changed content-derived IDs are paired by the existing P15 stable-anchor merger,
    not appended as new cases. Explicit replacement updates the SAME physical row,
    retaining its previous identities/content in HistoricalRecordRevision.
    A status of 'partial' means known changed/conflicting input was deliberately kept
    out; the successful batch is atomic, never a half-written batch.
    """
    from .models import HistoryDataset, HistoricalRecord, HistoricalRecordRevision, HistoryImportBatch

    if not isinstance(dataset_key, str) or not dataset_key.strip() or len(dataset_key) > 200:
        raise HistoryImportError("dataset_key 必须为 1..200 字符的非空键")
    vendor = connections[using].vendor
    if vendor != "postgresql" and not (allow_sqlite and vendor == "sqlite"):
        raise HistoryImportError("历史数据库导入仅允许 PostgreSQL；SQLite 仅限显式测试")
    try:
        data = Path(path).read_bytes()
        payload = json.loads(data)
    except (OSError, ValueError, UnicodeError) as exc:
        raise HistoryImportError(f"输入快照不可读（{type(exc).__name__}）") from exc
    snapshot, formats = _validate_payload(payload, expected_count=expected_count, require_real=require_real)
    metadata = copy.deepcopy(payload["dataset"])
    incoming = copy.deepcopy(payload["records"])
    with transaction.atomic(using=using):
        # get_or_create's unique-key retry is safe against simultaneous first imports;
        # always re-fetch with FOR UPDATE, including the existing-object path.
        dataset, _ = HistoryDataset.objects.using(using).get_or_create(
            key=dataset_key, defaults={"synthetic": snapshot.dataset.synthetic, "metadata": metadata})
        dataset = HistoryDataset.objects.using(using).select_for_update().get(pk=dataset.pk)
        if dataset.synthetic != snapshot.dataset.synthetic:
            raise HistoryImportError("固定 dataset_key 不允许真实/合成数据混合")
        existing_rows = list(HistoricalRecord.objects.using(using).filter(dataset=dataset).order_by("pk"))
        if len(existing_rows) != dataset.record_count:
            raise HistoryImportError("已有数据库 count 不一致，拒绝导入")
        if existing_rows:
            old_meta = dataset.metadata
            for key in ("input_formats", "identity_version"):
                if old_meta.get(key) != metadata.get(key):
                    raise HistoryImportError(f"固定 dataset_key 的 {key} 不兼容")
            old_sources = {str(item.get("source_id", "")) for item in old_meta.get("source_files", [])}
            new_sources = {str(item.get("source_id", "")) for item in metadata.get("source_files", [])}
            if old_sources != new_sources:
                raise HistoryImportError("固定 dataset_key 不允许更换来源文件集合")
        old_payloads = [copy.deepcopy(row.payload) for row in existing_rows]
        existing = {"dataset": dataset.metadata, "records": old_payloads}
        conflicts, missing = [], []
        if FORMAT_CONSOLIDATED in formats:
            final, duplicates, writes, conflicts, missing, actions, added = _merge_consolidated_records(
                existing, incoming, replace_changed=replace_changed, keep_business_duplicates=False,
                accept_conflicts=False, current_source_ids={str(item["source_id"]) for item in metadata.get("source_files", [])} or {str((p.get("attributes") or {}).get("source_file_id", "")) for p in incoming})
        else:
            final, duplicates, writes = _merge_records(existing, incoming, replace_changed=replace_changed)
            incoming_ids = {p["record_id"] for p in incoming}
            missing = [{"record_id": p["record_id"], "kind": "missing_from_source", "action": "kept"}
                       for p in old_payloads if p["record_id"] not in incoming_ids and not any(
                           d.get("kind") == "changed" and d.get("action") == "replaced" and
                           (p.get("attributes") or {}).get("source_key") == d.get("source_key") for d in duplicates)]
        repairs = _enforce_stored_hs_review(final)
        # Revalidate the complete merged generation, not just input.
        _snapshot_from_payload({"schema_version": SCHEMA_VERSION, "dataset": metadata, "records": final}, source_name="merged")
        old_by_id = {row.record_id: row for row in existing_rows}
        final_ids = [p["record_id"] for p in final]
        if len(final_ids) != len(set(final_ids)):
            raise HistoryImportError("合并产生重复身份，拒绝整批")
        replacements = {d["record_id"]: d["previous_record_id"] for d in duplicates
                        if d.get("action") == "replaced" and d.get("previous_record_id")}
        # Legacy source-key replacement also keeps its physical SQL identity.
        for p in final:
            if p["record_id"] not in old_by_id and p["record_id"] not in replacements:
                source_key = (p.get("attributes") or {}).get("source_key")
                for row in existing_rows:
                    if row.record_id not in final_ids and source_key and (row.payload.get("attributes") or {}).get("source_key") == source_key:
                        replacements[p["record_id"]] = row.record_id
        if set(old_by_id) - set(final_ids) - set(replacements.values()):
            raise HistoryImportError("合并试图删除旧历史，拒绝整批")
        added_count = sum(p["record_id"] not in old_by_id and p["record_id"] not in replacements for p in final)
        kept_changed = sum(d.get("kind") == "changed" and d.get("action") == "kept_existing" for d in duplicates)
        conflict_count = sum(c.get("kind") != "business_key_variant" for c in conflicts)
        report = {
            "schema_version": SCHEMA_VERSION, "sha256": hashlib.sha256(data).hexdigest(),
            "input_total": len(incoming), "previous_total": len(existing_rows), "total": len(final),
            "added": added_count, "replaced": sum(d.get("action") == "replaced" for d in duplicates),
            "duplicate": sum(d.get("kind", "").startswith("duplicate") for d in duplicates),
            "changed_kept": kept_changed, "missing": len(missing), "conflicts": conflict_count,
            "business_key_variants": sum(c.get("kind") == "business_key_variant" for c in conflicts),
            "needs_review": sum(_record_values(p)["needs_review"] for p in final),
            "status": "partial" if kept_changed or conflict_count else "success", "dry_run": dry_run,
            "duplicates": duplicates, "conflict_details": conflicts, "missing_details": missing,
            "review_state_repairs": repairs, "input_dataset": copy.deepcopy(metadata),
            "revision": dataset.revision + 1,
        }
        if dry_run:
            transaction.set_rollback(True, using=using)
            return report
        dataset.revision += 1
        metadata["record_count"] = len(final)
        dataset.metadata = metadata
        dataset.record_count = len(final)
        dataset.save(using=using)
        batch = HistoryImportBatch.objects.using(using).create(
            dataset=dataset, sha256=report["sha256"], revision=dataset.revision,
            total=len(final), status=report["status"], replace_changed=replace_changed, report=report)
        for p in final:
            row = old_by_id.get(p["record_id"]) or old_by_id.get(replacements.get(p["record_id"]))
            if row is not None and row.payload == p:
                continue
            values = _record_values(p)
            if row is None:
                row = HistoricalRecord.objects.using(using).create(dataset=dataset, **values)
            else:
                row.revision += 1
                for key, value in values.items():
                    setattr(row, key, value)
                row.save(using=using)
            HistoricalRecordRevision.objects.using(using).create(
                record=row, batch=batch, revision=row.revision, historical_record_id=row.record_id, payload=p)
        if HistoricalRecord.objects.using(using).filter(dataset=dataset).count() != len(final):
            raise HistoryImportError("写入后 count 校验失败，整批回滚")
        return report
