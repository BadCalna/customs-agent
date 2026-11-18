"""Never infer a queued Run's original engine from migration-time settings."""
from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone

import apps.runs.engine_config


KEYS = ("result_mode", "engine_version", "prompt_version", "rules_version")


def freeze_history(apps, schema_editor):
    Run = apps.get_model("runs", "Run")
    Row = apps.get_model("runs", "RunItem")
    Work = apps.get_model("runs", "RunWork")
    Attempt = apps.get_model("runs", "RunAttempt")
    Import = apps.get_model("reviews", "ImportJob")
    db = schema_editor.connection.alias
    now = timezone.now()
    for run in Run.objects.using(db).all().iterator():
        # Completed homogeneous rows can describe history, but cannot prove the
        # original model/endpoint config hash. Never make them resumable.
        results = list(Row.objects.using(db).filter(run_id=run.pk).values_list("result", flat=True))
        identities = [tuple((r or {}).get(k) for k in KEYS) for r in results]
        if identities and len(set(identities)) == 1 and all(identities[0]):
            values = dict(zip(KEYS, identities[0]))
            if values["result_mode"] in ("mock", "live"):
                values["engine_mode"] = "mock" if values["result_mode"] == "mock" else "openai"
                Run.objects.using(db).filter(pk=run.pk).update(engine_snapshot=values)
        if run.status in ("queued", "running"):
            Run.objects.using(db).filter(pk=run.pk).update(status="superseded", is_current=False, finished_at=now)
            Work.objects.using(db).filter(run_id=run.pk).update(
                state="done", token=None, lease_until=None, last_error="legacy_engine_configuration_unknown")
            Attempt.objects.using(db).filter(Q(run_id=run.pk) | Q(row__run_id=run.pk), status="running").update(
                status="superseded", finished_at=now)
            Import.objects.using(db).filter(precheck_id=run.pk, status="staged").update(
                status="failed", error={"code": "legacy_engine_configuration_unknown",
                    "message": "旧执行未登记固定引擎配置，请重新上传以发起完整新轮"})


class Migration(migrations.Migration):
    dependencies = [("runs", "0008_import_run_attempts"), ("reviews", "0008_import_job_superseded")]
    operations = [
        migrations.AddField(model_name="run", name="engine_snapshot",
            field=models.JSONField(default=dict, verbose_name="固定引擎配置",
                help_text="登记时固定模式、版本及非敏感配置摘要；未知历史配置不得自动恢复。")),
        migrations.RunPython(freeze_history, migrations.RunPython.noop),
        migrations.AlterField(model_name="run", name="engine_snapshot",
            field=models.JSONField(default=apps.runs.engine_config.current_engine_snapshot, verbose_name="固定引擎配置",
                help_text="登记时固定模式、版本及非敏感配置摘要；未知历史配置不得自动恢复。")),
    ]
