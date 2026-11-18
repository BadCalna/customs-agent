# P18 专家 AI 核查引擎提示词与安全契约

版本：`engine_version = "expert-check-0.1.0"`，`prompt_version = "expert-check-json-1"`，`rules_version = "d4-0.6-p14-p17-p18"`。

本目录仅定义 `ExpertCheckEngine`（`adapters/engines/expert_check.py`）与可选 `CompletionModel` 交互时的严格输入输出边界。与 P16 `PrecheckEngine` 一致：本引擎不访问网络、不读写数据库、不调用 `get_engine()` 重新解析引擎、不自动创建带默认样本的 `HistorySearchSource`。

## 1. 组合来源与分项推导原则

`ExpertCheckEngine.expert_check` 对业务员不可变提交快照 `ExpertCheckInput` 的 `submitted_hs` 产出四类分项结论（`elements` / `images` / `tax` / `regulatory`），每个分项严格产出一条 `ExpertFinding`：

| 分项 | 来源 | 结论推导规则（禁止手拼布尔） |
|---|---|---|
| `elements` | P14 `DeclarationElementsSource`（可选 `HsLookupSource` + 可选语义核对） | 仅 `sufficiency=sufficient` 且权威已复核版本已核非合成（且 HS 可申报时）才可为 `matched`；`insufficient` 或权威明确失效/并码为 `conflict`；8 位/未核实/查无/失败/解析歧义/条件要素缺失为 `unknown` 且不生成补料 |
| `images` | `payload.images`（可选 `CompletionModel` 可见事实比对） | 存在 `read_failed` 必为 `unknown`（`IMAGE_READ_FAILED`，不混同 `absent`）；`absent` 或缺少 `visible_description` 必为 `unknown`；仅有明确可见描述且经模型比对才可能为 `matched`/`conflict` |
| `tax` | P17 `TaxRateSource` | 只消费 `usable_as_basis` 与 `expert_review_required`：仅 `usable_as_basis and not expert_review_required` 为 `matched`；`usable_as_basis and rate_comparison == conflict` 为 `conflict`；其余（含查无、空白、条件、不予退税 C-18、8 位、未核实、失败、非权威）一律 `unknown` |
| `regulatory` | P17 `RegulatorySource` | 只消费 `usable_as_basis`、`expert_review_required`、`outcome` 与 `dual_use`：仅 `usable_as_basis and not expert_review_required` 且 `outcome in {measures_required, declared_none}` 且无两用物项线索时为 `matched`；其余一律 `unknown` |
| `history` | 可选 P15 `HistorySearchSource` | 只作为参考证据附加到 `evidence`（`LIVE` 排除非 `synthetic=False`）；绝不修改任何分项 `verdict`，绝不直接当作 `suggested_hs` 或结论 |

## 2. 严格模型 JSON 契约

`model.complete(request: dict) -> str | dict` 同步接收 `{system, facts, source_summary, history_reference, response_schema}`，仅接受严格 JSON（禁止 Markdown 代码块、额外键、重复键、NaN/Infinity）：

```json
{
  "elements_semantic_conflict": false,
  "elements_conflict_reason": "",
  "image_verdict": "unknown",
  "image_reason": "未提供可用附图可见描述，保持未核实",
  "suggested_hs": null
}
```

## 3. 防漂移与不可升级硬约束

1. **不可升级**：模型响应绝不能将 `unknown` 或 `insufficient` 升级为 `matched`，不能推翻 P14/P17 结论，不能从 `read_failed` 或 `absent` 图片推断材质、工艺或税率。
2. **输入脱敏**：`execution` 上下文标识、`submission_id`、`image_id`、`source_cell`、图片 `error_message`、路径/凭据容器不进入模型输入。
3. **非正向原因码校验**：任何 `conflict` 或 `unknown` 分项在构造期强制校验至少具备一条非空 `IssueEntry.code`；`target_hs` 恒等于 `payload.submitted_hs`。
4. **结构化降级**：模型超时/异常/越权响应或引擎内部自检失败均转为脱敏 `EvidenceStatus.QUERY_FAILED` 结构化结果与四项 `unknown`，不返回 HTTP 500。
