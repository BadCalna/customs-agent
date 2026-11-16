# 运维脚本（P22）

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
