"""Lazy ORM implementation of the P15 HistoryRecordStore protocol."""
from __future__ import annotations

from adapters.sources.history_search import HistoryStoreError


class DatabaseHistoryStore:
    def __init__(self, dataset_key, using="default"):
        self.dataset_key = dataset_key
        self.using = using

    def load(self):
        # No model import at module load: this provider can be selected before
        # Django initializes its app registry. Writers and readers both lock the
        # dataset first, so metadata and rows belong to the same committed batch
        # even under PostgreSQL's default READ COMMITTED isolation.
        try:
            from django.db import transaction
            from .models import HistoryDataset, HistoricalRecord
            from .services import _validate_payload, _record_values
            with transaction.atomic(using=self.using):
                dataset = HistoryDataset.objects.using(self.using).select_for_update().get(key=self.dataset_key)
                rows = list(HistoricalRecord.objects.using(self.using).filter(dataset=dataset).order_by("record_id"))
                if not rows:
                    raise HistoryStoreError("历史数据集为空，不能伪装成 no_match")
                if len(rows) != dataset.record_count or dataset.metadata.get("record_count") != dataset.record_count:
                    raise HistoryStoreError("历史数据集 count 与存储记录不一致")
                if dataset.metadata.get("synthetic") is not dataset.synthetic:
                    raise HistoryStoreError("历史数据集来源标记不一致")
                for row in rows:
                    expected = _record_values(row.payload)
                    if any(getattr(row, key) != value for key, value in expected.items() if key != "payload"):
                        raise HistoryStoreError("历史记录 SQL 字段与原文快照不一致")
                snapshot, _ = _validate_payload({
                    "schema_version": "p15.history.1", "dataset": dataset.metadata,
                    "records": [row.payload for row in rows],
                }, expected_count=dataset.record_count)
                return snapshot
        except HistoryStoreError:
            raise
        except Exception as exc:
            # No credentials, raw SQL or real record contents in the public reason.
            raise HistoryStoreError(f"历史数据库不可用或不符合契约（{type(exc).__name__}）") from exc
