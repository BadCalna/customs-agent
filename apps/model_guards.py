from django.core.exceptions import ValidationError
from django.db import models, router
from django.db.models.signals import pre_delete


class FixedInputQuerySet(models.QuerySet):
    def _check_update_fields(self, names):
        protected = {"pk", self.model._meta.pk.name}
        for name in self.model.fixed_input_fields:
            field = self.model._meta.get_field(name)
            protected.update((field.name, field.attname))
        conflicts = protected.intersection(names)
        if conflicts:
            raise ValidationError(f"历史记录禁止批量更新字段 {', '.join(sorted(conflicts))}。")

    def update(self, **kwargs):
        self._check_update_fields(kwargs)
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        fields = tuple(fields)
        self._check_update_fields(fields)
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def bulk_create(
        self, objs, batch_size=None, ignore_conflicts=False, update_conflicts=False,
        update_fields=None, unique_fields=None,
    ):
        if update_conflicts and update_fields:
            update_fields = tuple(update_fields)
            self._check_update_fields(update_fields)
        objs = list(objs)
        for obj in objs:
            obj.clean()
        return super().bulk_create(
            objs, batch_size=batch_size, ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts, update_fields=update_fields, unique_fields=unique_fields,
        )


class FixedInputModel(models.Model):
    """创建后固定业务输入。运行状态等其余字段仍可用普通或批量更新。"""

    fixed_input_fields = ()
    objects = FixedInputQuerySet.as_manager()

    class Meta:
        abstract = True
        # 反向外键的批量 add() 通过基础管理器更新已有行。
        base_manager_name = "objects"

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None):
        using = using or router.db_for_write(type(self), instance=self)
        # UUID 主键在插入前已经存在。显式以已有主键构造实例并更新时也要检查。
        if self.pk is not None and not force_insert:
            fields = self.fixed_input_fields
            stored = type(self).objects.using(using).filter(pk=self.pk).values_list(*fields).first()
            if stored is not None:
                current = tuple(getattr(self, self._meta.get_field(name).attname) for name in fields)
                if current != stored:
                    raise ValidationError(f"{self._meta.verbose_name}的历史输入已经固定。")
        self.clean()
        return super().save(
            force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields,
        )


def protect_from_direct_delete(model):
    """禁止单独删除历史记录；随父记录级联删除仍然允许。

    与 ``fixed_input_fields`` 配套：前者防止历史内容被改写，本函数防止历史行
    被删掉后用新行顶替。级联（如删除整个任务）不拦，避免清理流程完全不可用。
    """

    def _protect(sender, instance, origin, **kwargs):
        if isinstance(origin, sender) or getattr(origin, "model", None) is sender:
            raise models.ProtectedError(
                f"{sender._meta.verbose_name}属于历史记录，不能单独删除。", [instance],
            )

    pre_delete.connect(
        _protect, sender=model,
        dispatch_uid=f"model_guards.protect_from_direct_delete.{model._meta.label_lower}",
        weak=False,
    )
