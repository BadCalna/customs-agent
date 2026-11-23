# install-manifest-03：基础数据中心（reference_browser）

> **本 PR 只交付未接线的独立模块和合成数据，不交付可用的生产数据中心。** 默认配置不安装此 app，也不开放页面或 API。01 组另行负责公共设置、路由、权限和上线验收；本清单并非接线完成或待合并的证明。

## 1. 应用与版本

| 项目 | 接线内容 |
|---|---|
| Django app | `apps.reference_browser`（app label `reference_browser`） |
| migration | `apps/reference_browser/migrations/0001_reference_access.py`；仅声明权限锚点，不创建业务/参考数据表 |
| 静态资源 | app static discovery；`apps/reference_browser/static/reference_browser/` |
| 模板 | `apps/reference_browser/templates/reference_browser/` |
| 只读数据 | `fixtures/snapshots.v1.json`、`fixtures/generation.v1.json`、`evidence-map.v1.jsonl`；仅 synthetic fixture |

## 2. 公共 settings 接线（01 组执行）

```python
INSTALLED_APPS += ["apps.reference_browser"]
# 生产环境不启用；仅在支持本地调试时由 01 组加入这一布尔映射。
REFERENCE_BROWSER_FIXTURE = os.environ.get("REFERENCE_BROWSER_FIXTURE") == "1"
```

本期不新增数据库连接、不读取业务库、不增加外部服务凭据。

- `REFERENCE_BROWSER_FIXTURE=1` 只有 `DEBUG=True` 时生效；仅本地调试使用。
- `REFERENCE_BROWSER_FIXTURE_TESTING=True` 仅 standalone test URLConf/测试 runner 使用。
- 两个开关都必须在部署检查中被拒绝；生产环境不得开启。
- 站点行权策略可配置 `REFERENCE_BROWSER_SCOPE_POLICY`（callable 或 dotted path）：
  `(user, *, kind, snapshot_id, dataset_key=None, record_id=None) -> bool`。策略只能收窄 Django 权限。

## 3. URL/API 接线（01 组执行）

页面 include：

```python
path("ops/data/", include("apps.reference_browser.urls"))
```

API include（`config/urls.py` 的 `api_v1`）：

```python
from apps.reference_browser.api import router as reference_browser_router
api_v1.add_router("/ops/data", reference_browser_router)
```

提供的只读端点：

- `GET /ops/data/`（`?tab=note|hs|history`）
- `GET /ops/data/evidence`
- `GET /api/v1/ops/data/snapshots`
- `GET /api/v1/ops/data/catalog`
- `GET /api/v1/ops/data/catalog/{kind}/{record_id}`
- `GET /api/v1/ops/data/hs/{hs_code}`
- `GET /api/v1/ops/data/history`
- `GET /api/v1/ops/data/history/{record_id}`
- `GET /api/v1/ops/data/evidence`

`GET /api/v1/ops/data/history/export` 仅返回拒绝并写审计，不提供导出能力。

本期**不得挂载**导入、发布、回滚等 POST 路由：`/import/runs`、`/import/{run_id}/validate`、`/import/{run_id}/publish`、`/releases/{release_id}/rollback`。

## 4. 权限 codename

migration 产生下列 `reference_browser` 权限（默认拒绝；通过用户或组显式授予，超级用户身份不自动授权）：

- `view_catalog`：注释、HS、申报要素、出口退税目录及详情。
- `view_history`：脱敏历史列表/检索。
- `view_history_detail`：历史详情、历史 evidence；详情同时要求 `view_history`。

可选后续权限（本期不创建、不挂载）：`view_evidence`、`review_reference_import`、`publish_reference_release`。Evidence 请求仍在服务端重新执行对象、快照与历史二次授权。

## 5. 数据与安全门禁

- `fx-2026-01`（`FX2026`）为当前 synthetic fixture；`fx-2025-01`（`FX2025`）用于版本 pin 回归；`fx-withdrawn` 必须对调用者统一返回 404。
- evidence map 文件名固定为 `evidence-map.v1.jsonl`；每行必须同时含 `kind/snapshot_id/source_locator/record_id/anchor/label`，禁止重复键、路径、手机号、真实业务身份或跨快照引用。
- `record_id` 使用 `[a-z0-9][a-z0-9._:-]{0,191}`；读取端不自推 hash、不跳转最新版；请求必须显式携带 `snapshot_id`。
- 历史响应只返回预脱敏 synthetic 字段；无权请求不得泄露内容、计数、存在性、locator 或版本差异；审计使用 `customs.ops.events` 的公共 `ops.event` envelope。
- fixture 不是真实来源，运行时不联网、不写业务表、不把模型输入/历史原文写入请求、日志或 URL。
- API 审计 `event_type`：`reference_browser.snapshots.read`、`reference_browser.catalog.read`、`reference_browser.history.search`、`reference_browser.history.detail`、`reference_browser.history.export`、`reference_browser.evidence.read`。HTML 证据读取和 API 共用审计入口，成功或拒绝各记一条；拒绝不写目标 ID 或原始请求参数。

## 6. 独立验证与回归命令

03 组使用独立 settings `optional_tests.ops_data.settings`、内存 SQLite 与独立 URLConf `optional_tests.ops_data.urls`。套件不进入默认 Django 发现路径。集成流水线应把下方独立命令作为单独检查，与默认全量并行运行；默认全量通过不代表独立套件通过。

```bash
python manage.py test tests
python manage.py test optional_tests.ops_data --settings=optional_tests.ops_data.settings
```

01 组接线后，使用公共配置运行 `python manage.py check --deploy --settings=config.settings`。正常部署配置应通过；另在隔离环境显式打开 fixture 开关验证检查报 `reference_browser.E001`。导入/发布/回滚未启用，故当前回归验证这些 URL 不存在，不执行真实写入或切换指针。01 组接线后另行运行完整项目测试与 OpenAPI 契约更新。

## 7. 01 组接线验收清单

- [ ] 只把 `apps.reference_browser` 加入 `INSTALLED_APPS`，不修改业务 app/model。
- [ ] 按上文分别 include 页面和 API；确认 URL 只读、无写入路由。
- [ ] 执行 `migrate` 生成三项权限；将权限分配到运维组，不默认授予所有用户。
- [ ] 保持 fixture 开关关闭于生产；执行 `check --deploy`。
- [ ] 运行上面的独立测试命令及完整项目回归。
