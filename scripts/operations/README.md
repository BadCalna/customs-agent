# 运维脚本（P22）

## 修订后的运行契约

- 宿主机需要 Python >=3.12；shell 优先选择项目 `.venv` / `.venv-customs`，可用
  `OPERATIONS_PYTHON` 显式指定编排解释器。`PYTHON` 指 native Django 解释器。
- CLI 选项保留，但所有入口成功时 **stdout 仅输出一个 JSON 对象**，不再输出路径尾行。
  人类日志和子进程终端输出落在状态目录 `.log`；终端 stderr 显示记录位置和失败原因。
  `backup` 返回 `backup_dir`；`restore` 返回 `target_db`、`target_files`、`database_oid`、
  `file_identity`、`state_file`；`verify_restore` 可传 `--state-file` 校验恢复后目标没有被替换。
- 状态/演练证据默认位于 `BACKUP_ROOT` 的兄弟目录 `.state/`（可设置 `OPERATIONS_STATE_ROOT`），
  必须和备份介质分离。恢复/校验只读备份介质，不在介质写 restored 标记。
- 备份目录 `umask 077`；进行中有 `INCOMPLETE`，失败有 `FAILED`，成功才有 `COMPLETE`。
  restore 拒绝未完成/失败备份。JSON manifest 精确覆盖 dump/archive/counts/meta；一致性元数据也必须通过摘要校验。没有一致性证据的旧备份不能充当新版完整恢复验收产物；传统 SHA256 清单保留便于人工检查。
- Compose 从 `docker compose config --format json` 取得实际 app 数据库和文件根，
  不依赖 db 容器的初始化 POSTGRES_DB 等于当前 app 库名。原件根必须处于可写持久卷内，
  恢复目标必须在同卷边界内且与原件根无相同/祖先/后代关系；容器内再解析实际路径，拒绝符号链接和越界。
  默认目标在卷内预建的可写 `restores/restore-<stamp-random>` 下（`OPERATIONS_RESTORE_ROOT` 可设置另一卷内父目录）；
  从已恢复目录切换为新生产根后，后续恢复仍创建 sibling，不能创建其祖先/后代。
  native 的恢复边界默认原件根的父目录，可通过 `OPERATIONS_RESTORE_ROOT` 收紧。
- 恢复只创建全新库和全新目录（已存在的空目录也不抢占）；拒绝生产库、系统库、SQL 非安全标识符、超长名称。
  解包拒绝绝对路径、`..`、软/硬链接和特殊文件。`pg_restore --exit-on-error`；失败只按 OID/inode 身份清理自身创建目标。
- 两种模式都要求可执行的一致性边界，`--no-quiesce` 失败，不能生成 COMPLETE。native 取得 public 表 SHARE 锁、序列锁及本机文件排他锁，将导出的事务 snapshot 传给 `pg_dump`，归档后比较前后数据库/文件指纹，释放锁成功后才标记 COMPLETE；任何一步失败都留 FAILED。文件写入者必须已加载本版本的 storage 协作锁；它只覆盖同机同用户代码路径，旧进程、外部手工文件修改、跨主机写入及 DDL 仍须运维停写。锁文件位于固定 `/tmp/customs-file-guards-<uid>/`，不随 TMPDIR 改变。
- Compose 停写前记录运行服务；除 db/redis 外全部停止（含 worker 和未来写入者），只 `start` 原运行服务，不 `up` 意外启动原来关闭服务。
  文件归档始终使用一次性 app 容器，绝不 exec 已停止的 app。SIGTERM/SIGINT 走恢复 finally；SIGKILL/主机掉电仍需按 services-before 证据人工处理。
- 整个 deployment 的 backup/restore/verify/drill 使用非阻塞排他锁，防止并发操作；不同备份目录或切换 app 库名不绕过同 Compose 项目锁。
  锁路径固定为 `/tmp/customs-operations-<uid>/`，不读取登录/cron 的 `TMPDIR`，避免跨会话绕过。
- 演练成功才默认清理；任何阶段失败保留隔离目标、JSON 状态及终端日志。`--keep` 让成功演练也保留。
- 清单包含数据库实际 current_database/OID、Django 实际文件根/inode、全部 introspected 表
  （含 `django_migrations`，仅排除 `django_session`）、表行数及逐行稳定 SHA256 内容指纹、文件路径/大小/SHA256。
  直接从 cursor 逐批读取并保留 datetime 六位微秒，不用 dumpdata；每行哈希排序避免物理行序差异。
  指纹的排序占 O(行数) 内存；dump、tar 和介质 SHA256 均流式。新恢复契约不接受缺指纹/缺文件清单降级。
  独立 compare_counts 可读取旧版行数清单，但明确警告仅行数验证，不能作为新版恢复验收通过依据。
- 合法空原件目录允许（`files: []`、`file_count: 0`）；备份源目录不存在或缺 `files` 字段失败。
  灾难恢复允许生产原件目录已丢失：仍保护其 canonical 路径与祖先/后代，持久卷边界必须存在，
  restore/verify 的隔离目标仍必须是真实存在/新建的合法目录。
  备份源库名和恢复库名本就不同，不拿两者相等当验收条件，只验证实际目标身份。
- PostgreSQL 清单另含 `sequences`（last_value/is_called、类型/start/increment/min/max/cache/cycle/owned列）
  和 `database_schema`（关系/视图定义、列类型/default/null/identity/generated/collation、constraint、index 定义与状态）。
  定义不纳入数据库名、OID 或 owner，确保跨恢复目标稳定；列序用逻辑位置，忽略 DROP 列后的物理 attnum 空洞。
  仅对 PG 已知无 typmod varchar 字面量 ARRAY → text[] 的 dump/restore 改写作窄规范，保留其它 cast 与字段值。
  sequence CACHE>1 保留服务端持久 last_value/is_called，
  不承诺保留已断开连接的客户端预取号段；当前真实 PG 回归验证 Django 默认 CACHE 1。
  当前明确支持 `public` 应用 schema；存在其他用户 schema 关系即 fail closed，避免只验部分表却宣称完整。
  不涵盖角色/ACL、扩展实现、存储过程 body、触发器 body、RLS/策略的完整语义审计，不声称任意 PostgreSQL 对象全量等价。

云端部署、备份恢复与演练的操作说明在 [deploy/README.md](../../deploy/README.md)；
这里只说明脚本本身怎么用、边界在哪。

## 两种执行模式

| 模式 | 什么时候用 | 怎么做 |
|---|---|---|
| `compose`（默认） | 云端测试环境，容器里跑 | 需要 `docker` + `docker compose`，读 `deploy/.env` |
| `native` | 本机演练脚本逻辑，或没有 Docker 的机器 | 直接对本机 PostgreSQL + 本机 venv 操作 |

```bash
# compose 模式（云端）
scripts/operations/drill.sh

# native 模式（本机演练，示例）
BACKUP_MODE=native \
CUSTOMS_PG_HOST=127.0.0.1 CUSTOMS_PG_PORT=55432 \
CUSTOMS_PG_NAME=customs_agent CUSTOMS_PG_USER=customs_agent \
CUSTOMS_PG_ADMIN_USER=postgres \
CUSTOMS_FILE_ROOT=/tmp/p22-files \
scripts/operations/drill.sh
```

`native` 模式里建库/删库用 `CUSTOMS_PG_ADMIN_USER`（默认 `postgres`），
业务角色 `CUSTOMS_PG_USER` 不授予 `CREATEDB`——这也是生产环境该有的权限分工。

## 脚本清单

| 脚本 | 做什么 | 破坏性 |
|---|---|---|
| `backup.sh` | 执行停写/锁保护 → `pg_dump -Fc` → 打包文件 → 前后指纹一致 → manifest + 一致性证据 | 不修改业务数据；native 暂时阻塞写入，Compose 暂停原运行服务 |
| `restore.sh` | 校验 manifest → 建**新库** → `pg_restore` → 解包到**新目录** | 只写入新目标；目标已存在即拒绝 |
| `verify_restore.sh` | 统计恢复目标的表行数与文件哈希，和备份比对 | 无 |
| `drill.sh` | 备份 → 恢复 → 比对 → 清理隔离目标 | 清理只删本次演练新建的库/目录 |
| `table_counts.py` | 输出表行数 + 文件 sha256 清单（JSON） | 无 |
| `compare_counts.py` | 判定两份清单是否一致（退出码 0/1） | 无 |

## 约定

- 备份目录默认 `deploy/backups/<UTC时间戳>-<标签>/`，已被 Git 忽略；**不要**提交进仓库。
- `incoming/` 暂存区不进备份：那是“上传已落暂存、事务未提交”的半成品。
- 恢复**不覆盖**正在用的库或文件目录；要回到旧数据请用“换指向”的方式（见 deploy/README.md 第 8.2 节）。
- 任何脚本都不做降级迁移；结构回退靠备份 + 换库，不靠 `migrate --fake`。
- macOS 自带 bash 3.2：脚本里变量紧跟中文时必须写 `${VAR}`，否则变量名会被认成包含中文字节。
  `tests/p22/test_operations_scripts.py` 会检查这一条。
