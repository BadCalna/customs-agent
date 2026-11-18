from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("reviews", "0007_p06_preserve_raw_text"),
    ]

    operations = [
        migrations.AlterField(
            model_name="importjob",
            name="status",
            field=models.CharField(
                choices=[
                    ("staged", "已暂存"),
                    ("succeeded", "解析成功"),
                    ("failed", "处理失败"),
                    ("superseded", "已由新版本替代"),
                ],
                default="staged",
                max_length=16,
            ),
        ),
    ]
