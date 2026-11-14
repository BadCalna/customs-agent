#!/usr/bin/env python
"""P01 手工自测：对已启动的本地服务逐项验证身份、CSRF 与会话语义。

用法（先另开终端 `make dev`）：
    .venv-customs/bin/python tests/p01/manual_check.py
    BASE=http://127.0.0.1:8001 .venv-customs/bin/python tests/p01/manual_check.py

只访问 P01 发布的端点，不创建业务数据，可重复执行；全部通过时退出码为 0。
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
        print(f"无法连接 {BASE}：{exc.reason}\n请先在另一终端执行 make dev。")
        raise SystemExit(2)


def csrf_token():
    for cookie in cookies:
        if cookie.name == "csrftoken":
            return cookie.value
    return ""


def check(name, expected, actual):
    ok = expected == actual
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"（期望 {expected!r}，实际 {actual!r}）"))


def error_code(body):
    return (body.get("error") or {}).get("code")


print(f"目标服务：{BASE}（演示账号 operator_a）")

status, body = request("GET", "/api/v1/me")
check("匿名 GET /api/v1/me 返回 401", 401, status)
check("匿名错误体 code=unauthorized", "unauthorized", error_code(body))
check("错误体带 request_id", True, bool(body.get("request_id")))

status, body = request("POST", "/api/v1/auth/login", {"username": "operator_a", "password": DEMO_PASSWORD})
check("无 CSRF 登录返回 403", 403, status)
check("无 CSRF 登录错误码 csrf_failed", "csrf_failed", error_code(body))

# 未登录 GET 会种下 csrftoken；登录成功后 Django 会轮换令牌，之后必须重新读取。
request("GET", "/api/v1/me")
status, body = request(
    "POST", "/api/v1/auth/login",
    {"username": "operator_a", "password": DEMO_PASSWORD}, csrf=csrf_token(),
)
check("带 CSRF 登录返回 200", 200, status)
check("登录返回 operator 角色", "operator", body.get("role"))
check("登录返回中文角色名", "业务员", body.get("role_label"))

status, body = request("GET", "/api/v1/me")
check("登录后 GET /me 返回 200", 200, status)
check("登录后 /me 是 operator_a", "operator_a", body.get("username"))

status, body = request("POST", "/api/v1/auth/logout")
check("无 CSRF 退出返回 403", 403, status)

# 登录轮换了令牌，重新读取后再退出。
status, body = request("POST", "/api/v1/auth/logout", csrf=csrf_token())
check("带 CSRF 退出返回 200", 200, status)
check("退出返回 status=ok", "ok", body.get("status"))

status, body = request("GET", "/api/v1/me")
check("退出后 GET /me 返回 401", 401, status)

status, body = request("GET", "/api/health")
check("P00 健康接口仍为 200", 200, status)

status, body = request("GET", "/api/v1/openapi.json")
check(
    "OpenAPI 恰含三个 P01 端点",
    ["/api/v1/auth/login", "/api/v1/auth/logout", "/api/v1/me"],
    sorted(body.get("paths", {})),
)

passed = sum(results)
print(f"\n结果：{passed}/{len(results)} 项通过")
sys.exit(0 if passed == len(results) else 1)
