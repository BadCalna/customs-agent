#!/usr/bin/env python
"""P02 手工自测：对已启动的本地服务验证契约端点与 FIX 场景。

前置（在 P02 工作区）：
    make migrate && make seed-demo && make seed-contracts && make dev
用法：
    .venv-customs/bin/python tests/p02/manual_check.py
    BASE=http://127.0.0.1:8766 .venv-customs/bin/python tests/p02/manual_check.py

只读查询与未实现端点的 501 响应，不创建业务数据，可重复执行。
"""
import http.cookiejar
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = os.environ.get("BASE", "http://127.0.0.1:8000").rstrip("/")
DEMO_PASSWORD = re.search(
    r'DEMO_PASSWORD = "(.*)"',
    (ROOT / "apps/accounts/management/commands/seed_demo.py").read_text(),
).group(1)

cookies = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
results = []


def request(method, path, body=None, csrf=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if csrf:
        req.add_header("X-CSRFToken", csrf)
    try:
        with opener.open(req) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return exc.code, {"_raw": raw.decode(errors="replace")}
    except urllib.error.URLError as exc:
        print(f"无法连接 {BASE}：{exc.reason}\n请先执行 make dev，并确认已 seed-contracts。")
        raise SystemExit(2)


def csrf_token():
    for cookie in cookies:
        if cookie.name == "csrftoken":
            return cookie.value
    return ""


def check(name, expected, actual):
    ok = expected == actual
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n      期望 {expected!r}\n      实际 {actual!r}"))


def error_code(body):
    return (body.get("error") or {}).get("code")


def login(username):
    request("GET", "/api/v1/me")
    status, body = request(
        "POST", "/api/v1/auth/login", {"username": username, "password": DEMO_PASSWORD}, csrf=csrf_token(),
    )
    check(f"{username} 登录成功", 200, status)
    return body


def tasks_by_title():
    status, body = request("GET", "/api/v1/tasks", )
    check("GET /tasks 返回 200", 200, status)
    return {entry["title"]: entry for entry in body.get("items", [])}


def item_by_row(version_id, row):
    status, body = request("GET", f"/api/v1/versions/{version_id}/items")
    check("GET 商品列表返回 200", 200, status)
    for entry in body.get("items", []):
        if entry["source_location"]["row_number"] == row:
            return entry
    raise SystemExit(f"版本 {version_id} 中找不到第 {row} 行")


print(f"目标服务：{BASE}")

status, body = request("GET", "/api/v1/tasks")
check("匿名 GET /tasks 返回 401", 401, status)
check("匿名错误码 unauthorized", "unauthorized", error_code(body))

login("operator_a")
tasks = tasks_by_title()
check("业务员可见 3 个任务", 3, len(tasks))
main = tasks.get("契约演示-主流程")
recode = tasks.get("契约演示-专家改码")
if main is None or recode is None:
    raise SystemExit("缺少契约演示任务，请先执行 make seed-contracts")
check("任务带 mock 结果模式", "mock", main["result_mode"])
check("任务阶段为待业务员确认", "awaiting_confirmation", main["stage"])
check("当前版本为 V2", 2, main["current_version_number"])

status, detail = request("GET", f"/api/v1/tasks/{main['task_id']}")
check("任务详情返回 200", 200, status)
check("任务含两个版本", 2, len(detail["versions"]))
check("V1 标记为历史版本", False, [v for v in detail["versions"] if v["number"] == 1][0]["is_current"])
check("允许动作含提交", True, "submission.create" in detail["allowed_actions"])

status, page = request("GET", "/api/v1/tasks?limit=2")
check("分页返回 2 条", 2, len(page["items"]))
check("分页有下一页游标", True, bool(page["next_cursor"]))
status, page2 = request("GET", f"/api/v1/tasks?limit=2&cursor={page['next_cursor']}")
check("第二页不重复", True, {i["task_id"] for i in page["items"]}.isdisjoint({i["task_id"] for i in page2["items"]}))
status, body = request("GET", "/api/v1/tasks?limit=999")
check("超限 limit 返回 400", 400, status)
status, body = request("GET", "/api/v1/tasks?stage=nonsense")
check("非法阶段返回 400", 400, status)

v2 = main["current_version_id"]
rows = {}
status, body = request("GET", f"/api/v1/versions/{v2}/items?limit=50")
check("V2 商品共 8 行", 8, len(body["items"]))
for entry in body["items"]:
    rows[entry["source_location"]["row_number"]] = entry
check("FIX-01 一致", "consistent", rows[5]["comparison_status"])
check("FIX-01 不一致", "inconsistent", rows[6]["comparison_status"])
check("FIX-02 缺事实待确认", "needs_confirmation", rows[7]["comparison_status"])
check("FIX-02 建议为 null", None, rows[7]["precheck_suggested_hs"])
check("FIX-03 附图读取失败", "read_failed", rows[8]["image_status"])
check("FIX-04 原码空白保留", "", rows[9]["supplier_hs_raw"])
check("FIX-04 原码缺失", "supplier_missing", rows[9]["comparison_status"])
check("FIX-04 原码异常", "supplier_invalid", rows[10]["comparison_status"])
check("FIX-04 异常原文保留", "ABC", rows[10]["supplier_hs_raw"])
check("FIX-06 查询失败待确认", "needs_confirmation", rows[12]["comparison_status"])
check("FIX-06 无置信度", None, rows[12].get("confidence"))

card7 = request("GET", f"/api/v1/versions/{v2}/items/{rows[7]['item_id']}")[1]
check("卡片缺用途问题具体", "MISSING_USAGE", card7["issues"][0]["code"])
check("卡片证据状态 not_found", "not_found", card7["evidence_status"])
check("卡片阶段值带执行引用", rows[7]["latest_run_id"], card7["precheck_suggested_hs"]["source_id"])
check("卡片业务确认值为 null", None, card7["business_confirmed_hs"]["value"])
check("卡片结果为 mock", "mock", card7["result_mode"])

card8 = request("GET", f"/api/v1/versions/{v2}/items/{rows[8]['item_id']}")[1]
check("图片失败保留源单元格", "H8", card8["images"][0]["source_cell"])
check("图片失败原因明确", True, bool(card8["images"][0]["error_message"]))

run_id = rows[11]["latest_run_id"]
status, run = request("GET", f"/api/v1/runs/{run_id}")
check("执行详情返回 200", 200, status)
check("FIX-05 进度总数 8", 8, run["progress"]["total"])
check("FIX-05 技术失败 1 行", 1, run["progress"]["failed"])
check("FIX-05 成功 7 行", 7, run["progress"]["succeeded"])
failed_row = [i for i in run["items"] if i["status"] == "failed"][0]
check("失败行有技术错误原因", True, bool(failed_row["error_message"]))
check("失败行不冒充业务结论", "", failed_row.get("comparison_status") or "")
check("执行标记为当前有效", True, run["is_current"])

v1 = [v for v in detail["versions"] if v["number"] == 1][0]
v1_items = request("GET", f"/api/v1/versions/{v1['version_id']}/items")[1]["items"]
v1_run = request("GET", f"/api/v1/runs/{v1_items[0]['latest_run_id']}")[1]
check("FIX-09 V1 执行仍可回查", 200, request("GET", f"/api/v1/runs/{v1_items[0]['latest_run_id']}")[0])
check("FIX-09 V1 执行在该版本内仍有效", True, v1_run["is_current"])
check("FIX-09 V1 与 V2 是不同执行", True, v1_run["run_id"] != run_id)
check("FIX-09 重传产生新的商品行", True, v1_items[0]["item_id"] != rows[5]["item_id"])

recode_detail = request("GET", f"/api/v1/tasks/{recode['task_id']}")[1]
recode_version = recode_detail["current_version_id"]
recode_items = request("GET", f"/api/v1/versions/{recode_version}/items")[1]["items"]
recode_card = request("GET", f"/api/v1/versions/{recode_version}/items/{recode_items[0]['item_id']}")[1]
check("FIX-10 提交码保留", recode_card["submitted_hs"]["value"], recode_card["latest_expert_check"]["target_hs"])
check("FIX-10 专家终值已改码", True, recode_card["expert_final_hs"]["value"] != recode_card["submitted_hs"]["value"])
check("FIX-10 改码有决定引用", True, bool(recode_card["expert_final_hs"]["source_id"]))
check("FIX-10 旧核查针对提交码", recode_card["submitted_hs"]["value"], recode_card["latest_expert_check"]["target_hs"])
check("FIX-10 任务已通过", "approved", recode_card["task_stage"])

status, body = request("POST", "/api/v1/tasks", {})
check("写请求缺 CSRF 返回 403", 403, status)
check("写请求缺 CSRF 错误码", "csrf_failed", error_code(body))
status, body = request("POST", "/api/v1/tasks", {}, csrf=csrf_token())
check("未实现写端点返回 501", 501, status)
check("未实现错误码 not_implemented", "not_implemented", error_code(body))
status, body = request("GET", f"/api/v1/tasks/{main['task_id']}/history")
check("历史端点显式 501", 501, status)
status, body = request(
    "PATCH", f"/api/v1/versions/{v2}/items/{rows[5]['item_id']}/confirmation", {}, csrf=csrf_token(),
)
check("确认端点显式 501", 501, status)

before = request("GET", f"/api/v1/tasks/{main['task_id']}")[1]
request("GET", f"/api/v1/versions/{v2}/items?limit=50")
after = request("GET", f"/api/v1/tasks/{main['task_id']}")[1]
check("GET 不推进任务修订", before["revision"], after["revision"])
check("GET 不改变最新执行引用", before["latest_run_id"], after["latest_run_id"])

login("supplier_b")
status, body = request("GET", f"/api/v1/tasks/{main['task_id']}")
check("FIX-07 越权读取返回 404", 404, status)
check("FIX-07 越权错误码 not_found", "not_found", error_code(body))
status, body = request("GET", f"/api/v1/versions/{v2}/items")
check("FIX-07 越权读商品返回 404", 404, status)
supplier_b_tasks = tasks_by_title()
check("FIX-07 供应商乙只见自己任务", 1, len(supplier_b_tasks))

passed = sum(results)
print(f"\n结果：{passed}/{len(results)} 项通过")
sys.exit(0 if passed == len(results) else 1)
