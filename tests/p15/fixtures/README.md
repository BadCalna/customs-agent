# P15 固定检索样例

本目录只有**合成样本**，不是真实历史申报库：

- `queries.json`：固定检索用例（有命中、无命中、相似但关键属性不同、旧时间版本、缺要素）。
  记录集指向 `adapters/sources/fixtures/history_records.sample.json`，同一文件也是
  `HistorySearchSource()` 的默认数据源。
- 合成记录里的品名、HS、单据号均为虚构；`https://synthetic.invalid/...` 是不可解析的
  占位地址。合成样本只验证接口、差异标注与四态（obtained / not_found / query_failed /
  version_unverified）行为，**不证明检索效果**。

新增用例时同步更新 `tests/p15/test_search.py` 的断言；记录集结构变化要递增
`adapters/sources/history_search.py::SCHEMA_VERSION`。
