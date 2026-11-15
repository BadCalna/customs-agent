"""P12 测试用合成数据：一条任务、一个版本、两条已确认商品、一份待审提交。

与 P09/P10 的工厂保持同一口径，只抽取 PostgreSQL 兼容性检查需要的最小图。
"""
from apps.accounts.models import Role
from apps.reviews.models import Confirmation, Submission, SubmissionItem, Task
from tests.p01.factories import assign, make_item, make_task, make_user, make_version

HS_A = "8544421900"
HS_B = "7304210000"


def build_submitted_task():
    """构造已提交任务，返回 (task, version, items, confirmations, submission, users)。"""
    supplier = make_user("supplier_p12", Role.SUPPLIER)
    operator = make_user("operator_p12", Role.OPERATOR)
    expert = make_user("expert_p12", Role.EXPERT)
    task = make_task(supplier, stage=Task.Stage.SUBMITTED)
    assign(task, operator, Role.OPERATOR)
    assign(task, expert, Role.EXPERT)
    version = make_version(task, supplier)
    item_a = make_item(version, row=5, name="合成商品A", supplier_hs=HS_A)
    item_b = make_item(version, row=6, name="合成商品B", supplier_hs=HS_B)
    confirm_a = Confirmation.objects.create(
        item=item_a, revision=1, previous_hs="", confirmed_hs=HS_A,
        reason="合成确认", created_by=operator,
    )
    confirm_b = Confirmation.objects.create(
        item=item_b, revision=1, previous_hs="", confirmed_hs=HS_B,
        reason="合成确认", created_by=operator,
    )
    submission = Submission.objects.create(
        task=task, version=version, revision=task.revision, created_by=operator,
    )
    SubmissionItem.objects.create(
        submission=submission, item=item_a, confirmation=confirm_a,
        submitted_hs=HS_A, item_revision=1,
    )
    SubmissionItem.objects.create(
        submission=submission, item=item_b, confirmation=confirm_b,
        submitted_hs=HS_B, item_revision=1,
    )
    return {
        "supplier": supplier,
        "operator": operator,
        "expert": expert,
        "task": task,
        "version": version,
        "items": [item_a, item_b],
        "confirmations": [confirm_a, confirm_b],
        "submission": submission,
    }