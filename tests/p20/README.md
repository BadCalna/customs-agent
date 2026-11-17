# P20 验证入口

`make test TASK=P20`（44 项）覆盖导出适配器、API 契约与 **真实 P05 存储端到端**。

前置：`test_p05_storage_e2e.py` 的「另开文件核对内容」断言需要一个能 `import openpyxl` 的解释器
（自动探测 `CUSTOMS_OPENPYXL_PYTHON` → 当前解释器 → `python3`/`python`）；找不到时**显式失败**（不 skip），
因为少了这一层就只剩「文件能下载」的证据。Node 缺失同样显式失败（与 p07/p11 约定一致）。

- `test_exports.py`：固定结果集/模板保真/独立清单的适配器行为，全部合成资料。
- `test_export_api.py`：权限、引用一致性、执行状态、结果来源与 HTTP 头，`_read_original` 用替身。
- `test_p05_storage_e2e.py`：**不 patch `_read_original`**。真实存储根（`CUSTOMS_FILE_ROOT` 指向临时目录）
  下用真实业务模板走 `POST /api/v1/tasks`（multipart）→ inline mock 预审 → `POST /versions/{id}/exports`
  导出 `precheck` 与 `issues`，再用 openpyxl（独立解释器）另开导出文件核对页签与表头；
  另含「用 V2 的执行导 V1 → 422 `result_reference_mismatch`」的引用一致性用例。
  也可单独跑：`.venv-customs/bin/python manage.py test tests.p20.test_p05_storage_e2e`
- `test_frontend.py` + `export_client.cjs` / `export_page.cjs`：Node 执行 `web/api.js` 与 `web/pages/p06.js`
  的真实源码，验请求头、文件名解析、空/非 ZIP/敌意文件名与错误分支、「缺执行引用不发请求」。Node 缺失时**失败**不跳过。
- `test_workbench_export_guard.py`：源码守卫，锁住 workbench 原型页不再生成本地文件、不再提示导出成功。

## 真实浏览器检查（不随 `make test` 运行）

需要 Playwright（`NODE_PATH` 指向含 `playwright` 的 `node_modules`）、Chrome、以及一个隔离的 dev server：

```bash
rm -rf /tmp/p20-browser && mkdir -p /tmp/p20-browser/files
CUSTOMS_SQLITE_PATH=/tmp/p20-browser/db.sqlite3 CUSTOMS_FILE_ROOT=/tmp/p20-browser/files \
  .venv-customs/bin/python manage.py migrate --noinput
CUSTOMS_SQLITE_PATH=/tmp/p20-browser/db.sqlite3 CUSTOMS_FILE_ROOT=/tmp/p20-browser/files \
  .venv-customs/bin/python manage.py seed_demo
CUSTOMS_SQLITE_PATH=/tmp/p20-browser/db.sqlite3 CUSTOMS_FILE_ROOT=/tmp/p20-browser/files \
  .venv-customs/bin/python manage.py runserver 127.0.0.1:8011 --noreload

PW=$(python3 -c "import re;print(re.search(r'DEMO_PASSWORD = \"(.+?)\"', open('apps/accounts/management/commands/seed_demo.py').read()).group(1))")
BASE=http://127.0.0.1:8011 NODE_PATH=<node_modules> P20_PASSWORD="$PW" \
  P20_OPENPYXL_PYTHON=/usr/bin/python3 node tests/p20/browser_check.cjs
BASE=http://127.0.0.1:8011 NODE_PATH=<node_modules> P20_PASSWORD="$PW" node tests/p20/workbench_check.cjs
```

- `browser_check.cjs`：供应商真实上传真实模板 → 业务员认领 → 在 `/uploads/` 点两个导出按钮，浏览器真实下载
  `预审结果-测试结果.xlsx` / `独立补料清单-测试结果.xlsx`，用 openpyxl 另开，并核对原件 sha256 不变。
- `workbench_check.cjs`：`/workbench/` 的 FIXTURE 模式点击导出只提示样例不可导出、无请求无下载；
  LIVE 模式的导出控件禁用并指向 `/uploads/`、`/tasks/`。

## 已知限制（不属于本包改动）

- `/workbench/?mode=live` 打开任务详情会在 `curVersion()` 处抛 `TypeError`：LIVE 数据桥只读 `/tasks` 列表，
  而 `TaskListEntry` 不含 `versions`（`TaskDetail` 才有）。这是原型页既有缺陷，P20 未改该数据桥。
- 导出文件在 WPS / Excel 中的打开与重开未验收；`manifest` 里 `client_wps` / `client_excel` 仍为 `pending`。
- 真实退税与监管文库仍未取得（D-09），导出内容不含真实归类依据。

## 已知证据边界（2026-11-17 对抗审查确认）

- `browser_check.cjs`（真实浏览器 18 项）会真实上传、认领、下载两个 xlsx 并独立打开校验；
  但它的「原件 sha256 不变」是「上传前本地样本 vs 两次导出后从服务端下载的原件」的**间接**比对，
  没有在导出前对服务端副本取样。逐次前后取样只在 `test_p05_storage_e2e.py` 里有（`storage_files()` + `stored.read_bytes()`）。
- `workbench_check.cjs` 现在把「因既有数据桥缺陷未执行」的 LIVE 任务头断言单列成 NOTE，
  不再把未执行项加进 PASS 数字。该缺陷（`/workbench/?mode=live` 打开任务详情抛
  `TypeError: Cannot read properties of undefined (reading 'v')`）在 `origin/main` 同样存在，**不是本轮引入**；
  它意味着 workbench 的 LIVE 导出控件目前只能在页面函数级被断言，而不是在真实任务详情里。
- 导出文件名做了收敛（不含 `/`、`\`、`..`、控制字符，长度 ≤120，字符集白名单），
  非法名字直接报 `missing_export_filename` 而不是落盘；200 + 空 body 或非 ZIP 头也会被拒（`empty_export_body` /
  `unexpected_export_type`）。后端目前恒写 xlsx，这些是纵深防御，不是已发生故障的复现。
