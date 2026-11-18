# P15 历史参考存储

此 app 只维护历史参考表，与 reviews/runs 等业务表没有外键或写入路径。
`0001_initial` 的 `dependencies=[]`。由部署方加入 `INSTALLED_APPS` 并运行迁移。

## Python 接口

```python
from apps.history_reference.services import import_history_snapshot
from apps.history_reference.store import DatabaseHistoryStore

report = import_history_snapshot(
    path, dataset_key="historical-success", expected_count=1198,
    require_real=True, replace_changed=False,
)
snapshot = DatabaseHistoryStore("historical-success", using="default").load()
```

接口返回私有审计 `dict`，公开日志只能选取脱敏 counts。导入 API 默认仅 PostgreSQL；
`allow_sqlite=True` 只供独立单元测试，CLI 不提供此开关。
`dry_run=True` 使用与正式操作相同的锁/合并/校验，但回滚整个事务，包括新 dataset。

导入前校验版本、DTO、明确 synthetic、来源系统/清单、汇总身份版本、重复 ID、声明
record_count、expected_count。真实数据必须保留完整 human_declaration（声明人/时间/文本/
覆盖所有来源的 scope_source_ids）。不补造放行凭证、业务日期或版本。

## 安全命令

```sh
python manage.py import_history_records \
  --records /private/history_records.json \
  --dataset-key historical-success --expected-count 1198 --require-real \
  --expect-database TARGET_DATABASE --dry-run \
  --report /private/new-report.json
```

`--expect-database` 必须与 `SELECT current_database()` 的真实返回值一致，不只核对环境标签。
报告使用 O_EXCL 新建、权限 0600，拒绝覆盖现有文件/符号链接；报告失败可能发生在数据库事务
已提交之后，此时命令会明确说明。建议先选择新的私有报告路径。

退出状态：0=成功（或正常 dry-run），1=输入/数据库/目标校验错误，2=已原子完成但
changed/conflict 默认保留旧内容，须人工审查私有报告；dry-run 的 2 不会提交。

## 合并、风险与历史

- 复用 P15 `_merge_consolidated_records` / `_merge_records`，绝非仅 HS+品名去重。
- 同内容再导入新增 0 行；风险/图片/坐标更新遵循 P15 provenance 规则。
- changed 默认保留旧事实及其旧风险标记，不抄入新输入的干净结论或来源位置，且数据集主
  `source_files` 哈希与 `human_declaration` 保留原来源声明不覆盖（混合保留与新增/更新时
  同时在 `active_source_files` / `active_declarations` 保留双向映射）；新批次输入元数据
  与声明保存在每次的 `HistoryImportBatch.report["input_dataset"]` 审计中。
- 唯一稳定 anchor 配对的内容身份变化被报告为 changed，不追加成新案例。
- `--replace-changed` 才接受整条新事实；SQL 主键保留，旧 record_id 与完整 payload 保存在
  `HistoricalRecordRevision`，每次更改追加 revision。missing 仅报告，绝不自动删行。
- 汇总记录最终保留的非 ASCII/非十位 HS 恢复待核标记，默认检索排除。
- 同 dataset_key 不可混合真实/合成、格式、身份版本或来源集合。多对多不确定 anchor
  遵循 P15 不猜覆盖规则，记录 identity_uncertain 并以 partial 报告。

所有 dataset、records、revision 和 batch 在同一事务提交；异常整批回滚，无半成功 batch。
并发写入同一 dataset 时通过 `select_for_update` 串行；读者采用无排他锁读取并在事务内外
核对 `HistoryDataset` 的 `(revision, record_count, updated_at)` 代际一致（遇并发提交自动重试），
不会在整批预审期间持有 `FOR UPDATE` 锁，并发查询无需互相等待，也不会混读不同批次的元数据和记录。
同一预审批次内通过 `precheck_history_source()` 复用同一份历史快照，只读库和校验一次。
store 自身 atomic/savepoint 外转换 HistoryStoreError，不污染调用方外层业务事务。
空 dataset、缺失表/连接、count/SQL冗余字段/DTO 契约错误均是 query_failed，不伪装成 no_match。

测试：`python manage.py test tests.p15_pg`；使用 Django 创建的独立测试库，不对运行中的
业务库执行导入。PostgreSQL 多连接专项覆盖首次导入竞态、无排他锁并发读、并发导入代际一致性、错误 savepoint。
