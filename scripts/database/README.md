# SQLite → PostgreSQL 切换（P12）

推荐日常业务使用 PostgreSQL。Django 与数据库脚本读取同一份 `.env`，已有进程环境优先，禁用 dotenv 插值；显式选择 PostgreSQL 但缺少连接配置时会报错，不会回落到 SQLite。`.env.example` 默认启用 SQLite，`cp .env.example .env; make setup; make migrate; make dev` 可直接启动；PostgreSQL 示例保持注释，按下面步骤初始化后再启用。

## 本机启动

先 `make setup`，安装 Homebrew PostgreSQL 17（无需 Docker，也不用 `brew services`）。本项目单独管理 `.local/pgdata`，默认只监听 `127.0.0.1`。将下列连接配置写入 `.env`，有端口冲突时选择其他端口：

```dotenv
CUSTOMS_DB_BACKEND=postgres
CUSTOMS_PG_NAME=customs_agent
CUSTOMS_PG_USER=customs_agent
CUSTOMS_PG_HOST=127.0.0.1
CUSTOMS_PG_PORT=5432
CUSTOMS_FILE_ROOT=.local/files
CUSTOMS_SQLITE_PATH=.local/db.sqlite3
```

```sh
scripts/database/bootstrap.sh       # 幂等初始化、启动、建角色/库、migrate
make check-postgres                 # 必须实际连上 PG；P12/P05/P06/P09/P10
make dev                            # 使用 .env 的 PostgreSQL 配置
```

只需新演示环境时可用 `bootstrap.sh --with-demo`；迁移现有数据时，目标必须是未 seed 的空库。开发角色有 CREATEDB 供 Django 创建独立测试库。本机 trust 认证仅适用于本机开发；`pg_ctl.sh init/start/restart` 只接受单个回环 IP（127/8 或 `::1`），在建目录/启停前拒绝通配、外网地址、主机名或地址列表。通用连接脚本仍允许远端 PG；共享部署按 D6 使用口令认证。

| 配置 | 默认值 / 说明 |
|---|---|
| `CUSTOMS_PG_BIN` | 探测 Homebrew `postgresql@17` 或 `pg_config --bindir` |
| `CUSTOMS_PGDATA` | `.local/pgdata` |
| `CUSTOMS_PG_SOCKET` | `/tmp/customs-pg-<uid>-<repo摘要>`，避免长路径超过系统 socket 限制 |
| `CUSTOMS_PG_LOG` | `.local/pg.log` |
| `CUSTOMS_PG_SUPERUSER` | `postgres`，仅本机建库用 |
| `CUSTOMS_PG_ADMIN_USER` | 运维建恢复库角色，默认同本机超级用户 |
| `CUSTOMS_PG_PASSWORD` | 业务角色口令，可选 |
| `CUSTOMS_FILE_ROOT` | `.local/files`，相对路径按仓库根解析 |

`make pg-start`、`make pg-stop`、`make pg-status` 管理该工作树的集群；`scripts/database/pg_ctl.sh log` 看日志。macOS 初始化采用 mmap/posix 共享内存，支持沙盒环境。复制、备份和恢复只连接配置的目标，不擅自启动另一个集群。

## 保留 SQLite 数据并切换

切换前先在原部署排查并完成所有导入。错误会列出未完成 ImportJob 的 ID/状态：在原数据库、原附件根用原幂等键重试，或运行 `python manage.py resume_import <import_id>`；不能通过删除暂存区、删除记录或手工标记 succeeded 绕过。原件/回执已缺失时先修复恢复材料。完成后停止该部署所有 web、worker 和其他写入者，保持停写直至备份恢复演练完成。不要停止其他工作树的应用或共用集群。保留源 SQLite 及附件根，初始化一个全新 PG 目标，然后执行：

```sh
scripts/database/copy_sqlite_to_pg.sh /absolute/path/source.sqlite3 \
  --source-files-root /absolute/path/old-deployment/files \
  --target-files-root /absolute/path/new-deployment/files
# make 等价入口：make db-copy SOURCE_SQLITE=... SOURCE_FILES_ROOT=... TARGET_FILES_ROOT=...
```

脚本会：

1. 通过 SQLite backup API、只读源连接生成独占快照，包含 WAL 中已提交的行；不复制活动数据库文件、不改源库。
2. 在补迁移**之前**拒绝当前代码不认识的已应用迁移、非空表和业务列；在副本上补齐本分支迁移。使用带未合并模型字段的工作台 WIP 库时，需先集成对应业务分支，不能忽略字段迁移。
3. 用 Django registry 选择全部 managed 模型，包含 P06 `ImportJob`/`TaskClaim`、用户组、管理员日志和会话。生成元数据 ContentType/Permission 由 migrate 创建，引用用自然键解析；不能由当前代码重建的自定义或旧元数据即使没有被引用也拒绝迁移，避免静默丢弃。所有 M2M 关系随 fixture 保留。
4. 先拒绝 `staged`、`failed` 和未知导入状态；已被新版本成功替代的 `superseded` 须核对该版本正式原件的内容寻址路径及 SHA-256。`succeeded` 保留既有附件清单核验口径，兼容历史合成记录的空路径。再检查附件引用并在接触目标 PG 前保存 `source-files.json`（路径/大小/SHA-256）。未完成导入仍可能依赖 `incoming/` 原件或回执，不能当作零附件成功；缺失、目录、路径越界及符号链接逃逸均失败。
5. 要求目标迁移齐全，ContentType/Permission 必须与当前 migrate 的完整基线精确一致（含权限名称）；拒绝旧、自定义、缺失或改名的元数据和所有非空业务表。先检查全部目标附件冲突，再向显式目标根独占发布源清单中的实体：同内容可复用，内容不同不覆盖，源/目标不能互相嵌套；目标未指定时使用 `CUSTOMS_FILE_ROOT`，源根不可省略或猜测。只迁移数据库引用的实体；P22 完整备份另保存未引用的正式文件，`incoming/` 始终不随迁移或完整备份搬运。
6. fixture 按外键/M2M/自然键依赖排序，Task 在 TaskAssignment 之前；双向外键形成的分量保持稳定顺序，交给 `loaddata` 的延迟约束在事务末检查。严格导入，不忽略未知字段；校正全部模型整数主键序列。
7. 比较所有模型及自动 M2M 表的行数和内容摘要，核对必填/可空外键。规范化 JSON 类型、UUID、微秒和时区，完整保留浮点精度；生成元数据 FK 比自然键，自动 through 表比关系而不比生成 ID。分别重算源、目标根的原件/图片大小及 SHA-256，两边必须与导入前清单一致。

每次的源升级副本、fixture、迁移日志、核对 JSON 都独占保存在 `.local/p12/copy-<时间>-<随机>/`，权限限制为当前用户。重跑时应换全新目标库。仅在明确可清空的开发目标上，使用 `CUSTOMS_PG_ALLOW_FLUSH=<精确库名>` 配合 `--fresh`；不建议用该模式做正式切换。

核对脚本的 `--source` 应指向**该次升级后的快照**，并且必须提供该次导入前清单：

```sh
.venv-customs/bin/python scripts/database/verify_copy.py \
  --source .local/p12/copy-<该次目录>/source-upgraded.sqlite3 \
  --files-manifest .local/p12/copy-<该次目录>/source-files.json \
  --source-files-root /absolute/path/old-deployment/files \
  --target-files-root /absolute/path/new-deployment/files
```

只核对目标内部完整性可用：

```sh
.venv-customs/bin/python scripts/database/verify_copy.py --skip-source
```

`--skip-source` 不证明源/目标等价。附件报告先执行同一导入原件门禁，再统计非空引用；`superseded` 版本的原件必须随迁移保留并核对。完整文件目录的恢复由下面的 P22 工具核验。

## 数据库 + 附件完整备份恢复

P12 备份入口复用已经合入主线的 P22 实现：

```sh
make db-backup
# 或 scripts/database/backup.sh <label>
```

它先保留 SQLite 的一致性回退快照（源存在时），再执行 native PG + 文件备份。PG 不可达、文件根不存在、dump/归档/指纹失败都返回非零；完整产物包含 `COMPLETE`、manifest、PG dump、文件归档、全表/内容/schema/sequence 与文件指纹。

native 模式不管理进程生命周期，但会实际取得所有 public 表的 SHARE 锁、全部序列锁和本机附件排他锁，再导出 PG snapshot 给 `pg_dump`；锁持续到归档与前后内容/schema/sequence/文件指纹比较结束。在持锁期间、dump/归档之前，严格清单检查会拒绝全部未完成导入；Compose 模式也在停止写入者后做相同检查。遇到未完成导入、正在写入、缺锁、子进程失败或任何差异均失败，不生成 COMPLETE；`--no-quiesce` 被拒绝。一致性证据写入受 manifest 校验的 `meta.json`。

正式切换仍先停止该部署的写入者，并将应用/worker 重启到包含附件协作锁的新代码后再恢复运行。同机同用户的新 storage 写入受锁保护；旧进程、手工文件写入、其他主机或绕过 storage 的脚本不在协作锁边界内，备份期间不得运行这些写入及 schema 变更。该边界不承诺覆盖任意外部文件修改。

P22 编排入口本身不读取 `.env`，直接调用前先通过公共配置入口显式导出：

```sh
. scripts/database/pg_env.sh
export BACKUP_MODE=native
export PYTHON="$REPO_ROOT/.venv-customs/bin/python"
scripts/operations/restore.sh /absolute/path/backup-directory \
  --target-db customs_restore_20261117 \
  --target-files "$REPO_ROOT/.local/restore-20261117-files"
scripts/operations/verify_restore.sh /absolute/path/backup-directory \
  --target-db customs_restore_20261117 \
  --target-files "$REPO_ROOT/.local/restore-20261117-files"
```

恢复只准全新数据库和文件根，校验 OID/inode 身份并生成逐项 diff。详见 [P22 运维说明](../operations/README.md)。旧 `scripts/database/restore.sh <dump> <新库名>` 只处理独立数据库，永不删除已有库，也不构成附件恢复验收。

## 回退边界与双库回归

业务未在 PG 接受新写入前，可将后端改回原 SQLite 与原文件根。**PG 已接受新写入后，切回旧 SQLite 会丢掉切换后的业务变化**；此时先停写、保全 PG+文件完整备份，再恢复到独立 PG 目标并核对，不能直接改后端当作无损回退。

`scripts/database/dual_db_check.sh [测试目录...]` 在独立测试库分别执行两种后端，任意一边失败都非零。SQLite 会跳过 PG 事务锁/DDL 专项；并发验收以 PG 实跑为准。日志及差异报告位于 `.local/p12/`，不进 Git。
