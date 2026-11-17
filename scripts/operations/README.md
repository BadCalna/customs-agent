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
  restore 拒绝未完成/失败备份。新增 JSON manifest 精确覆盖 dump/archive/counts；传统 SHA256 清单保留便于人工检查。
- Compose 从 `docker compose config --format json` 取得实际 app 数据库和文件根，
  不依赖 db 容器的初始化 POSTGRES_DB 等于当前 app 库名。原件根必须处于可写持久卷内，
  恢复目标必须在同卷边界内且与原件根无相同/祖先/后代关系；容器内再解析实际路径，拒绝符号链接和越界。
  默认目标在卷内预建的可写 `restores/restore-<stamp-random>` 下（`OPERATIONS_RESTORE_ROOT` 可设置另一卷内父目录）；
  从已恢复目录切换为新生产根后，后续恢复仍创建 sibling，不能创建其祖先/后代。
  native 的恢复边界默认原件根的父目录，可通过 `OPERATIONS_RESTORE_ROOT` 收紧。
- 恢复只创建全新库和全新目录（已存在的空目录也不抢占）；拒绝生产库、系统库、SQL 非安全标识符、超长名称。
  解包拒绝绝对路径、`..`、软/硬链接和特殊文件。`pg_restore --exit-on-error`；失败只按 OID/inode 身份清理自身创建目标。
- 停写前记录运行服务；除 db/redis 外全部停止（含 worker 和未来写入者），只 `start` 原运行服务，不 `up` 意外启动原来关闭服务。
  文件归档始终使用一次性 app 容器，绝不 exec 已停止的 app。SIGTERM/SIGINT 走恢复 finally；SIGKILL/主机掉电仍需按 services-before 证据人工处理。
- 整个 deployment 的 backup/restore/verify/drill 使用非阻塞排他锁，防止并发操作；不同备份目录或切换 app 库名不绕过同 Compose 项目锁。
- 演练成功才默认清理；任何阶段失败保留隔离目标、JSON 状态及终端日志。`--keep` 让成功演练也保留。
- 清单包含数据库实际 current_database/OID、Django 实际文件根/inode、全部 introspected 表
  （含 `django_migrations`，仅排除 `django_session`）、表行数及逐行稳定 SHA256 内容指纹、文件路径/大小/SHA256。
  直接从 cursor 逐批读取并保留 datetime 六位微秒，不用 dumpdata；每行哈希排序避免物理行序差异。
  指纹的排序占 O(行数) 内存；dump、tar 和介质 SHA256 均流式。新恢复契约不接受缺指纹/缺文件清单降级。
  独立 compare_counts 可读取旧版行数清单，但明确警告仅行数验证，不能作为新版恢复验收通过依据。
- 合法空原件目录允许（`files: []`、`file_count: 0`）；不存在目录或缺 `files` 字段失败。
  备份源库名和恢复库名本就不同，不拿两者相等当验收条件，只验证实际目标身份。

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
| `backup.sh` | 停写（默认）→ `pg_dump -Fc` → 打包文件 → 行数/哈希清单 → manifest | **无**（只读；不停写时用 `--no-quiesce`） |
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
