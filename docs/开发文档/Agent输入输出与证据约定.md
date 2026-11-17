# D4 Agent 输入输出与证据约定

版本：0.5，2026-11-17。状态：P02 第一批已发布引擎协议与 mock 实现；P14 已发布 HS 与申报要素来源适配契约（第 6 节，合成样本自验，真实官方来源未接入）；P17 已发布退税与监管来源适配契约（第 7 节，同样是合成样本自验）；P06 已补充 inline mock 接线与独立推荐输入隔离说明（文末）。协议 DTO 在 `adapters/engines/protocols.py`，固定场景实现与比较优先级在 `adapters/engines/mock.py`，JSON Schema 见 `contracts/schemas/engine/`。本文件规定系统内关务 AI 引擎；编程 Agent 的分工见 [协作与交接约定](Agent协作与交接约定.md)。

## 1. 引擎边界

| 能力 | 输入 | 输出 | 不可执行的业务动作 |
|---|---|---|---|
| 独立 HS 推荐 | 版本化商品事实、图片引用、查询基准和可用证据 | 候选、理由、缺口、依据和置信度 | 不接收供应商原 HS 作为推荐条件；不修改原值、提交或批准 |
| 原码比较 | 独立推荐结果 + 供应商原 HS + 有效性/关联信息 | 六类比较结论及原因 | 不改推荐的原始记录来追平供应商码 |
| 专家 AI 核查 | 不可变提交快照、业务员提交 HS、事实/图片/相关证据 | 要素、图片、退税、监管分项意见和问题 | 不选择最终退回对象、不人工审批、不覆盖专家终值 |

业务服务构造输入、校验输出、按 run/version/submission 落库。引擎只返回结构化结果；模型返回了数据库 ID 也不能据此选择写入目标。任务调度、权限、重试和审计不藏在提示词中。

## 2. 最小数据契约

共有执行上下文：`task_id, version_id, item_id, run_id, input_revision, input_hash, engine_version, prompt_version, rules_version, result_mode`；专家核查另带 `submission_id` 及 `submitted_hs`。标识由服务端注入。时间使用带时区格式；秘密配置、完整路径和账号凭据不进入模型输入。

独立推荐使用精简 DTO（只含该阶段需要的字段），不把含原码的整张表或完整前端卡片传给模型。事实原文内可能自然出现编码时，输入构造器记录来源并按约定处理；不能声称仅移除一个字段就彻底消除了锚定风险。

输出分为以下互相独立的字段：

| 维度 | 建议枚举/结构 | 要求 |
|---|---|---|
| 执行 | `queued/running/succeeded/partially_failed/failed/superseded` | 技术状态由执行服务维护；被新执行替代的结果仍保留 |
| 比较 | `consistent/inconsistent/needs_confirmation/supplier_missing/supplier_invalid/association_error` | 同码缺决定性事实或证据仍为 needs_confirmation |
| 证据 | `obtained/not_found/query_failed/version_unverified` | 查询失败与确实未查得分开；字段支持逐来源状态 |
| 置信度 | 标签、理由、评测/校准版本 | 无可验证校准时不伪装为准确率或客观概率；预审和专家核查分别记录 |
| 候选 | `suggested_hs`、候选列表和理由 | 不确定可为 null，不填假码凑格式 |
| 问题 | code、描述、关联字段、缺什么、建议责任角色 | 具体到商品；最终责任人由业务服务/人工确定 |
| 专家分项 | 元素/图片/退税/监管各自的 matched/conflict/unknown、原因、依据 | unknown 不能当成无风险；旧核查明确显示针对哪个 HS |

主比较结论按“关联异常 → 原码缺失/格式或已证实有效性异常 → 资料或证据不足 → 可比较的一致/不一致”判定；其余问题保留在 issues 中。来源不可达只能记待核，不据此宣称编码无效。P02/P16 用显式场景验证优先级，不由不同 Agent 各写一套判断。

固定 mock 输出示例（字段最小子集，完整 schema 由 P02 建立）：

```json
{
  "task_id": "T001",
  "version_id": "V002",
  "item_id": "I007",
  "run_id": "R003",
  "result_mode": "mock",
  "run_status": "succeeded",
  "comparison_status": "needs_confirmation",
  "suggested_hs": null,
  "issues": [{"code": "MISSING_USAGE", "message": "请补充具体用途"}],
  "evidence_status": "not_found",
  "evidence": []
}
```

mock 的固定结果由测试场景选择，不依赖随机数或请求轮询次数。涉及真实编码格式的测试可使用明确标注的合成格式样本，不把它们描述为真实归类结论。

P02 第一批落地情况：

- 场景由 `ExecutionContext.mock_scenario` 显式选择，已登记 `normal`、`missing_facts`、`image_failed`、`query_failed`、`tech_failure`、`association_error`、`supplier_invalid`、`expert_conflict`；未登记的场景显式报错，不随意成功。
- 比较结论的判定顺序只有 `adapters/engines/mock.py::compare_original` 一份实现，P16 真实引擎应复用。
- 技术失败用 `EngineTechnicalError` 表达，落库为行级 `status=failed` 与 `error_message`，不与业务待确认混用。
- 上面的 JSON 示例字段名与正式契约一致，但正式结构以 `contracts/schemas/engine/` 为准；示例中的 `T001` 等标识在真实响应里是 UUID。

## 3. 证据结构

每条证据至少保留：证据 ID、来源类型、标题、URL 或文档标识、条款/页码/章节定位、必要摘录、取得时间、来源发布/生效/失效时间（未知则空）、适用范围、查询参数、查询状态、关联判断。历史参考另保存历史业务日期及与当前商品的差异。

系统不填写拟申报日期。资料没有适用日期时，查询基准和仍需专业确认的部分单独保存；不能用查询日冒充业务申报日。过期、版本未知和第三方线索不能标作已核实的现行官方依据。

图片只能支持可见事实；读图失败和无图分别呈现，不推断图片看不到的工艺、材质或税务条件。查询没有结果就留空和状态，不生成貌似可点击的虚构条款。

## 4. 自动执行、重试和过期保护

1. 原文件、商品及映射成功持久化后，在同一业务提交中登记待运行记录；事务成功后通知执行器。解析致命错误不发布成功事件；可定位的行级缺口保留在商品并按发布契约进入待确认场景。
2. 业务员提交成功时建立不可变快照和专家核查待运行记录。重复提交使用幂等控制，不创建两个有效提交或重复审批。
3. P13 在 PostgreSQL 上实现待派发记录重投、有限重试及 worker 中断恢复。单独使用事务提交回调不足以保证数据库提交后进程崩溃时不丢派发，须有可重扫的持久待运行记录。
4. worker 只接收执行 ID，从服务端读取固定输入。每个结果校验 task/version/item/submission、输入修订及执行代次。V1 晚到、同版本旧一轮晚到均不能覆盖当前有效结果。
5. 单行失败允许单行重试，保留每次尝试、错误分类和时间；模型超时、限流、来源查询失败及业务缺事实分别记录。主流程不用一个失败字段表达全部问题。

M0/M1 的 inline mock 只验证短时顺序流程；在 PostgreSQL + RQ 下完成 P12/P13 之前，不能声称后台可恢复或并发正确。

## 5. 评测与回交

引擎 Agent 回交：输入/输出 schema 版本、运行配置版本、固定脱敏样例、实际结果、来源记录、模型/提示词版本、失败/未知样本及复跑命令。每次变更对同一批固定样本复跑，记录差异和解释，不仅汇报总体一致率。

业务评测同时检查编码、关键补料问题、引用是否真实可定位、置信度是否有依据、错误是否被明确暴露。专家预期与模型生成结果分开保存；100～200 行或某个 8 位一致率都不能单独证明全部商品可靠。阈值和样本覆盖由 Jay/专家在真实评测前确定，未确认时写“质量门槛待定”。

主控检查契约与落库，验收 Agent 检查流程与异常，关务专家检查专业答案。相关任务：P02、P13～P18、P21。

## 6. HS 与申报要素来源适配（P14 发布，2026-11-16）

### 6.1 边界与入口

P14 是**事实来源**，不是归类引擎。它只回答两件事：

1. 某个编码在某个税则版本里是什么、状态如何（`adapters/sources/hs_lookup.py`）；
2. 该编码在该版本要求哪些申报要素，现有事实是否充足（`adapters/sources/declaration_elements.py`）。

两个入口都只接收编码与查询基准，**不接收品名去猜码，不产生 `suggested_hs`，不生成要素值，不落库、不调用模型、不访问网络**。数据由可替换存储注入；默认读环境变量指向的本地 JSON，未配置就明确返回来源未配置。

| 入口 | 输入 | 输出 | 典型调用 |
|---|---|---|---|
| `HsLookupSource.lookup(HsQuery)` | 编码原文、`QueryBasis` | 格式分类、版本解析、税则记录、四态、问题、证据 | 校验推荐/提交/专家改码后的编码是否真实存在 |
| `DeclarationElementsSource.check(ElementQuery)` | 编码、`QueryBasis`、结构化要素和/或要素原文 | 清单、逐项核对、`sufficiency`、待补项、四态、问题、证据 | 预审/专家核查时判断“还缺哪些事实” |

`QueryBasis` 只有 `declaration_date`、`tariff_version`、`note` 三个字段。**系统不填拟申报日期**：两个基准都为空时版本一律 `unverified`，不得用查询当天顶替；调用方必须把申报要素表里的业务日期或明确税则版本传进来。

### 6.2 状态必须分开

查询技术状态与 D4 证据四态分开，格式、存在性、有效性、版本也分开：

| 情形 | `query_status` | `evidence_status` | 编码状态 |
|---|---|---|---|
| 输入为空/非数字/长度不对 | `invalid_input` | `null`（未发起查询） | 不判断 |
| 来源未配置/文件读不了/存储异常 | `source_unconfigured` / `source_unreadable` / `source_error` | `query_failed` | 不判断 |
| 查到 + 版本已核 | `ok` | `obtained` | 来源声明的 `active/expired/merged/unknown` |
| 查到 + 版本未核 | `ok` | `version_unverified` | 同上，但不得作为申报依据 |
| 本版本未收录 + 版本已核 | `ok` | `not_found` | `not_found` |
| 未收录 + 版本未核 | `ok` | `version_unverified` | `unknown`，不得宣称该码不存在 |

- 8 位数字是税目/子目（`subheading_only`），不是错码；仅当来源收录 8 位记录时可查该记录，不按 10 位前缀外推层级，也不能确认 10 位申报编码，`declaration_ready=false`。
- `expired`、`merged` 只按来源记录声明，不按“查不到”推断，也不按本机日期推断。
- 查到但来源非权威（合成、第三方、官方但未复核）时，`evidence_status` 仍可 `obtained`，但 `authoritative=false`、`usable_as_basis=false`，证据必须显著标注来源类型与“仅作线索”。
- 编码记录的有效期单独用 `effective_status` 表达：`effective / not_yet_effective / expired / unknown`。记录和税则版本都没有给出任何有效期依据时必须是 `unknown`，不能兜底成 `effective`。即使来源声明 `active`，只要申报日期早于记录的 `effective_from`、晚于 `effective_to`，或没有业务申报日期可核对，`declaration_ready` 与 `usable_as_basis` 都必须为 false，并给出 `HS_RECORD_NOT_EFFECTIVE`、`HS_EFFECTIVE_DATE_UNKNOWN` 或 `HS_RECORD_EFFECTIVE_UNVERIFIED`。
- 编码状态（来源是否声明 active/expired/merged）与“在某个申报日期是否适用”是两件事：不能因为记录属于某税则版本就跳过记录自身的生效/失效日期。

### 6.3 来源权威与复核

`DatasetInfo.authority=authoritative` 只在同时满足以下条件时成立：

- `source_type` 是 `official_tariff` 或 `official_declaration`；
- `synthetic` 不为 true；
- `review_status=reviewed`，且 `reviewed_by`、`reviewed_at` 非空（复核人必须是关务人员，不是导入脚本）；
- 第三方数据不得声明 `authoritative`，合成数据不得声明 `authoritative` 或 `synthetic=false`。

“官方文件导入”不等于“已复核”。未复核数据可用于技术联调，但不能产出 `sufficient` 或 `declaration_ready`。已复核数据必须同时有合法的 `reviewed_by`、ISO 格式 `reviewed_at` 与 `retrieved_at`；`synthetic=true` 只能与 `source_type=synthetic` 组合，官方来源不能声明合成/第三方权威。

### 6.4 事实充足性判定

`sufficiency` 只有四个取值，判定顺序固定：

| 取值 | 条件 | 下游动作 |
|---|---|---|
| `not_checked` | 输入不合法、没有编码、未发起核对 | 补输入；不能读成“无需要素” |
| `unverified` | 来源未配置/失败、清单未收录、版本未核、来源非权威、原文解析不确定、存在条件/口径待确认 | 转人工/专家；**不得生成供应商补料单** |
| `insufficient` | 权威+已复核+版本已核+解析确定，且至少一个必填项缺失/为空/取值非法（整列要素为空也算） | 可生成 `supplement_items` 向供应商补料 |
| `sufficient` | 上述门槛全部满足，且无缺失/非法/待确认项 | 仍不能替代人工确认与专家判断 |

逐项状态区分 `missing`（根本没出现）、`empty`（出现但为空）、`invalid_enum`/`invalid_number`（有值但不合规）、`unverifiable`（条件要素或口径未知）、`not_applicable`（选填未提供）。只有 `insufficient` 才允许携带 `supplement_items`（结果模型强制），避免“版本待核/来源是 mock”时误发补料。

整列要素为空与“未发起核对”不同：调用方明确传入空要素时按“必填全部缺失”判 `insufficient` 并生成补料；只有调用方根本没传核对对象（`None`）或编码/输入不合法时才 `not_checked`。这样供应商把要素栏留空不会被误读成“无需核对”。

条件要素：来源只给人类可读的 `condition` 文本，P14**不自动判断条件是否成立**；条件要素未提供时一律 `unverifiable`，转专家确认。要自动化必须先由关务专家把条件写成机读规则并另行评审，不能由模型猜。

### 6.5 要素原文解析

支持位置式（`4|1|深沟球轴承|…`）、编号式（`1:品牌类型:无品牌;…`）、键值式（`品牌类型:无品牌;…`）与结构化映射。解析保守：
- 位置数量与清单不一致、编号有缺口、名称歧义、结构化值与原文冲突，一律 `partial`，`sufficiency=unverified`，先人工核对原文；
- 原文先做 NFKC 归一化再检查长度；归一化后超过上限时**不截断**，整体标 `unparsed` 转人工，避免膨胀字符把尾部冲突截掉；
- 编号段超过 6 位数字不转整数、不按序号解释，直接记为未识别并转人工；
- `品牌类型|用途|型号` 这类竖线模板按 `template_only` 处理，不会把要素名当成已填值；`名称:值|名称:值` 按键值式解析，不按位置硬塞；
- 编号式只接受可识别的 `名称[:值]`；`1:无品牌` 这类无法区分“未知名称/纯值”的写法不按编号猜值，标 `partial`（纯值请用位置式或结构化输入）；
- 同一要素名重复出现（含两个别名指向同一要素）标 `partial`，保留首个值并转人工，不允许后值静默覆盖前值；模板原文与结构化值组合时，只要出现未识别字段也降 `partial`；
- 换行、制表、以及“空白后跟已知要素名”作为分段边界；`|` 与 `;`/换行混用、或单个值内再出现 `名称:` 冒号时标 `partial`，不把下一段吞进上一段；
- 只有名称没有值的模板按 `template_only` 处理，视为未填写，可判 `insufficient`；
- 未识别的键不静默丢弃，进入 `unrecognized_keys`；
- 不允许通过“猜位置”把缺项洗成已填。

### 6.6 证据与引用

每条编码/清单证据至少带：确定性 `evidence_id`（如 `hs:2026:8544422900`）、来源类型、标题、文档标识、定位（税则号列/清单条目）、摘录、取得时间、版本生效/失效日期、查询参数、状态与限制说明。`document`/`locator` 去除不可见字符（含零宽空格、BOM、NBSP、U+2060/U+2063 等 Cf 类，U+034F 组合字素连接符、U+E0100 补充变体选择符等 Mn 类，以及 Hangul filler/tag 字符、代理/未分配码位）后必须非空，权威来源的证据必须有 ISO 格式 `retrieved_at`；来源的 `retrieved_at` 在导入/加载期校验，非法值返回 `query_failed`，不得在结果构造期抛异常；没有取到记录或清单时不生成证据条目，不编造 URL/条款。

### 6.7 来源能力清单（2026-11-16 本机实测）

| 来源 | 内容 | 访问方式 | 本机结论 | 限制 |
|---|---|---|---|---|
| 海关总署「互联网+海关」税则查询 | 税则 | 公开页面 | `unreachable` | 无开放 API；`www.customs.gov.cn` 返回 504/超时，TLS 证书链被本机代理替换（curl self signed） |
| 《海关进出口商品涉税规范申报目录》年度版 | 要素 | 人工结构化导入 | `requires_authorization` | 无批量下载/开放接口；必填口径需专家确认；本机未取得 2026 年版原件 |
| 中国国际贸易单一窗口参数查询 | 税则+要素 | 公开页面（需登录） | `requires_authorization` | JavaScript 单页应用，未登录只返回加载提示；HEAD 403 不能当可用性证明 |
| 第三方 HS 编码站 | 税则+要素 | 公开页面 | `available`（仅初筛） | 可能滞后/缺项/错误；只能作字段形状参考，不得作为合规结论 |

因此 P14 当前交付的是**可运行适配器 + 明确标注的合成样本 + 来源能力清单**，没有真实官方数据；任何“真实查询已通过”的说法都不成立。真实接入需 Jay/关务专家提供授权导出文件并按 D6 导入、复核。

### 6.8 P16 集成规则

P16 只能在以下条件同时满足时，把 P14 的结果当作“事实已核实”：

- `authoritative=true` 且 `usable_as_basis=true`（HS）或 `sufficiency` 为 `sufficient`/`insufficient`（要素）；
- 版本 `verified`；
- 证据可定位且属于当前版本；
- 未出现 `version_unverified`/`query_failed`/`not_found` 被当作“无风险”的情况。

其余情况保持 `needs_confirmation`，把 P14 的 `gaps` 按 `suggested_role` 分流：`supplier` 只在 `insufficient` 时进入补料；`expert`/`operator` 进入人工核查。P16 不得用模型补齐 P14 未取得的条款或要素值。

## 7. 退税与监管来源适配（P17 发布，2026-11-17）

### 7.1 边界与入口

P17 与 P14 同级：**只提供来源事实，不做退税判定、不做合规裁定、不替代审批**。它回答两个问题：

1. 某个编码在某期退税政策里对应什么退税率/处理方式、有哪些适用条件（`adapters/sources/tax_lookup.py`）；
2. 某个编码在某个申报日期、某个进出口方向下，来源声明了哪些监管/检验检疫要求，是否**明确声明**无要求（`adapters/sources/regulatory_lookup.py`）。

两个入口都不接收图片、不接收关税税率、不接收“要不要办证”的调用方断言，不产生 AI 判断，不落库、不访问网络、不调用模型。存储可替换；默认读环境变量指向的本地 JSON，未配置就返回 `source_unconfigured`，**没有默认样本**。

| 入口 | 输入 | 输出 |
|---|---|---|
| `TaxRateSource.lookup(TaxRateQuery)` | 编码原文、适用退税代码（可选）、`QueryBasis`、原表退税率原文 | 格式分类、版本解析、来源记录、原值/查询值/机械比对三块、`applicability`、问题、待核原因、证据 |
| `RegulatorySource.lookup(RegulatoryQuery)` | 编码原文、**进出口方向（必需）**、`QueryBasis` | 格式分类、版本解析、来源记录、`outcome` 三态、措施与两用物项线索、问题、待核原因、证据 |

`QueryBasis` 沿用 P14（D3 第 5 节）：**系统不填拟申报日期**；两个基准都为空时版本保持 `unverified`。在退税适配器里 `tariff_version` 的语义是“退税政策/文库版本”。

**明确不做的**：不从图片或 OCR 推退税率；不把关税税率换算成出口退税率（输入里出现 `tariff_rate`/`duty_rate` 等字段直接 `invalid_input`，见 `TAX_RATE_DERIVED_FROM_TARIFF_REFUSED`）；不内置监管代码→含义表；不判定两用物项是否适用；不生成条件判定规则。

### 7.2 退税率：三种事实不能混

`rate_kind` 是**来源声明的处理方式**，`rate_percent` 只在数值型下存在：

| 来源声明 | `rate_kind` | `rate_percent` | 含义 |
|---|---|---|---|
| 具体退税率 | `export_rebate` | 13.0 | 退税率 13%（必须 > 0） |
| 明确零税率 | `zero_rate` | 0.0 | 零税率，不是“没查到” |
| 免税 | `exempt` | `null` | 免税；不是 0% 退税率 |
| 不予退税 | `not_refundable` | `null` | 明确不予退税；不是 0% |
| 不适用出口退税 | `not_applicable` | `null` | 不适用；不是 0% |
| 来源未说明 | `unknown` | `null` | 待核；不得补成任何数值 |

- 0% 只能用 `zero_rate` 表达：`export_rebate` 配 0 会在导入期被拒绝，避免把“未取得/不予退税”和“零税率”混成一个值；来源原文仍保留在 `rate_raw`。
- 查不到记录 ⇒ `record=null`、`looked_up_rate=null`、`evidence_status=not_found`；**永远不会出现 0%**。
- 原表空白 ⇒ `original_rate.state=blank`、`percent=null`；**空白不是 0%，也不是“不予退税”**。

### 7.3 三块分开：原值 / 查询值 / 机械比对

| 字段 | 内容 | 谁产生 |
|---|---|---|
| `original_rate` | 供应商原表退税率：`raw` 原样保留，`state` 为 `blank/numeric/non_numeric/unparseable` | 解析原文，不修正 |
| `looked_up_rate` | 来源查询值：`rate_kind/rate_percent/rate_raw/rate_state/rebate_code` | 逐字段镜像来源记录（结果校验强制） |
| `rate_comparison` | `matched/conflict/not_compared/unverified` + 原因 | 固定规则机械比对，`produced_by=deterministic_adapter` |

- `rate_comparison` **不是 AI 判断，也不代表审批**；P18 的 AI 判断必须另立字段，不能写回这里（结果校验拒绝 `produced_by != deterministic_adapter`）。
- 任一端不可比（原值空白、来源为非数值处理方式）一律 `not_compared` 并写明原因，不默认“一致”；原值有内容但无法确定唯一数值（区间、多个值）为 `unverified`。
- `conflict` 只陈述“数值不一致”，**不裁定谁对**；必须转专家。

### 7.4 适用性（`applicability`）

| 取值 | 条件 | 下游动作 |
|---|---|---|
| `applies` | 10 位完整编码 + 来源权威（官方 + 已复核）+ 版本已核 + 申报日期落在记录有效期 + 记录 `in_force` + 来源**明确声明无条件限制**（`conditions: []`）+ 适用退税代码核对一致 | 可作该申报日期依据（仍不等于审批通过） |
| `conditional` | 来源给出条件文本但未机读化 | 转专家；**不得**当作已适用 |
| `not_effective` | 申报日期早于/晚于记录有效期，或来源声明政策废止/被替代 | 换用当期政策或转专家 |
| `unknown` | 缺申报日期、版本待核、来源未声明条件、来源未声明处理方式/状态、来源非权威、8 位子目、退税代码无法核对 | 转人工/专家 |

- `conditions` 字段的三种状态必须区分：字段缺失 ⇒ `not_declared`（**不等于无条件**，判 `unknown`）；空数组 ⇒ `declared_none`；非空 ⇒ `declared_unverified`（判 `conditional`）。
- **任何非 `applies` 的结果至少带一条 `pending_reasons`**（结果校验强制 + 构造期兜底），P18 据此保持 `unknown`，不能读成无风险。
- `usable_as_basis` 与 `applicability=applies` 严格等价，不接受人工覆盖。
- 8 位子目一律 `unknown`（不 `applies`）：文库若按 10 位维护，退税率可能不同，且无法确认申报编码。
- `expert_review_required`：**只有 `applies` 且 `rate_comparison=matched` 时为 false**。`conflict`、`unverified` 与 `not_compared`（含原表空白、来源是“不予退税/免税”等非数值处理方式）都必须转专家——2026-11-17 复查修复的正是“原表 13%、来源不予退税却标无需专家”这一类（缺陷 C-18）。
- `usable_as_basis` 与 `expert_review_required` 是**两件事**：前者说“来源事实可作依据”，后者说“这一分项还有要人看的问题”。二者都为真时（例如来源是不予退税但原值写了 13%），P18 必须按专家复核处理，不能只看 `usable_as_basis`。

### 7.5 监管条件：三态与方向

| `outcome` | 含义 | 能否读成“无监管” |
|---|---|---|
| `measures_required` | 来源明确列出监管/检验检疫要求 | 否（是“有要求”） |
| `declared_none` | 来源明确声明无要求（`measures: []`）且日期/方向可核 | **仅当 `usable_as_basis=true`** |
| `not_effective` | 记录已废止，或申报日期不在有效期 | 否 |
| `unknown` | 未取得、`measures` 字段缺失、版本待核、缺申报日期、方向不明/不一致、来源失败 | 否 |

- `measures` 字段缺失（`null`）⇒ `measures_state=not_declared` ⇒ `outcome=unknown`；**“来源没写”不等于“来源说没有”**。
- **方向是必需输入**：未给方向直接 `invalid_input`（`REG_DIRECTION_UNKNOWN`），不默认进口或“双向都行”；记录方向与查询方向不一致时返回 `not_found` 并提示 `REG_OTHER_VERSION_OR_DIRECTION`，**不能据此方向判无监管**。
- `direction=both` 的记录**不得携带具体措施**（导入期拒绝）：来源表里 A 类进口、B 类出口必须拆成两条方向明确的记录，否则无法逐条核对方向。
- 措施名称（`name`）与主管机关（`agency`）只来自来源；只有代码时留空并报 `REG_MEASURE_NAME_UNKNOWN`，**不得由模型或硬编码补出含义**；此时 `usable_as_basis=false`。
- 8 位子目一律 `usable_as_basis=false`（`REG_SUBHEADING_ONLY` + 待核原因）：监管条件通常按 10 位维护，8 位只说明层级；“来源声明无监管”也不例外（缺陷 C-16）。
- **两用物项有两个入口，行为必须一致**：`dual_use[]` 线索列表，以及 `measures[].category=dual_use`。两种写法都要求 `usable_as_basis=false`、`expert_review_required=true` 并报问题（`REG_DUAL_USE_UNVERIFIED` / `REG_MEASURE_DUAL_USE_UNVERIFIED`）；不能因为写进“监管措施”就当作已核清（缺陷 C-17）。
- 两用物项只输出清单命中线索：`verdict` **恒为 `unknown`**（枚举只有一个取值，结果校验拒绝其它值），必须转专家按技术参数、最终用途与用户判定；不通过改名规避。
- 措施类别不明（`category=unknown`）同样 `usable_as_basis=false` 并报 `REG_MEASURE_CATEGORY_UNKNOWN`：不知道是证件、检验检疫还是两用物项，就不能说监管结论已核清。
- `expert_review_required == not usable_as_basis`：可用已排除 8 位码、来源非权威、方向不符、结论待核、措施不可定位/无名称/类别不明/两用物项，因此“可用”等价于“不需要专家再看”，任何降级都必须转人，不能自动放行。
- 来源明确声明无监管且可作依据时 `pending_reasons` 为空：人工审批属于 P18/专家的业务流程，不算“适配器还有没核清的事”，避免出现“无需专家却仍挂待核”的自相矛盾。

### 7.6 证据与引用

- 证据按“1 条记录证据 + 每条可定位措施/条件/线索各 1 条”生成：`tax:<版本>:<税号>`、`tax:<版本>:<税号>:cond<n>`、`reg:<版本>:<税号>:<方向>`、`reg:<版本>:<税号>:<方向>:measure<n>`、`...:dual<n>`；`document`/`locator` 非空、`retrieved_at` 合法、权威来源必须有 `retrieved_at`（与 P14 同一校验）。
- 没有文档/条款定位的条件、措施、线索**不生成证据**，改为问题 `*_UNLOCATED` 并计入 `pending_reasons`；此时不得 `usable_as_basis`。
- 证据 `query` 带查询参数（编码、方向、基准、措施代码），便于 P18 逐条复核与页面回跳。
- 查无/失败不生成任何证据条目，不编造公告号或条款。

### 7.7 来源能力清单（2026-11-17 本机实测）

| 来源 | 内容 | 访问方式 | 本机结论 | 限制 |
|---|---|---|---|---|
| 税务总局出口退税率文库 | 退税率 | 人工导入（需授权） | `requires_authorization` | 无公开批量接口；需电子税务局/离线工具取得；与海关税则分库，口径可能不一致 |
| 财政部/税务总局退税政策公告 | 政策依据 | 公开页面 | `available` | 只给政策口径与范围，不含全量税率表；条件为文字表述，未机读化前待核 |
| 中国国际贸易单一窗口参数查询 | 监管/检验检疫 | 公开页面（需登录） | `requires_authorization` | JavaScript 单页应用；代码含义需另取官方代码表 |
| 海关总署监管条件公告 | 监管/检验检疫 | 公开页面 | `unreachable` | `www.customs.gov.cn` 连接失败（与 P14 一致） |
| 商务部中国出口管制信息网 | 两用物项清单 | 公开页面 | `available` | 清单与 HS 不一一对应；是否受管制必须专家判定 |
| 第三方退税率/监管条件站 | 退税率/监管 | 公开页面 | `available`（仅初筛） | 可能滞后/缺项/错误，不得作为结论 |

结论：P17 交付的是**可运行适配器 + 明确标注的合成样本 + 来源能力清单**，没有真实数据；任何“真实退税率/监管条件已查通”的说法都不成立。真实接入需 Jay/关务专家按 D6 §2.6 导入并复核。

### 7.8 P18 集成规则

P18 只有在以下条件同时满足时，才能把 P17 的结果当作“已核实事实”：

- 退税：`usable_as_basis=true`（即 `applicability=applies`）**且** `expert_review_required=false`；原值与来源值一致只能作为**机械一致**，专业结论仍需专家；
- 监管：`usable_as_basis=true` **且** `expert_review_required=false`，且 `outcome` 为 `measures_required`/`declared_none`；
- 证据可定位且属于当前版本；两用物项 `verdict` 恒为 `unknown`（无论写在 `dual_use[]` 还是 `measures[].category` 里）。
- 两个标志都必须是“否/否”才能进 `matched`；只满足一个（例如来源事实可作依据，但原值比对没对上）一律按下一条降级处理。

其余情况：`conditional`/`unknown`/`not_effective` 一律记为分项 `unknown`（**不是 matched，也不是“无风险”**），把 `pending_reasons` 按 `suggested_role` 分流（`supplier` 补资料、`expert` 判专业、`operator` 补来源）。P18 不得用模型补齐 P17 未取得的条款、税率、证件名称，也不得把 `declared_none` 写成“无监管”除非 `usable_as_basis=true`；专家改码后必须以新 HS 重跑本适配器，旧结果只保留为历史。

## P06 inline mock 接线补充（2026-11-17）

上传事务先预登记一个 `queued, is_current=false` Run，作为持久执行凭据；解析/图片成功后，系统用
`execute_precheck(version, run=job.precheck)` 激活同一 Run，整表执行。未解析成功不激活，不产生有效预审结果。
该编排是 P06 inline 模式，P13 后续接队列和后台恢复。失败/中断恢复复用 ImportJob、版本、Run，不追加第二轮。
原有 P02 显式预审入口仍可不传 run，保持兼容。预审全部行失败时导入可以成功，但 `precheck_status=failed`，
页面分别展示解析状态与预审失败，不能写成“预审通过”。
P06 不读取 P10 候选对照，不使用旧版本确认/审批，不调用真实 AI、官方来源或后台队列。

P06 对抗性修复（2026-11-17）：`_build_recommend_input` 不再透传 Item.attributes。
只从固定白名单取标量商品事实；`excel_fields`、原 HS、退税率、历史审核意见、未识别备注和旧结果列留库核查，
不进入独立推荐 DTO。原码仅进入 compare 阶段；自由文本事实中可能自然出现编码，仍记录锚定风险。
