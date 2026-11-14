"""D3 第 2 节中尚未实现端点的显式 501 占位。

目的：前端与执行 Agent 调用时得到明确的“未实现”，而不是 404 或伪造成功。
这些占位不读写任何业务数据；真正的实现由对应任务包替换本表条目。
"""
import inspect
import re

from ninja import Router

from apps.accounts.auth import session_auth
from apps.accounts.errors import ApiError

router = Router(tags=["未实现端点"])

# (方法, 路径, 交付任务包)
# 注意：与已实现端点同路径的占位不能放在本 router，Ninja 按路径先注册者优先，
# 跨 router 的同路径方法会被 405。POST /tasks 因此在 reviews/api/read.py 注册。
PENDING_ENDPOINTS = [
    ("POST", "/tasks/{task_id}/versions", "P06"),
    ("POST", "/versions/{version_id}/prechecks", "P13"),
    ("POST", "/runs/{run_id}/retries", "P13"),
    ("PATCH", "/versions/{version_id}/items/{item_id}/confirmation", "P09"),
    ("POST", "/tasks/{task_id}/submissions", "P09"),
    ("POST", "/tasks/{task_id}/returns", "P10"),
    ("POST", "/submissions/{submission_id}/decisions", "P10"),
    ("GET", "/tasks/{task_id}/history", "P10"),
    ("POST", "/versions/{version_id}/exports", "P20"),
    ("GET", "/files/{file_id}", "P05"),
    ("GET", "/messages", "P19"),
    ("PATCH", "/messages/{message_id}", "P19"),
]


def pending_view(method: str, path: str, owner: str):
    def view(request, **kwargs):
        raise ApiError(
            501,
            "not_implemented",
            f"{method} {path} 尚未实现，由 {owner} 交付；本响应不代表任何业务动作已执行",
            {"owner": owner},
        )

    view.__name__ = "pending_" + (method + "_" + path).replace("/", "_").replace("{", "").replace("}", "")
    # Ninja 依据函数签名生成路径参数与 OpenAPI；占位视图也要显式声明，避免警告。
    parameters = [inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    parameters += [
        inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str)
        for name in re.findall(r"{(\w+)}", path)
    ]
    view.__signature__ = inspect.Signature(parameters)
    return view


for _method, _path, _owner in PENDING_ENDPOINTS:
    router.api_operation([_method], _path, auth=session_auth)(pending_view(_method, _path, _owner))
