"""Reference-only history tables; deliberately no review/business-table foreign keys."""
from django.db import models


class HistoryDataset(models.Model):
    key = models.CharField(max_length=200, unique=True)
    metadata = models.JSONField(default=dict)
    synthetic = models.BooleanField()
    revision = models.PositiveIntegerField(default=0)
    record_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class HistoryImportBatch(models.Model):
    dataset = models.ForeignKey(HistoryDataset, on_delete=models.PROTECT, related_name="batches")
    sha256 = models.CharField(max_length=64)
    revision = models.PositiveIntegerField()
    total = models.PositiveIntegerField()
    status = models.CharField(max_length=24)
    replace_changed = models.BooleanField(default=False)
    report = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class HistoricalRecord(models.Model):
    dataset = models.ForeignKey(HistoryDataset, on_delete=models.PROTECT, related_name="records")
    record_id = models.TextField()
    historical_hs = models.TextField(db_index=True)
    historical_name = models.TextField()
    needs_review = models.BooleanField(default=False, db_index=True)
    blocking_issue_codes = models.JSONField(default=list)
    source_document = models.TextField()
    source_locator = models.TextField()
    source_system = models.TextField()
    business_fingerprint = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField()
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["dataset", "record_id"], name="history_dataset_record_uniq")]


class HistoricalRecordRevision(models.Model):
    """Append-only snapshots, including the old content-derived identity on replacement."""
    record = models.ForeignKey(HistoricalRecord, on_delete=models.PROTECT, related_name="revisions")
    batch = models.ForeignKey(HistoryImportBatch, on_delete=models.PROTECT, related_name="record_revisions")
    revision = models.PositiveIntegerField()
    historical_record_id = models.TextField()
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["record", "revision"], name="history_record_revision_uniq")]
