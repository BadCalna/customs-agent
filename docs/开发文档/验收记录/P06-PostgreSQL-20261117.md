# P06 / PR #22 独立 PostgreSQL 并发验收（2026-11-17）

测试对象：`feature/P06-live-upload-claim`，合并当前 main 后的工作树 `/tmp/customs-agent-p06`。
环境：macOS、Homebrew PostgreSQL 17.12、Python 3.14.4、Django 5.2.19、psycopg 3.3.9、Pillow 13.0.0。

## 独立集群

- 数据目录：`/tmp/customs-p06-pgdata`；端口 `55406`；Unix socket `/tmp/customs-p06-pgsocket`；只监听 `127.0.0.1`；本地 trust，仅供本机验收。
- 该集群与 P12 使用的 5432 集群相互独立，不复用其数据目录或连接。
- 环境共享内存限制：按默认配置执行 `initdb` 会在 bootstrap 阶段失败：
  `FATAL: could not create shared memory segment: Operation not permitted`，
  `DETAIL: Failed system call was shmget(...)`。这是本机 SysV 共享内存权限限制，**不能把默认配置的启动尝试记作通过**。
- 本轮改用 mmap/posix 共享内存实现启动独立集群，真实完成 PostgreSQL 多连接测试：

```sh
rm -rf /tmp/customs-p06-pgdata /tmp/customs-p06-pgsocket
mkdir -p /tmp/customs-p06-pgsocket
initdb -D /tmp/customs-p06-pgdata -U postgres --locale=C --encoding=UTF8 --no-sync \
  -c shared_memory_type=mmap -c dynamic_shared_memory_type=posix
pg_ctl -D /tmp/customs-p06-pgdata -l /tmp/customs-p06-pg.log \
  -o "-p 55406 -k /tmp/customs-p06-pgsocket -c listen_addresses=127.0.0.1" -w start
createuser -h 127.0.0.1 -p 55406 -U postgres --createdb customs_agent
createdb -h 127.0.0.1 -p 55406 -U postgres -O customs_agent customs_agent
```

## 结果

| 检查 | 结果 |
|---|---|
| `manage.py migrate --noinput`（独立 PG） | 全部迁移应用成功，退出 0 |
| `manage.py test tests.p06`（独立 PG） | **41 项，41 通过、0 跳过** |
| PostgreSQL 并发专项（`tests.p06.test_concurrency`） | **4 项全部通过**：两人认领一胜一 409、同人同键重放一次、同键并发上传只建一版一 Run、同键不同内容一胜一 409 |
| `manage.py test tests.p01 tests.p09 tests.p10 tests.p12`（独立 PG） | **278 项，278 通过、0 跳过** |

说明：

- `tests/p06/test_concurrency.py` 使用 `Barrier` + 多线程 + `close_old_connections()` 模拟多连接，不能由 SQLite 顺序测试替代。
- P12 并发提交用例在 P06 默认专家授权变更后需要显式准备任务专家；本轮补充测试夹具后，P12 5 项并发测试全部通过。产品行为不变：没有可用专家时提交返回 409 `default_expert_unavailable`，不留下无人可审的 pending。
- 本轮没有进行性能、压测、多机或生产网络条件下的并发验证；`mmap/posix` 是本机受共享内存权限限制时的启动方式，不改变 PostgreSQL 事务、行锁和唯一约束语义。

## 复现命令

```sh
CUSTOMS_DB_BACKEND=postgres CUSTOMS_PG_NAME=customs_agent CUSTOMS_PG_USER=customs_agent \
CUSTOMS_PG_HOST=127.0.0.1 CUSTOMS_PG_PORT=55406 \
.venv-pg/bin/python manage.py test tests.p06 --noinput

CUSTOMS_DB_BACKEND=postgres CUSTOMS_PG_NAME=customs_agent CUSTOMS_PG_USER=customs_agent \
CUSTOMS_PG_HOST=127.0.0.1 CUSTOMS_PG_PORT=55406 \
.venv-pg/bin/python manage.py test tests.p01 tests.p09 tests.p10 tests.p12 --noinput
```
