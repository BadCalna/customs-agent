# P17 测试夹具说明

`tax_rates_synthetic.json` 与 `regulatory_synthetic.json` 是**合成的接口样本**，只用于验证：

- 政策版本窗口与查询基准解析（含“未提供基准”“日期越界”“多版本重叠”）；
- 退税处理方式六态（`export_rebate` / `zero_rate` / `exempt` / `not_refundable` /
  `not_applicable` / `unknown`）与 `rate_percent` 的一致性约束；
- 适用条件的三种声明（字段缺失 / `[]` / 非空文本）与“条件可定位才有证据”；
- 监管结论四态（`measures_required` / `declared_none` / `not_effective` / `unknown`）、
  方向匹配、措施只有代码没有名称、两用物项线索；
- 原税率与来源值的机械比对（`matched` / `conflict` / `not_compared` / `unverified`）。

它们不是真实退税率文库或监管条件表：`dataset.synthetic=true`、`authority=synthetic`、
`review_status=unreviewed`，因此任何测试或演示输出都**不得**复制成业务依据——合成来源在
适配器里一律 `authoritative=false`、`usable_as_basis=false`。

真实数据必须由 Jay/关务专家提供并复核后，通过环境变量 `CUSTOMS_TAX_RATE_PATH` 与
`CUSTOMS_REGULATORY_PATH` 指向独立文件（格式与导入步骤见 D6 第 2.6 节）。

测试里另有一组“已复核官方来源”的内存元数据（`tests/p17/helpers.py` 的
`official_tax_dataset()` / `official_regulatory_dataset()`），只用于证明“当上游确实提供权威且
已复核数据时”适配器会如何判定（例如 `applicability=applies`、`outcome=declared_none` 且
`usable_as_basis=true`），不代表本仓库已接入官方数据。
