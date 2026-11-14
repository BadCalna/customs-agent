# 契约 v1 发布记录

版本：`1.0.0`（`apps.api.schemas.CONTRACT_VERSION`）
发布：2026-11-14，任务包 P02（分支 `feature/P02-contracts-mock`）。
状态：只读查询与 mock 引擎边界已发布；写动作端点仍为显式 501，见下表。

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
| `schemas/fixtures/` | 固定样例文档结构 | `apps/api/fixtures.py` |
| `openapi/v1.json` | 由 `api_v1.get_openapi_schema()` 生成 | `config/urls.py` |
| `fixtures/FIX-01.json` ~ `FIX-10.json` | D3 第 4 节场景样例 | 由演示数据真实序列化生成 |

## 生成与校验

```sh
make seed-demo         # 演示账号（幂等）
make seed-contracts    # 契约演示任务、V1/V2、商品与 mock 执行（幂等）
make contracts         # 重新生成 schemas、openapi；有演示数据时同时刷新 fixtures
make test-contracts    # schema 漂移、OpenAPI 漂移、枚举对齐、样例校验、端点行为
make test TASK=P02     # 与上一条同入口，42 项
```

修改任何 schema、枚举或端点后必须重新执行 `make contracts` 并提交差异；
`make test-contracts` 会在漂移时失败。fixture 的 `null` 字段不得删除——
“未产生的阶段值为 `null`”是契约语义。

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

已实现（全部 `auth=session_auth`，GET 无副作用）：

- `GET /api/v1/tasks`、`GET /api/v1/tasks/{task_id}`
- `GET /api/v1/versions/{version_id}/items`、`GET /api/v1/versions/{version_id}/items/{item_id}`
- `GET /api/v1/runs/{run_id}`

显式 501（`error.code=not_implemented`，`details.owner` 标交付任务包）：
上传与版本创建（P06）、重跑与重试（P13）、确认与提交（P09）、退回与决定及历史（P10）、
导出（P20）、受控文件下载（P05）、站内消息（P19）。
`POST /tasks` 与 `GET /tasks` 同路径，占位注册在 `apps/reviews/api/read.py`，
其余在 `apps/api/not_implemented.py`。

写动作的 `Idempotency-Key` 持久化与 409 冲突检测**尚未实现**，只有错误形状样例；
FIX-08 不代表重试去重已可用。
