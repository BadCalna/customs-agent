"""Refuse data movement that cannot preserve unfinished uploads' recovery state.

ImportJob is committed before its original is published. An empty storage_path
therefore does not imply that there is no file to preserve: failed/staged jobs
can still need incoming/<token> and its receipt. Migration and complete backups
exclude incoming, so they are allowed only after every import has succeeded.
"""


def require_completed_imports():
    from apps.reviews.models import ImportJob

    pending = ImportJob.objects.exclude(status=ImportJob.Status.SUCCEEDED)
    count = pending.count()
    if count:
        # IDs/statuses are enough to locate jobs; never expose stage credentials.
        examples = list(pending.order_by("pk").values_list("pk", "status")[:10])
        details = ", ".join(f"{job_id} ({status})" for job_id, status in examples)
        raise ValueError(
            f"存在 {count} 个未完成导入（ImportJob 非 succeeded）：{details}。"
            "拒绝迁移/完整备份：任务仍可能依赖 incoming/ 暂存原件及回执。"
            "请在原数据库和原文件根用原幂等键重试，或运行 manage.py resume_import <import_id>；"
            "全部成功后再停写重试。不要删除暂存文件或直接修改导入状态。"
        )
