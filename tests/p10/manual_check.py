"""P10 手工/自测脚本：专家整批通过（含一次改码）→ 重复处置 409 → 历史回查。

前置：先跑过 P09 的 `tests/p09/manual_check.py`（主流程任务已整批提交），然后
    make dev BIND=127.0.0.1:8020
运行：
    BASE=http://127.0.0.1:8020 .venv-customs/bin/python tests/p10/manual_check.py

只操作契约演示数据；编码均为合成样例。
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

BASE = os.environ.get("BASE", "http://127.0.0.1:8020")
RECODE_HS = "8536901900"  # 合成改码样例，与 adapters/engines/mock.py 一致

cookies = {}
results = []


def call(method, path, body=None, key=None):
    headers = {"Content-Type": "application/json"}
    if cookies:
        headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in cookies.items())
    if "csrftoken" in cookies:
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
    return status, json.loads(raw) if raw else {}


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}")


def main():
    call("GET", "/me")
    status, session = call("POST", "/auth/login", {"username": "expert", "password": DEMO_PASSWORD})
    check("专家登录", status == 200 and session.get("role") == "expert")

    status, page = call("GET", "/tasks")
    task = next((t for t in page.get("items", []) if t["title"] == "契约演示-主流程"), None)
    if task is None:
        print("找不到契约演示主流程任务，请先 make seed-contracts 并跑 P09 自测")
        return 1

    status, history = call("GET", f"/tasks/{task['task_id']}/history")
    current = next(v for v in history["versions"] if v["is_current"])
    submission = current["submissions"][-1]
    lines = submission["items"]
    check("历史接口返回当前提交与逐行快照", status == 200 and len(lines) > 0, f"items={len(lines)}")

    payload = {
        "action": "approve", "reason": "手工自测：专家整批通过，首行改码",
        "items": [
            {"item_id": line["item_id"], "final_hs": line["submitted_hs"]} for line in lines
        ],
    }
    payload["items"][0] = {
        "item_id": lines[0]["item_id"], "final_hs": RECODE_HS, "change_reason": "手工自测：要素冲突，专家改码",
    }
    submission_id = submission["submission_id"]
    status, decision = call("POST", f"/submissions/{submission_id}/decisions", payload, key="p10-manual-1")
    check("整批通过（含改码）成功", status == 200 and decision.get("submission_status") == "approved", f"status={status}")
    if status != 200:
        print(json.dumps(decision, ensure_ascii=False))
        return 1

    status, replay = call("POST", f"/submissions/{submission_id}/decisions", payload, key="p10-manual-1")
    check(
        "同 key 重放同一决定",
        status == 200 and replay.get("decision_id") == decision["decision_id"] and replay.get("idempotent_replay"),
    )

    status, again = call("POST", f"/submissions/{submission_id}/decisions", payload)
    check("已处置提交再次决定返回 409", status == 409 and again["error"]["code"] == "submission_closed")

    status, after = call("GET", f"/tasks/{task['task_id']}/history")
    decisions = after["versions"][-1]["submissions"][-1]["decisions"]
    check(
        "历史保留决定与改码前后值",
        any(d["action"] == "approve" and d["items"][0]["final_hs"] == RECODE_HS for d in decisions),
    )

    status, detail = call("GET", f"/tasks/{task['task_id']}")
    check("任务阶段已通过", detail.get("stage") == "approved")

    status, card = call("GET", f"/versions/{current['version_id']}/items/{lines[0]['item_id']}")
    check(
        "卡片区分提交码与专家终值",
        card["submitted_hs"]["value"] != RECODE_HS and card["expert_final_hs"]["value"] == RECODE_HS,
        f"submitted={card['submitted_hs']['value']} final={card['expert_final_hs']['value']}",
    )

    status, operator = call("POST", "/auth/logout")
    call("GET", "/me")
    status, _ = call("POST", "/auth/login", {"username": "operator_a", "password": DEMO_PASSWORD})
    status, returned = call(
        "POST", f"/tasks/{task['task_id']}/returns",
        {"version_id": current["version_id"], "revision": detail["revision"], "issues": [{"message": "已通过后再退回"}]},
    )
    check("已通过任务不允许业务员退回", status == 409 and returned["error"]["code"] == "task_closed")

    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过" + (f"；失败：{failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
