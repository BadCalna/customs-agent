"""对象级权限：可见范围按任务归属，动作按角色矩阵。

约定（D3 第 1、2、5 节）：
- 未登录：401（``AuthenticationRequired``）；
- 已登录但对象不属于可见范围、或直接改 ID 取不到：404（``Http404``，不泄露存在性）；
- 可见但角色不允许该动作：403（``PermissionDenied``），例如业务员尝试批准；
- 文件版本、附图、商品、执行、提交、决定都归一到所属任务做同一套校验。

供 P02 及后续业务端点直接复用；服务层不依赖 HTTP，可由测试单独覆盖。
"""
from django.apps import apps
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404

from .errors import AuthenticationRequired
from .models import Role, TaskAssignment

READ = "read"
UPLOAD_VERSION = "version.create"
CONFIRM = "confirmation.write"
SUBMIT = "submission.create"
DECIDE = "decision.resolve"
RETURN_TASK = "task.return"

# 角色动作矩阵（D3 第 5 节发布）；空 role 的非业务账号不在表中，没有任何业务动作。
ROLE_ACTIONS = {
    Role.SUPPLIER: frozenset({READ, UPLOAD_VERSION}),
    Role.OPERATOR: frozenset({READ, CONFIRM, SUBMIT, RETURN_TASK}),
    Role.EXPERT: frozenset({READ, DECIDE}),
}

# 业务对象 → 所属任务。新增业务对象时补在这里，避免各处自行拼归属判断。
_TASK_PATH = {
    "Task": "",
    "FileVersion": "task",
    "Item": "version.task",
    "ItemMember": "item.version.task",
    "ItemImage": "item.version.task",
    "Confirmation": "item.version.task",
    "Submission": "task",
    "SubmissionItem": "submission.task",
    "Decision": "submission.task",
    "DecisionItem": "decision.submission.task",
    "Issue": "task",
    "AuditEvent": "task",
    "ImportJob": "version.task",
    "TaskClaim": "task",
    "Run": "task",
    "RunItem": "run.task",
}


def _require_authenticated(user):
    if user is None or not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        raise AuthenticationRequired()


def _review_model(name):
    return apps.get_model("reviews", name)


def is_readonly_superuser(user) -> bool:
    """无业务角色的超管只读全览；有业务角色的账号仍按角色和任务归属授权。"""
    return bool(getattr(user, "is_superuser", False) and not user.role)


def visible_tasks(user):
    """当前账号可见的任务集合。未登录直接拒绝，不返回空集合冒充“无数据”。"""
    _require_authenticated(user)
    task_model = _review_model("Task")
    if is_readonly_superuser(user):
        return task_model.objects.all()  # 空 role 不在动作矩阵中，不能写入。
    role = user.role
    if role == Role.SUPPLIER:
        return task_model.objects.filter(supplier=user)
    if role in (Role.OPERATOR, Role.EXPERT):
        return task_model.objects.filter(
            assignments__user=user, assignments__role=role,
        ).distinct()
    return task_model.objects.none()


def get_visible_task(user, task_id):
    try:
        task = visible_tasks(user).filter(pk=task_id).first()
    except (ValidationError, ValueError) as exc:
        raise Http404("任务不存在或不在可见范围") from exc
    if task is None:
        raise Http404("任务不存在或不在可见范围")
    return task


def task_of(obj):
    """把任意业务对象归一到所属任务。"""
    name = obj._meta.object_name
    if name not in _TASK_PATH:
        raise TypeError(f"不支持的对象类型：{name}")
    path = _TASK_PATH[name]
    target = obj
    if path:
        for attribute in path.split("."):
            target = getattr(target, attribute)
            if target is None:
                raise Http404("对象所属任务不存在或不在可见范围")
    return target


def require_action(user, action, obj):
    """校验动作权限。返回所属任务；调用方可在同一事务内继续写。"""
    _require_authenticated(user)
    task = task_of(obj)
    if not visible_tasks(user).filter(pk=task.pk).exists():
        raise Http404("对象不存在或不在可见范围")
    if action == READ:
        return task
    role = user.role
    if action not in ROLE_ACTIONS.get(role, frozenset()):
        raise PermissionDenied(f"角色「{user.role_label or '未分配'}」不允许执行 {action}")
    if role == Role.SUPPLIER and task.supplier_id != user.pk:
        raise Http404("对象不存在或不在可见范围")
    if role in (Role.OPERATOR, Role.EXPERT) and not TaskAssignment.objects.filter(
        task=task, user=user, role=role,
    ).exists():
        raise Http404("对象不存在或不在可见范围")
    return task


def get_visible_message(user, message_id):
    """站内消息只对收件人本人可见；他人消息与不存在的消息同样返回 404。"""
    _require_authenticated(user)
    message_model = apps.get_model("notifications", "Message")
    try:
        message = message_model.objects.filter(pk=message_id, recipient=user).first()
    except (ValidationError, ValueError, TypeError) as exc:
        raise Http404("消息不存在或不在可见范围") from exc
    if message is None:
        raise Http404("消息不存在或不在可见范围")
    return message
