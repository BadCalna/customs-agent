# 契约 v1 发布记录

版本：`1.1.0`（`apps.api.schemas.CONTRACT_VERSION`）
发布：2026-11-14，任务包 P02（分支 `feature/P02-contracts-mock`）。
状态：只读查询、P06 上传/认领、P09/P10 审批写入、P20 结果导出与历史记录 DTO 已发布；仍为显式 501 的端点仅剩 P13 重试/重跑与 P19 站内消息，见下表。

本目录是 D3《接口与页面约定》、D4《Agent 输入输出与证据约定》的可校验发布物。
**唯一的字段来源是 Python schema**，本目录文件全部由命令生成，不要手工编辑。

## 目录

| 路径 | 内容 | 代码来源 |
|---|---|---|
| `schemas/common/` | 错误体、源位置、阶段值、问题、证据、附图、执行进度 | `apps/api/schemas.py` |
| `schemas/expert/` | 专家分项意见与核查引用 | `apps/api/schemas.py` |
| `schemas/tasks/` | 任务列表、任务详情、版本条目 | `apps/reviews/schemas.py` |
| `schemas/items/` | 商品列表、商品卡片、事实组 | `apps/reviews/schemas.py` |
| `schemas/runs/` | 执行详情、逐行结果 | `apps/runs/schemas.py` |
| `schemas/engine/` | 两阶段引擎输入输出 DTO | `adapters/engines/protocols.py` |
| `schemas/history/` | 历史记录 DTO、检索输入/候选/结果与来源引用（1.1.0 新增） | `adapters/sources/history_search.py` |
| `schemas/fixtures/` | 固定样例文档结构 | `apps/api/fixtures.py` |
| `openapi/v1.json` | 由 `api_v1.get_openapi_schema()` 生成 | `config/urls.py` |
| `fixtures/FIX-01.json` ~ `FIX-10.json` | D3 第 4 节场景样例 | 由演示数据真实序列化生成 |
| `fixtures/DEMO-SET.json` | 组件演示壳 fixture 模式的完整数据集（任务/版本商品/卡片/执行） | `export_contracts.py::build_demo_set`，与 live 端点同构 |

## 生成与校验

```sh
make seed-demo         # 演示账号（幂等）
make seed-contracts    # 契约演示任务、V1/V2、商品与 mock 执行（幂等）
make contracts         # 重新生成 schemas、openapi；有演示数据时同时刷新 fixtures 与 DEMO-SET
make demo-components   # seed-contracts + contracts + 启动服务，打开 /demo/ 看组件
make test-contracts    # schema 漂移、OpenAPI 漂移、枚举对齐、样例校验、端点行为
make test TASK=P02     # 与上一条同入口，59 项
```

修改任何 schema、枚举或端点后必须重新执行 `make contracts` 并提交差异；
`make test-contracts` 会在漂移时失败。fixture 的 `null` 字段不得删除——
“未产生的阶段值为 `null`”是契约语义。

`fixtures/DEMO-SET.json` 被 `web/api.js` 的 fixture adapter 直接读取，
`tests/p02/test_web_shell.py` 会用 live 端点响应做逐字段等值断言：改了序列化器
却不重导 DEMO-SET，测试会失败。它不保存生成时间，同一份演示数据重复导出字节一致。

## 关键口径

| 维度 | 取值 | 说明 |
|---|---|---|
| 比较结论 `comparison_status` | `association_error` / `supplier_missing` / `supplier_invalid` / `needs_confirmation` / `consistent` / `inconsistent` | D4 六类；`null` 表示尚未得出结论。判定顺序见 `adapters/engines/mock.py::compare_original` |
| 证据 `evidence_status` | `obtained` / `not_found` / `query_failed` / `version_unverified` | 查询失败与确实未查得分开；`null` 表示未查询 |
| 附图 `image_status` | `available` / `absent` / `read_failed` | 任一读取失败即整体 `read_failed`，读不出不得当无图 |
| 执行状态 `status` | `queued` / `running` / `succeeded` / `failed` | 技术维度；部分行失败看 `progress.failed`，不改写执行状态 |
| 结果模式 `result_mode` | `mock` / `live` | mock 结果必须在页面可识别 |
| 阶段值 `StageValue` | `{value, source_id}` | 值为 `null` 时仍保留产生它的 run/submission/decision 引用 |
| 当前执行 `is_current` | 按**文件版本 + 执行类型**维护 | V1 的执行在 V1 版本内仍可回查，不被 V2 顶掉；同版本重跑才转为历史 |
| 分页 | `{items, next_cursor}` | 游标为不透明字符串，`null` 表示无下一页；`limit` 默认 20、上限 100 |
| 错误 | `{error:{code,message,details}, request_id}` | 401/403/404 沿用 D3 第 6 节；新增 400 参数错误、501 未实现 |

## 样例索引

| 文件 | 场景 | 主要内容 |
|---|---|---|
| `FIX-01.json` | 正常建议 | 一致卡片 + 不一致对照卡片，证据 obtained，带 mock 标记 |
| `FIX-02.json` | 缺关键事实 | 建议为 `null`、问题指出缺用途、证据 not_found |
| `FIX-03.json` | 图片失败 | `read_failed` 卡片 + 确无图片 `absent` 对照 |
| `FIX-04.json` | 原码异常/未填 | 空白原值 `supplier_missing` + 异常原文 `supplier_invalid` |
| `FIX-05.json` | 部分运行失败 | 8 行执行：成功 7、技术失败 1，失败行带错误原因 |
| `FIX-06.json` | 查询失败 | `query_failed`、置信度 `null`，不出现“已核实” |
| `FIX-07.json` | 空列表/无权 | 200 空分页 + 404 错误体，两者分开 |
| `FIX-08.json` | 修订冲突 | 409 `conflict` 错误体形状；实际冲突检测由 P09/P10 实现 |
| `FIX-09.json` | V1 延迟返回 | V1 执行仍可回查；前端按 `version_id` 丢弃晚到旧响应 |
| `FIX-10.json` | 专家改码 | 提交码、专家终值、改码原因、旧核查目标分别可见 |

## 已实现与未实现端点

已实现（全部 `auth=session_auth`，GET 无副作用；以 `contracts/openapi/v1.json` 为准）：

- 身份：`POST /auth/login`、`POST /auth/logout`、`GET /me`
- 任务读取：`GET /tasks`、`GET /tasks/{task_id}`、`GET /tasks/claimable`、`GET /tasks/{task_id}/history`
- 版本与商品读取：`GET /versions/{version_id}/import`、`GET /versions/{version_id}/items`、
  `GET /versions/{version_id}/items/{item_id}`、`GET /runs/{run_id}`
- 文件下载：`GET /files/{file_id}`
- 写入：`POST /tasks`（P06 上传）、`POST /tasks/{task_id}/versions`（P06 补料）、
  `POST /tasks/{task_id}/claim`（P06 认领）、`PATCH /versions/{version_id}/items/{item_id}/confirmation`（P09）、
  `POST /tasks/{task_id}/submissions`（P09）、`POST /submissions/{submission_id}/withdraw`（P09）、
  `POST /submissions/{submission_id}/decisions`（P10）、`POST /tasks/{task_id}/returns`（P10）、
  `POST /versions/{version_id}/exports`（P20，返回 xlsx 文件流，见下）
- 导出响应口径（P20）：`Content-Type` 为 xlsx，`Content-Disposition` 为附件文件名，
  另带 `X-Export-Run`（本次执行）与 `X-Result-Mode`（`mock`/`live`）响应头；
  结果集必须属于请求版本指定的执行，不属于即 422 `result_reference_mismatch`。

显式 501（`error.code=not_implemented`，`details.owner` 标交付任务包，见 `apps/api/not_implemented.py`）：

- `POST /versions/{version_id}/prechecks`（P13）
- `POST /runs/{run_id}/retries`（P13）
- `GET /messages`、`PATCH /messages/{message_id}`（P19）

`Idempotency-Key` 的持久化幂等已随 P09/P10 落地（同 key 同对象重放、不同内容 409、不同目标冲突）；
FIX-08 样例仍只描述错误形状，不代表所有写端点都已覆盖幂等。

> 2026-11-17 修订：此前本节把 P06/P09/P10/P20 的端点都写成「显式 501」，与代码不符。
> 本次按 `api_v1.get_openapi_schema()` 的实际 path 重写，并删掉 P20 导出占位。

## 1.1.0 变更（2026-11-15）

- 新增 `schemas/history/`：`HistoryRecord`、`HistoryQuery`、`HistoryCandidate`、
  `HistorySearchResult`、`HistorySourceRef`、`HistoryDatasetInfo`，供 P16 依赖公共结构而不 import 适配器内部类。
- 仅新增，不改既有字段与端点；`contract_version` 同步升为 `1.1.0`（FIX 样例与 DEMO-SET 已更新）。

P06（2026-11-17）增量发布：`schemas/imports/` 与 OpenAPI 已包含真实 multipart 上传、补料版本、待认领池、
认领、导入报告与受控文件读取。DTO 来源 `apps/reviews/import_schemas.py`；具体语义和错误见 D3 第 10 节。
`POST /tasks` 和 `POST /tasks/{id}/versions` 已替换 501，占位端点测试同步更新。

## 1.1.0 增补（2026-11-17，P15 历史模板导入）

- `HistoryCandidate` 新增两个可选字段：`needs_review: bool = false`、
  `blocking_issue_codes: list[str] = []`。导入层把 P04 行级 blocking/error
  问题透传到检索候选与证据；`needs_review=true` 只表示“记录取到了但待核”，
  不改变 `outcome` 原值，也不能被下游当成成功案例直接采用。
- `HistorySearchResult` 新增 `excluded_needs_review: int = 0`，表示本次检索因
  `needs_review=true` 而未纳入成功案例的记录数；默认检索排除待核异常记录，
  需要审计时可构造 `HistorySearchSource(include_needs_review=True)`。
- 纯新增可选字段，未改既有字段、端点或 `CONTRACT_VERSION`（仍为 `1.1.0`）。
