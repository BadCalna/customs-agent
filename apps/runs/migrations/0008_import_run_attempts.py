from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("runs", "0007_register_existing_pending_runs"),
    ]

    operations = [
        migrations.AddField(
            model_name="runattempt",
            name="run",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                related_name="attempts", to="runs.run",
            ),
        ),
        migrations.AlterField(
            model_name="runattempt",
            name="row",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                related_name="attempt_records", to="runs.runitem",
            ),
        ),
    ]
