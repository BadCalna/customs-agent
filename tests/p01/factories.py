"""P01 测试用合成数据：供应商甲/乙、业务员甲/乙、单专家（D1 R10）。"""
from django.contrib.auth import get_user_model

from apps.accounts.models import Role, TaskAssignment
from apps.reviews.models import FileVersion, Item, Task

User = get_user_model()

PASSWORD = "p01-test-pass"


def make_user(username, role, display_name=""):
    user = User(username=username, role=role, display_name=display_name or username)
    user.set_password(PASSWORD)
    user.save()
    return user


def make_task(supplier, title="合成任务", stage=Task.Stage.UPLOADED):
    return Task.objects.create(supplier=supplier, title=title, stage=stage)


def assign(task, user, role):
    return TaskAssignment.objects.create(task=task, user=user, role=role)


def make_version(task, uploader, number=1, filename="合成模板.xlsx"):
    return FileVersion.objects.create(
        task=task, number=number, original_filename=filename, uploaded_by=uploader,
    )


def make_item(version, row=8, name="合成商品", supplier_hs="8544421900"):
    return Item.objects.create(
        version=version,
        source_sheet="申报要素",
        source_row=row,
        source_cells=f"A{row}:Y{row}",
        name=name,
        supplier_hs_raw=supplier_hs,
    )
