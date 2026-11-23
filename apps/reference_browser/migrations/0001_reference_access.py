from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="ReferenceAccess",
            fields=[("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"))],
            options={
                "managed": False,
                "default_permissions": (),
                "permissions": (
                    ("view_catalog", "查看基础目录（注释、申报要素和退税）"),
                    ("view_history", "检索脱敏历史参考"),
                    ("view_history_detail", "查看脱敏历史详情及证据"),
                ),
            },
        ),
    ]
