from django.db import migrations


def register_pending(apps, schema_editor):
    Run = apps.get_model("runs", "Run")
    Work = apps.get_model("runs", "RunWork")
    Row = apps.get_model("runs", "RunItem")
    for run in Run.objects.filter(status__in=["queued", "running"]).iterator():
        _, created = Work.objects.get_or_create(run_id=run.pk)
        if created:
            # Legacy inline runs did not have an attempt ledger. Retain completed
            # rows, resume unfinished ones, and never fabricate historical calls.
            Row.objects.filter(run_id=run.pk, status="running").update(status="queued")
            Run.objects.filter(pk=run.pk).update(status="queued")


class Migration(migrations.Migration):
    dependencies = [("runs", "0006_p13_durable_work_and_attempts")]
    operations = [migrations.RunPython(register_pending, migrations.RunPython.noop)]
