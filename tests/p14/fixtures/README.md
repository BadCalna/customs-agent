# P14 测试夹具说明

`tariff_synthetic.json` 与 `elements_synthetic.json` 是**合成的接口样本**，只用于验证：

- 版本窗口与查询基准解析；
- 10 位/8 位编码、作废、并码、跨版本未收录等分支；
- 位置式/编号式/键值式要素原文解析；
- 四态与事实充足性的结构约束。

它们不是真实税则或《规范申报目录》，`dataset.synthetic=true`、`authority=synthetic`、
`review_status=unreviewed`。任何测试或演示输出都不得复制成业务依据；真实数据必须由
Jay/关务专家提供并复核后，通过环境变量 `CUSTOMS_TARIFF_PATH` 与
`CUSTOMS_DECLARATION_ELEMENTS_PATH` 指向独立文件。

测试里另有一组“已复核官方来源”的内存元数据（`tests/p14/helpers.py`），只用于证明
“当上游确实提供权威且已复核数据时”充足性逻辑会如何判定，不代表本仓库已接入官方数据。
