# P12 SQLite → PostgreSQL 切换验收记录

日期：2026-11-17（Asia/Shanghai）。分支：`feature/P12-cutover`。初始严格导入基线 `ad89340`；收尾时合入新主线 `6731a3d`（PR #28，无模型/迁移变化），重新完成 PG 业务流程、七套件双库回归和完整恢复。历史 PR #11 保留为已合并历史，新补强变更单独评审。

## 范围与数据来源

本次切换对象是隔离 P12 工作树，不替换原工作台 WIP 或现有服务。源来自原 P12 演示库的一致性副本，在副本补齐当前迁移并加入 P06 合成上传/认领、组/权限、日志/Session 关系，以覆盖原库没有的路径；原库未修改。

- 原源：`/Users/jaychen/PycharmProjects/customs-agent-p12/.local/p12/source-with-demo.sqlite3`。
- 原 SHA-256：`58979101e6898fa91aea039f5994c42efc64e621408ce7b80dd7a5f02054f3ea`。
- 工作源：`.local/cutover/source.sqlite3`。
- 工作源 SHA-256：`06912e83377a52501d52d80f31142883b72d97314d1744e32d48b1e6d8f8dc83`。
- 目标：`customs_p12_cutover`，本工作树独立 PG 17.12 集群 `127.0.0.1:55412`；文件根 `.local/files`。
- 原工作台 WIP 的 `reviews.0006_issue_lineage` 未合入主线，保护检查拒绝导入；没有忽略其额外字段或数据。

附件为合成 XLSX/PNG，AI 为 mock、执行器为 inline。原演示记录中的空 storage_path 不代表存在附件实体；物理文件核对只统计非空引用，完整目录另由 P22 归档与恢复校验。会话原始值随数据保留，恢复下载使用本轮备份中有效的登录会话。

## 严格迁移与保护

命令：

```sh
CUSTOMS_PG_ALLOW_FLUSH=customs_p12_cutover \
  scripts/database/copy_sqlite_to_pg.sh .local/cutover/source.sqlite3 --fresh
```

这里的 fresh 只针对本轮尚未接纳 PG 新业务的独立演练库；新写入后没有再次清空。正式切换建议换全新空目标。

结果：23 个模型 / 87 个对象导入；26 组模型与自动 M2M 行数及内容摘要相等；外键孤儿 0；2 个源实体文件路径/大小/SHA-256 全相等。包含原任务/版本/商品/结果/提交/决定、P06 导入/认领、用户/组/权限、管理员日志、Session。

证据：`.local/cutover/copy-final.log`、`.local/p12/copy-20261117T172705-7zEuIj/verify.json`、同目录 `source-upgraded.sqlite3`、`source-files.json` 和 `sqlite-dump.json`。

| 负向操作 | 退出码 | 结果 |
|---|---:|---|
| 向非空 PG 导入 | 3 | 拒绝，目标内容指纹不变 |
| fresh 无精确库名授权 | 3 | 拒绝，目标内容指纹不变 |
| 原工作台未知 WIP 迁移 | 1 | 拒绝，目标内容指纹不变 |
| 引用的源附件缺失 | 1 | 导入前拒绝，目标内容指纹不变 |
| 克隆附件同路径/同大小翻转一字节 | 1 | 附件清单不匹配，26 组数据库检查仍通过 |

证据：`.local/cutover/guards.json`、`guard-*.log`、`tampered-verify.json`。原库和工作源 SHA-256 均保持上述值。自定义/旧 ContentType 与 Permission、浮点微差和清单内容差异另有自动回归用例。

## PostgreSQL 切换后业务

两轮均保留新增业务，第二轮基于最终主线 `6731a3d`。使用 `Django Client(enforce_csrf_checks=True)`、真实认证 middleware/session/CSRF 和实际 PG；无需另起服务器。每轮 21 项检查通过：

- 迁移前的原件、图片可通过 `/api/v1/files/{id}` 下载并匹配 SHA-256。
- multipart 新建上传，3 商品解析成功；图片状态分别 available / absent / read_failed；预审自动执行。
- 业务员认领及同 key 重放，3 行确认；旧 revision 返回 409 `stale_revision`。
- 新提交及同 key 重放，专家核查 run 含 3 行。
- 专家批准并将首行改码为 `8536901900`；同 key 返回同一决定，再次普通请求返回 409 `submission_closed`。
- 历史包含决定、任务阶段 approved；新增原件/图片均可下载。
- 整数主键组、权限 M2M 和管理员日志可继续写入，证明序列恢复后可用。
- 只读查询旧 SQLite，确认 PG 新任务不在旧库中，明确回退边界。

第二轮关键标识：

| 对象 | ID |
|---|---|
| Task | `8bd60f88-5a63-4712-930d-48e583d714c5` |
| FileVersion | `ea8bbe68-ef90-458e-8557-192653ed107a` |
| Submission | `41303016-94fb-4d60-acde-84ef8de93e84` |
| Decision | `b5dcada5-ae13-45b0-aac5-22cabc0dd05b` |

证据：`.local/cutover/live-workflow.json`、`live-workflow-main28.json` 及各自日志。两次本机 API 验收保持合成数据标记，没有宣称真实外部 AI 或队列验收。

## PG + 文件完整备份与恢复

完成 PG 新写入、退出所有本轮业务脚本后停写；测试使用独立数据库及临时文件根，不写业务目标。执行 `scripts/database/backup.sh p12-post-main28`，随后用 P22 native `restore.sh` 和 `verify_restore.sh` 写入全新目标。

- 完整备份：`.local/backups/20261117T095035-p12-post-main28-af8cf954/`；包含 COMPLETE、manifest、PG dump、文件归档、表/schema/sequence 与文件指纹。
- 恢复数据库：`customs_p12_full_restore_main28`；OID `39943`。
- 恢复文件根：`.local/cutover/restored-files-main28`；恢复状态保存目标 OID 与目录 device/inode。
- 通过备份中的有效会话，在恢复目标只读检查 14 项：所有 26 组模型/M2M 数量、6 文件清单、旧/最新原件和图片下载、PG 新任务/提交/决定/历史；未新增数据行。
- 最后再执行 native 完整验证，返回 `verified=true`；数据库内容/schema/sequence 和完整文件目录均与备份一致，覆盖 API 读取之后的状态。

关键数量：

| 对象 | 迁移后、PG 新写入前 | 最新 PG / 完整恢复目标 |
|---|---:|---:|
| User / Group | 5 / 1 | 5 / 3 |
| Task / FileVersion / Item | 5 / 4 / 14 | 7 / 6 / 20 |
| Run / RunItem | 5 / 15 | 9 / 27 |
| ImportJob / TaskClaim | 1 / 1 | 3 / 3 |
| Confirmation / Submission / Decision | 1 / 1 / 1 | 7 / 3 / 3 |
| AuditEvent / IdempotencyRecord | 3 / 2 | 23 / 10 |
| 实体附件 | 2 | 6 |

证据：`.local/cutover/backup-main28.log`、`restore-main28.json`、`restored-reads-main28.json`、`verify-restore-main28.log`；完整逐项结果 `.local/.state/verify-20261117T095039-c70a0a47.json`。上一轮独立恢复目标与备份也保留，没有覆盖或删除。

## 最终回归

`make check` 通过；最终基线 `make check-postgres` 实际连接 PG，373 项 OK，跳过 1。七套件在两个后端分别执行：

| 套件 | 总数 / 后端 | SQLite 跳过 | PG 跳过 |
|---|---:|---:|---:|
| P12 | 88 | 18 | 0 |
| P05 | 106 | 1 | 1 |
| P06 | 42 | 4 | 0 |
| P09 | 86 | 0 | 0 |
| P10 | 51 | 0 | 0 |
| P01 | 109 | 0 | 0 |
| P02 | 59 | 0 | 0 |
| 合计 | 541 | 23 | 1 |

全部退出 0。P05 两端同一项未实现项跳过；SQLite 其余跳过为 PG 专属检查。P06 故障注入中的 disk full / interrupt 日志是预期测试路径。原始报告 `.local/p12/dual-db-report.md`，最终合并套件日志 `.local/cutover/check-postgres-main28.log`。

PG 并发包括确认/提交、P06 上传与认领、P10 决定：各线程独立连接、屏障启动，最终落库与审计计数断言。新增决定测试证明不同请求仅一个成功；同 key 同内容一次新建一次重放；更新版本出现后旧提交双路 409 无副作用。SQLite 的跳过结果不作为锁有效的证据。

## 运行与回退边界

未入 Git 的 `.env` 已指向上述隔离 PG，可在 P12 工作树运行 `make dev` 使用它。本轮没有将原 `customs-agent` WIP 或既有 HTTP 服务热切到本工作树。原 WIP 须先集成未知迁移，再独立执行切换。

PG 新业务已经存在，旧 SQLite 已不是无损回退点。需回退时停写、保存完整 PG + 文件备份，恢复到全新 PG 目标并校验后改连接。不要重新 flush 业务库、直接改回旧 SQLite，或只恢复一个 DB dump 就声称完成附件恢复。
