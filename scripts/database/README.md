# PostgreSQL 本机集群与切换（P12）

`scripts/database/` 下所有脚本都 source `pg_env.sh`，路径与端口只有一处定义。

- 集群数据目录放在仓库内 `.local/pgdata`，**不复用** Homebrew 默认集群
  （`/opt/homebrew/var/postgresql@17`），避免与其它项目或全局服务共用一个数据目录。
- 端口默认 `5432`，可被 `CUSTOMS_PG_PORT` 覆盖；只监听 `127.0.0.1`，不对外网开放。
- 本机开发集群用 `trust` 认证，因此仓库里不保存任何数据库口令；共享或生产环境必须改成
  `scram-sha-256` 并单独管理口令（D6 第 3 节）。
- `.local/` 已在 `.gitignore` 中，集群数据、日志、备份都不会进 Git。

| 变量 | 默认值 | 用途 |
|---|---|---|
| `CUSTOMS_PG_BIN` | 自动探测 `postgresql@17` 或 `pg_config --bindir` | PostgreSQL 可执行文件目录 |
| `CUSTOMS_PGDATA` | `.local/pgdata` | 集群数据目录 |
| `CUSTOMS_PG_PORT` | `5432` | 监听端口 |
| `CUSTOMS_PG_SOCKET` | `.local/pgsocket` | Unix socket 目录（避免与系统 socket 混用） |
| `CUSTOMS_PG_LOG` | `.local/pg.log` | 服务日志 |
| `CUSTOMS_PG_NAME` | `customs_agent` | 业务库名 |
| `CUSTOMS_PG_USER` | `customs_agent` | 业务登录角色 |
| `CUSTOMS_PG_SUPERUSER` | `postgres` | 本机集群超级用户（仅脚本内部建库用） |

## 首次安装（macOS / Homebrew，无需 Docker）

```sh
brew install postgresql@17           # 只装可执行文件；不使用 brew services
scripts/database/bootstrap.sh        # 初始化集群 → 启动 → 建角色/库 → make migrate
```

`bootstrap.sh` 幂等：集群、角色、库已存在时跳过，不删除任何已有数据。加 `--with-demo`
会在 migrate 后跑 `seed_demo` 与 `seed_contracts`，得到可重复的合成演示数据。

## 日常命令

| 操作 | 命令 |
|---|---|
| 初始化集群 | `make pg-init`（`scripts/database/pg_ctl.sh init`） |
| 启动 / 停止 / 状态 | `make pg-start` / `make pg-stop` / `make pg-status` |
| 看日志 | `scripts/database/pg_ctl.sh log [行数]` |
| 切换后端 | `.env` 里写 `CUSTOMS_DB_BACKEND=postgres` 与 `CUSTOMS_PG_*` |
| 迁移检查 | `make check-postgres`（后端不是 PostgreSQL 时直接失败） |
| 双库对照 | `scripts/database/dual_db_check.sh` |
| 备份 | `make db-backup` → `.local/backups/<时间戳>-<标签>/` |
| 恢复到独立库 | `scripts/database/restore.sh <dump> <目标库名>` |

不设置 `CUSTOMS_DB_BACKEND` 时后端仍是 SQLite，默认行为对既有开发流程零影响；
`CUSTOMS_DB_BACKEND=postgres` 但缺 `CUSTOMS_PG_NAME`/`CUSTOMS_PG_USER` 时启动即报
`ImproperlyConfigured`，不会静默回退。

## 保留现有 SQLite 数据（本项目的实际选择）

本项目选择**保留并迁移比对**，而不是丢弃重建演示数据。顺序固定为：

```sh
make db-backup                                # 1. 先备份 SQLite 与 PG，只读不删源库
scripts/database/copy_sqlite_to_pg.sh --fresh # 2. 按依赖顺序导出/导入并自动核对
scripts/database/verify_copy.py --source .local/db.sqlite3 --json .local/p12/verify.json
```

`copy_sqlite_to_pg.sh` 负责：

1. 用 `dumpdata` 从 SQLite 只读导出，按父表在前的顺序排列 19 个模型；
2. `loaddata --ignorenonexistent` 导入 PostgreSQL；
3. `sqlsequencereset` 校正 `auth.Group` 等整数主键的序列；
4. 调 `verify_copy.py` 做数量、逐行内容（JSON/时间/UUID/浮点归一后摘要比对）、外键孤儿和附件引用核对。

`--fresh` 会先 `flush` 目标库，只允许对本机开发库使用；不加该参数时目标库有数据就会因主键/唯一
约束报错，这是保护而不是缺陷。

## 回退到 SQLite

旧 SQLite 文件从头到尾没有被删除或移动，因此回退只需：

```sh
# .env 中删除或改成 CUSTOMS_DB_BACKEND=sqlite / CUSTOMS_SQLITE_PATH=.local/db.sqlite3
make pg-stop        # 需要时停掉本机集群
```

不通过降级迁移回退：PostgreSQL 里新建的行不会自动回到 SQLite。确需带走数据时，用
`backup.sh` 的 dump 与新库恢复演练，而不是反向改写旧库（D6 第 3 节）。