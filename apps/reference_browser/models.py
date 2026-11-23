"""Django permission anchor. Snapshot read models live in store.py, not the DB."""
from django.db import models


class ReferenceAccess(models.Model):
    class Meta:
        managed = False
        default_permissions = ()
        permissions = (
            ("view_catalog", "查看基础目录（注释、申报要素和退税）"),
            ("view_history", "检索脱敏历史参考"),
            ("view_history_detail", "查看脱敏历史详情及证据"),
        )
