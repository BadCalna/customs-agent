"""P09 手工/自测脚本：真实 HTTP 走一遍「确认 → 整批提交 → 自动专家核查 → 幂等重放」。

前置（在本工作区根目录）：
    make migrate && make seed-demo && make seed-contracts
    make dev BIND=127.0.0.1:8019
运行：
    BASE=http://127.0.0.1:8019 .venv-customs/bin/python tests/p09/manual_check.py

只操作契约演示数据；全部编码为合成样例，不构成真实归类结论。
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import django  # noqa: E402

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from apps.accounts.management.commands.seed_demo import DEMO_PASSWORD  # noqa: E402

BASE = os.environ.get("BASE", "http://127.0.0.1:8019")
MOCK_FALLBACK_HS = "8544422900"  # 与 adapters/engines/mock.py 的合成样例一致

cookies = {}
results = []


def call(method, path, body=None, key=None, csrf=True):
    headers = {"Content-Type": "application/json"}
    if cookies:
        headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in cookies.items())
    if csrf and "csrftoken" in cookies:
        headers["X-CSRFToken"] = cookies["csrftoken"]
    if key:
        headers["Idempotency-Key"] = key
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + "/api/v1" + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            raw, status = response.read(), response.status
            for header in response.headers.get_all("Set-Cookie") or []:
                name, _, rest = header.partition("=")
                cookies[name] = rest.split(";", 1)[0]
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
        for header in exc.headers.get_all("Set-Cookie") or []:
            name, _, rest = header.partition("=")
            cookies[name] = rest.split(";", 1)[0]
    payload = json.loads(raw) if raw else {}
    return status, payload


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}")


def main():
    status, _ = call("GET", "/me")
    check("匿名 /me 返回 401 并种下 CSRF cookie", status == 401 and "csrftoken" in cookies, f"status={status}")

    status, session = call("POST", "/auth/login", {"username": "operator_a", "password": DEMO_PASSWORD}, csrf=True)
    check("业务员登录", status == 200, f"status={status} role={session.get('role')}")
    # 登录会轮换 CSRF 令牌：必须重读 cookie，否则后续写请求 csrf_failed。
    call("GET", "/me")

    status, page = call("GET", "/tasks")
    task = next((t for t in page.get("items", []) if t["title"] == "契约演示-主流程"), None)
    check("找到契约演示主流程任务", status == 200 and task is not None)
    if task is None:
        return 1

    status, detail = call("GET", f"/tasks/{task['task_id']}")
    version_id = detail["current_version_id"]
    status, items_page = call("GET", f"/versions/{version_id}/items", None)
    items = items_page["items"]
    check("读取当前版本商品列表", status == 200 and len(items) > 0, f"items={len(items)}")

    confirmations = []
    for entry in items:
        hs = entry["precheck_suggested_hs"] or MOCK_FALLBACK_HS
        status, body = call(
            "PATCH", f"/versions/{version_id}/items/{entry['item_id']}/confirmation",
            {"confirmed_hs": hs, "reason": "手工自测：按预审建议确认", "expected_revision": entry["revision"]},
        )
        ok = status == 200
        check(f"确认 {entry['name']}", ok, f"status={status} hs={body.get('confirmed_hs')}")
        if not ok:
            print(json.dumps(body, ensure_ascii=False))
            return 1
        confirmations.append({"item_id": entry["item_id"], "confirmation_id": body["confirmation_id"]})

    status, conflict = call(
        "PATCH", f"/versions/{version_id}/items/{items[0]['item_id']}/confirmation",
        {"confirmed_hs": MOCK_FALLBACK_HS, "reason": "过期修订", "expected_revision": 1},
    )
    check("过期修订返回 409 stale_revision", status == 409 and conflict["error"]["code"] == "stale_revision")

    status, detail = call("GET", f"/tasks/{task['task_id']}")
    payload = {"version_id": version_id, "revision": detail["revision"], "items": confirmations}
    status, submission = call("POST", f"/tasks/{task['task_id']}/submissions", payload, key="p09-manual-1")
    check("整批提交成功并返回专家执行 ID", status == 200 and submission.get("expert_run_id"), f"status={status}")
    if status != 200:
        print(json.dumps(submission, ensure_ascii=False))
        return 1

    status, replay = call("POST", f"/tasks/{task['task_id']}/submissions", payload, key="p09-manual-1")
    check(
        "同 key 同内容重放同一提交",
        status == 200 and replay["submission_id"] == submission["submission_id"] and replay["idempotent_replay"],
    )

    status, run = call("GET", f"/runs/{submission['expert_run_id']}")
    check(
        "专家核查已自动执行且行数等于快照行数",
        status == 200 and len(run.get("items", [])) == len(confirmations),
        f"run_status={run.get('status')} items={len(run.get('items', []))}",
    )

    status, after = call("GET", f"/tasks/{task['task_id']}")
    check("任务阶段变为已提交专家", after.get("stage") == "submitted", f"stage={after.get('stage')}")

    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过" + (f"；失败：{failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
