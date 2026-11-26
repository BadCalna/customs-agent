# Agent 调试台：安装与运行

## 专家核查调试（2026-11-25）

既有调试台新增“独立预审 / 专家核查”阶段选择，继续使用同一套连接、探针、预览票据、worker、运行记录及权限。专家结果只写入 `DebugRun`；输入是独立调试快照，不选择、修改或审批正式提交。未指定 `stage` 的既有请求保持预审。无需数据库迁移。

先在“专家核查”阶段选择内置 `expert-demo`，以 mock 验证页面和四项结果。获批的本机自填请求示例：

```json
{
  "stage": "expert_check",
  "mode": "live",
  "facts": {"name": "脱敏商品", "attributes": {}, "elements_text": "已确认的申报要素"},
  "query_basis": {"declaration_date": "2026-11-25"},
  "expert": {
    "submitted_hs": "0101210010",
    "supplier_tax_rate_raw": "",
    "direction": "export",
    "rebate_code": ""
  }
}
```

示例编码仅说明格式，不是商品归类结论。`submitted_hs` 接受 8 或 10 位 ASCII 数字；8 位保持不完整待核，不补零。日期、税则版本、贸易方向和退税代码由用户提供，不填今天或猜测业务条件。税则版本只用于 HS 与申报要素；退税和监管各自按申报日期解析其来源版本，不把 `2026` 传作退税版本 `2026C`，也不在缺日期时用今天补位。事实字段沿用既有白名单和输入检查。

贸易方向为出口时才查询和比较出口退税率。进口标记本项不适用；方向未确定则提示先确认方向。两种情况均不根据填写的退税率输出相符或冲突，也不把未执行比较伪装成查无或来源故障。

- P18 显式注入同一固定发布版本中的 HS、申报要素、退税来源；可选的 `p17_regulatory` 使用真实监管条件快照，`regulatory_code_ledger` 只作代码台账，不作为监管条件。未提供可核验来源的分项保持待核实，不回退环境默认或合成资料。
- 第一版不接图片输入，图片分项始终 `unknown`。模型不能把无图或未核实来源升级为相符。四项结论只用于辅助核查，`matched` 不等于审核通过。
- 专家预览额外返回 `stage`、`model_required`。P18 只在其原有的已核充分要素条件成立时调用语义模型；否则完成来源核对，`model_required=false`，运行出站计数为零。预览本身不发送模型请求。
- 获批样例配置 `OPS_DEBUG_SAMPLES` 可增加 `stage="expert_check"` 和上述 `expert` 对象。样例和父运行的核查编码、阶段及基准不能通过请求覆盖；复制重跑保留原输入。外部调用继续要求精确请求摘要审批与逐次确认，新阶段不能复用不同正文的旧审批。
- 详情返回 `target_hs`、`suggested_hs`、四项 `findings`、固定文案问题和来源状态，另保留退税适用范围及结构化监管结论。监管明确区分“有监管要求”“来源明确无监管要求”“待核实”，只在来源、复核、日期、方向及适用性已核实时展示措施代码和类别；模型不能编造或升级该结论。原始模型自由文本不进入摘要或报文跟踪。分项与材料通过哈希证据标识关联；只有现有资料中心支持的精确同源映射可跳转。要素、退税、监管没有对应映射时显示不可定位，不猜链接；来源材料不会冒充模型引用。
- 运行列表支持 `stage=precheck|expert_check`，旧运行视为预审。技术失败、超时和业务待核分开显示；不自动重发已尝试的模型调用。

验证入口：`python -m pytest -c optional_tests/ops_debug/pytest.ini optional_tests/ops_debug`。前端测试 `optional_tests/ops_debug/frontend.expert.cjs` 需要 Node 和 Playwright，使用本机假 API 验证阶段选择、重跑、四项展示及内容转义。真实模型质量仍需用获批真实样本和专家答案另行评测。

调试台已通过系统管理清单注册进共享应用，见 [OPS 注册与演示](../../docs/开发文档/交接/OPS-注册与演示.md)。以下保留独立环境的安装和运维说明；新增专家阶段复用该注册及运行方式，不接管正式审核或正式 Run。资料快照准入及证据映射由基础数据中心提供。本地 Web 页面不代表云端调用的数据不出机。

## 隔离环境接入（不用于生产）

1. 在项目已有的 Python 环境安装附加依赖：`uv pip install -r apps/agent_debug/requirements.txt`（使用带 pip 的虚拟环境也可运行 `python -m pip install -r apps/agent_debug/requirements.txt`）。
2. 在 `INSTALLED_APPS` 加入 `apps.agent_debug`。模块内 `apps.agent_debug.urls` 只用相对路径，由集成方挂在根 `urlpatterns` 的 `path("ops/debug/", include("apps.agent_debug.urls"))`；01 系统管理装载清单时也按相对路径解析入口。现有 v1 Ninja API 注册 `api_v1.add_router("ops/debug", agent_debug_router)`，其中 `from apps.agent_debug.api import router as agent_debug_router`；完整 API 前缀是 `/api/v1/ops/debug/`。保留项目的会话、CSRF 中间件及异常处理。即使绕开页面调用 API，写请求也须提供同会话 CSRF token。
3. `python manage.py migrate agent_debug`；`python manage.py check`。SQLite 只用于开发/隔离测试，不用于多执行器并发；共享运行环境需要 PostgreSQL 行锁。
4. 按需授权 `agent_debug.use_debug`（预览、运行、本人摘要）、`agent_debug.manage_connections`（连接/探针）、`agent_debug.view_all_runs`（叠加 `use_debug` 才能读其他人摘要）、`agent_debug.view_trace_payload`（还需该运行的读取权）。真实资料另需 `reference_browser.view_catalog` 以及 C 对对应快照和指纹的准入；缺任一项不放行。不要把上述权限与 Django staff 身份混为一谈。

## Web、worker 与清理进程配置

通过 Django settings 或受控进程环境提供；Web、worker 使用同一数据库/端点/审批与资料配置。布尔环境变量用 `true` / `false`；复合配置用 JSON。不要把密钥放进项目 `.env`、数据库、同步盘或日志，也不要在命令行粘贴真实供应商凭据。

| 配置 | 用途、缺省 |
| --- | --- |
| `OPS_DEBUG_ENABLED` / `OPS_DEBUG_LIVE_ENABLED` | 均缺省 `false`；前者启用模块，后者放行真实商品运行。首次仅开前者、端点清单留空；连接探针不是 mock，批准前不要配置/测试端点。 |
| `OPS_DEBUG_DATA_KEY` | 独立 AES-GCM 数据域密钥，预览/输入/报文加密，mock 也需要；服务密钥管理注入 URL-safe Base64 的随机 32 字节。 |
| `OPS_DEBUG_MASTER_KEY` / `OPS_DEBUG_SECRET_STORE` | 前者是独立的连接凭据域密钥（同编码，不能与数据密钥相同）。后者是由服务账户控制的本机私有绝对目录，仅存 revision 绑定的密文；目录不得位于仓库、OneDrive 或其他同步盘。无鉴权 loopback 不录供应商 Key；需要 Bearer 时缺托管就禁止保存。 |
| `OPS_DEBUG_ENDPOINTS` | 运维固定端点清单。对象包含 `id,url,models,auth_mode,target` 以及可选 `label`。仅确认不转发的本机推理允许标记 `local`，使用字面量 `127.0.0.1` / `::1`；HTTP 只限本机，外部必用经过验证的 HTTPS；不允许重定向或浏览器提供 URL。容器内 loopback 指向实际 worker 容器，必须从那里验证。 |
| `OPS_DEBUG_PUBLISHED_DIR` / `OPS_DEBUG_EVIDENCE_MAP_ROOT` | 指向受控只读资料发布根与 C 映射根；后者按 `<release_id>/evidence-map.v1.jsonl` 提供同源版本映射（无精确映射不生成证据跳转）。保留被运行引用的发布版本，不用最新版替代。 |
| `OPS_DEBUG_SNAPSHOT_ACCESS` | C 提供的 `checker(user, snapshot_id, fingerprint) -> bool` 函数或点号路径。缺失、异常或未明确返回 `True` 时拒绝。 |
| `OPS_DEBUG_LOCAL_FREE_INPUT` | 缺省 `false`，仅在运维确认本地不转发且获批后允许自由文本用于本机真实请求；外部端点永不接收自由输入。 |
| `OPS_DEBUG_SAMPLES` | 已审批样例数组，字段 `id,label,facts` 及可选 `query_basis`；`facts` 包含 `name`，可有 `attributes`、`elements_text`。内置 `demo` / `missing-material` 只是 mock，不可用于 live。 |
| `OPS_DEBUG_EXTERNAL_PACKET_DIGESTS` | `{端点 ID: [最终请求体 SHA-256, …]}`，未登记即拒发外部。逐条审查精确正文后才登记；不能给未经批准的数据或资料摘录登记。 |
| `OPS_DEBUG_USER_CONCURRENCY` / `OPS_DEBUG_INSTANCE_CONCURRENCY` / `OPS_DEBUG_OUTBOUND_PER_HOUR` | 同时开放作业每人 1、实例 2；probe/live 合计每人每滚动小时 20。只可调小。 |
| `OPS_DEBUG_TIMEOUT_SECONDS` / `OPS_DEBUG_RUN_SECONDS` | 请求 30 秒、运行 120 秒，只可调小；租约 180 秒，排队 5 分钟。 |
| `OPS_DEBUG_INPUT_RETENTION_DAYS` / `OPS_DEBUG_METADATA_RETENTION_DAYS` | 输入/报文 7 天、结论/技术元数据 90 天，只可调小；备份必须同步遵守保留政策。 |

无鉴权端点登记示例（端口、模型按实际批准配置）：

```json
[{"id":"local-reviewed","label":"已核实本机推理","url":"http://127.0.0.1:8001/v1/chat/completions","models":["model-id"],"auth_mode":"none","target":"local"}]
```

外部审批：先在隔离、受控运维环境登记端点及非敏感样例。`apps.agent_debug.services.external_packet(sample_id, endpoint_id, model)` 是离线管理辅助函数，返回最终正文且不发请求；只允许授权运维在受控环境检查，不向终端日志、工单或聊天复制正文。关务与出网负责人审查**精确正文**、目标域名和模型，确认仅含批准样例和公开摘录。随后用同模块 `external_packet_digest(sample_id, endpoint_id, model)` 计算摘要并登记，部署同版配置。样例、模型、资料、模板等改动若导致正文摘要改变，必须重审。启用 live 后，管理员在受保护页面保存 revision → 用无业务资料探针验证模型身份、鉴权和结构化回复 → 按当前默认指针版本激活；启用/撤销写入带 `expected_default_version` 的 CAS，冲突后刷新重新确认。普通用户外发前仍须逐次明确确认。

发送预览只返回技术元数据（目标类型、主机、模型、报文字节数、SHA-256、版本、确认票据和提示），不返回原报文、事实字段和值、消息、提示词或资料正文。服务端仍按精确字节生成并校验审批摘要；逐次确认和出站发送流程不变。

## 执行、审计与清理

```sh
python manage.py ops_debug_worker --watch --interval 2
python manage.py ops_debug_worker --once
python manage.py ops_debug_cleanup --dry-run
python manage.py ops_debug_cleanup
```

- 用独立服务监管 `--watch`，防止挂入正式 RQ 队列；未指定模式时处理当前队列直到空，`--once` 至多领取一项。SIGTERM/SIGINT 停止领新作业，当前作业可正常结束；为服务设置足够的优雅退出时间，强制终止时由租约清理处理。命令统计 `processed` 是领取数，不是业务成功数或对外请求数。
- 用独立调度器至少每分钟执行一次非 dry-run 清理；`--dry-run` 不写库、不投递事件，只输出计数。超时排队标记未发送；过租约中断后不重发，持久化的 dispatching 标记保留尝试记录并显示送达未知。需要重跑时重新预览、确认、创建新运行。
- 清理删除过期预览和滚动小时之外的额度记录；到期输入/报文清空密文但保留过期标记；技术元数据过期后删除 run/probe 记录及关联数据（不动连接修订或正式任务）。按 `outbox_pending` 和交付失败告警监控审计积压。
- 给 logger `customs.ops.events` 配置 INFO 级结构化采集，按 `event_id` 幂等入库；不要用只记录消息文本、不保存 `ops_event` 对象的 handler。未配置可用 INFO sink 或 handler 抛错时条目保留、后续清理自动重投。标准 logging 无远端确认，本地 `delivered_at` 表示已交给配置的 handler；采集端在未持久接收时应报告失败，防止静默丢失。已交付 outbox 受元数据保留期约束，待交付不因过期被丢弃；系统审计独立治理。

## 凭证、备份和回退

- Web、worker 使用同一批准密文目录及同版密钥注入；限定服务身份与备份访问，分开备份密钥和密文。恢复时先用隔离环境核实目录权限、对应密钥、过期清理和撤销策略；不要恢复后点开 live 或重放旧作业。运行输入或报文的备份不得比保留期存得更久。
- 轮换供应商 Key 时新建 revision、探测再切默认；泄露时立即撤销旧 revision 并在供应商侧吊销 Key。应用不自动迁移 AES 密文；切换主密钥前协调停机、退役旧版本并重新录入凭证，避免用新密钥覆盖旧密钥后误认为还能读旧密文；切换数据密钥会使未过期旧报文不可读，需先按保留政策完成计划或离线迁移。不得回退明文。密文目录中的废弃凭证按批准的运维策略单独处置，常规清理不会删除它们。
- 故障回退先关闭 `OPS_DEBUG_LIVE_ENABLED` 并停调试 worker（包括全部探针）；需要时关闭 `OPS_DEBUG_ENABLED`。按政策撤销问题 revision，运行清理标记超时，核对送达未知、费用与外发；保留系统审计及加密表，不以删库回退。正式工作台和正式队列保持原状。

## 显式运行隔离验收

在仓库根目录的项目虚拟环境中执行（无需修改共享 Django 配置）：

```sh
uv pip install -r apps/agent_debug/requirements.txt pytest pytest-django
python -m pytest -c optional_tests/ops_debug/pytest.ini optional_tests/ops_debug -q
python manage.py check --settings=optional_tests.ops_debug.settings
python manage.py makemigrations agent_debug --check --dry-run --settings=optional_tests.ops_debug.settings
```

该可选测试套件不属于默认 Django 测试发现；使用隔离配置和 SQLite，并以合成样例与假 transport 验证，不发真实供应商请求。生产鉴权、出机与关务业务效果必须在另行批准的环境核验；本地测试通过不代表可用于正式归类。
