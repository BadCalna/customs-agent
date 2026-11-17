# SQLite → PostgreSQL 切换（P12）

推荐日常业务使用 PostgreSQL。Django 与数据库脚本读取同一份 `.env`，已有进程环境优先，禁用 dotenv 插值；显式选择 PostgreSQL 但缺少连接配置时会报错，不会回落到 SQLite。未配置后端的新检出仍可用 SQLite 做快速开发。

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

只需新演示环境时可用 `bootstrap.sh --with-demo`；迁移现有数据时，目标必须是未 seed 的空库。开发角色有 CREATEDB 供 Django 创建独立测试库。本机 trust 认证仅适用于本机开发；共享部署按 D6 使用口令认证。

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

先停止该部署所有 web、worker 和其他写入者，保持停写直至备份恢复演练完成。不要停止其他工作树的应用或共用集群。保留源 SQLite 及附件根，初始化一个全新 PG 目标，然后执行：

```sh
scripts/database/copy_sqlite_to_pg.sh /absolute/path/source.sqlite3
```

脚本会：

1. 通过 SQLite backup API、只读源连接生成独占快照，包含 WAL 中已提交的行；不复制活动数据库文件、不改源库。
2. 在补迁移**之前**拒绝当前代码不认识的已应用迁移、非空表和业务列；在副本上补齐本分支迁移。使用带未合并模型字段的工作台 WIP 库时，需先集成对应业务分支，不能忽略字段迁移。
3. 用 Django registry 选择全部 managed 模型，包含 P06 `ImportJob`/`TaskClaim`、用户组、管理员日志和会话。生成元数据 ContentType/Permission 由 migrate 创建，引用用自然键解析；不能由当前代码重建的自定义或旧元数据即使没有被引用也拒绝迁移，避免静默丢弃。所有 M2M 关系随 fixture 保留。
4. 检查附件非空路径是受控文件根内的可读文件，并在接触目标 PG 前保存 `source-files.json`（路径/大小/SHA-256）。缺失、目录、路径越界及符号链接逃逸均失败，不能警告后继续。
5. 要求目标迁移齐全并拒绝所有非空业务表，严格 `loaddata` 导入，不忽略未知字段；校正全部模型整数主键序列。
6. 比较所有模型及自动 M2M 表的行数和内容摘要，核对必填/可空外键。规范化 JSON 类型、UUID、微秒和时区，完整保留浮点精度；生成元数据 FK 比自然键，自动 through 表比关系而不比生成 ID。重新计算原件/图片实体大小及 SHA-256，必须与导入前清单一致。

每次的源升级副本、fixture、迁移日志、核对 JSON 都独占保存在 `.local/p12/copy-<时间>-<随机>/`，权限限制为当前用户。重跑时应换全新目标库。仅在明确可清空的开发目标上，使用 `CUSTOMS_PG_ALLOW_FLUSH=<精确库名>` 配合 `--fresh`；不建议用该模式做正式切换。

核对脚本的 `--source` 应指向**该次升级后的快照**，并且必须提供该次导入前清单：

```sh
.venv-customs/bin/python scripts/database/verify_copy.py \
  --source .local/p12/copy-<该次目录>/source-upgraded.sqlite3 \
  --files-manifest .local/p12/copy-<该次目录>/source-files.json
```

只核对目标内部完整性可用：

```sh
.venv-customs/bin/python scripts/database/verify_copy.py --skip-source
```

`--skip-source` 不证明源/目标等价。附件报告只统计原有非空引用；老合成样例的空路径不代表有真实文件。完整文件目录的恢复由下面的 P22 工具核验。

## 数据库 + 附件完整备份恢复

P12 备份入口复用已经合入主线的 P22 实现：

```sh
make db-backup
# 或 scripts/database/backup.sh <label>
```

它先保留 SQLite 的一致性回退快照（源存在时），再执行 native PG + 文件备份。PG 不可达、文件根不存在、dump/归档/指纹失败都返回非零；完整产物包含 `COMPLETE`、manifest、PG dump、文件归档、全表/内容/schema/sequence 与文件指纹。

native 模式**无法自动停写**，运行者必须先停止该部署写入者。P22 编排入口本身不读取 `.env`，直接调用前先通过公共配置入口显式导出：

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
