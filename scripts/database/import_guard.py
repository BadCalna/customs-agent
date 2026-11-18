"""Shared import-original gate for migration, complete backup and restore."""


def require_completed_imports(file_root=None, *, file_hashes=None):
    from pathlib import Path

    from django.conf import settings

    from adapters.files import storage
    from apps.reviews.models import ImportJob

    completed = (ImportJob.Status.SUCCEEDED, ImportJob.Status.SUPERSEDED)
    pending = ImportJob.objects.exclude(status__in=completed)
    count = pending.count()
    if count:
        examples = list(pending.order_by("pk").values_list("pk", "status")[:10])
        details = ", ".join(f"{job_id} ({status})" for job_id, status in examples)
        raise ValueError(
            f"存在 {count} 个未完成导入（ImportJob 非 succeeded/superseded）：{details}。"
            "拒绝迁移/完整备份：任务仍可能依赖 incoming/ 暂存原件及回执。"
            "请在原数据库和原文件根重试，或上传修正版并等待成功导入。"
        )

    root = Path(file_root if file_root is not None else settings.CUSTOMS_FILE_ROOT)
    for job in ImportJob.objects.select_related("version").iterator():
        path = job.version.storage_path
        suffix = Path(path).suffix.lower()
        expected = storage.version_original_relative_path(
            str(job.version.task_id), str(job.version_id), job.sha256, suffix,
        ) if path else ""
        if not path or suffix not in storage.ORIGINAL_SUFFIXES or path != expected:
            raise ValueError(f"导入原件未保全或路径不符: import_id={job.pk}, version_id={job.version_id}")
        try:
            actual_hash = (file_hashes.get(path) if file_hashes is not None
                           else storage.file_sha256(root, path))
        except (storage.StorageError, OSError) as exc:
            raise ValueError(f"导入原件未保全: import_id={job.pk}, version_id={job.version_id}") from exc
        if actual_hash != job.sha256:
            raise ValueError(f"导入原件未保全或哈希不符: import_id={job.pk}, version_id={job.version_id}")
