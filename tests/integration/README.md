# P21 独立技术验收

从仓库根目录执行。下文的 `python` 指已安装本项目锁定依赖的 Python 解释器；Windows 可改为实际虚拟环境的 `Scripts/python.exe`，POSIX 可改为 `.venv-customs/bin/python`。工程安装、数据库和账号准备见 [运行与交接说明](../../docs/开发文档/运行与交接说明.md)。

本目录验证当前代码的跨模块行为。测试输入为合成工作簿，AI 固定为 mock；不能据此确认真实 HS、退税率、监管结论或关务业务质量。

## 1. SQLite 集成验收

```sh
python tests/integration/run.py --backend sqlite
```

入口实际调用 `manage.py test tests/integration --noinput --verbosity 2`。`--verbosity` 可选 `1` 或 `2`。具备 make 的开发环境也可运行：

```sh
make test TASK=P21
make test-integration
```

`run.py` 清除继承的 `CUSTOMS_*`、`DJANGO_*` 配置，不读取工作区 `.env`；为子进程固定 mock AI、inline 执行器、mock 通知和 live API 页面模式，关闭历史参考的自动接线，并使用临时 SQLite 路径、临时受控文件根和随机测试密钥。临时文件根在运行结束后清理。

默认报告为 `.local/p21/integration-sqlite.json`，完整输出为同名 `.log`。也可显式指定报告位置：

```sh
python tests/integration/run.py --backend sqlite --verbosity 1 --output .local/p21/integration-sqlite-recheck.json
```

只有测试进程退出码为 0 且实际运行测试数大于 0 时，报告才为 `passed`。失败、配置不合法或未发现测试均返回非零。报告列出实际测试数、跳过数、运行模式、时间和未验证范围；跳过项须另查日志，不能计作已验证。

## 2. PostgreSQL 集成验收

先准备独立、本机的 P21 PostgreSQL 测试集群和具备创建测试库权限的账号。入口只接受名称匹配 `customs_p21*` 或 `p21_*` 的明确数据库，以及 `127.0.0.1`、`localhost` 或 `::1`。未配置 `CUSTOMS_PG_USER` 会直接失败。

以下 PowerShell 示例对应本轮隔离环境的连接事实；在其他本机环境中应填写实际的 P21 测试库、账号和端口：

```powershell
$env:CUSTOMS_PG_NAME = 'customs_p21'
$env:CUSTOMS_PG_USER = 'customs_p21'
$env:CUSTOMS_PG_HOST = '127.0.0.1'
$env:CUSTOMS_PG_PORT = '55421'
python tests/integration/run.py --backend postgres
```

`CUSTOMS_PG_PASSWORD` 仅在实际鉴权需要时通过进程环境提供。该入口只透传这五个 PostgreSQL 连接变量，不把其他业务配置带入测试。Django 创建和销毁自己的测试数据库；不要并发运行使用同一测试库名的多个测试进程。

默认报告为 `.local/p21/integration-postgres.json` 和同名 `.log`。make 等价入口为：

```sh
make test TASK=P21 P21_BACKEND=postgres
make test-integration P21_BACKEND=postgres
```

两种数据库入口均使用 inline 执行器。PostgreSQL 测试通过本身不能证明真实 Redis、独立 worker 或 dispatcher 已运行。

## 3. 扩大核心回归

```sh
python tests/integration/run.py --backend sqlite --core-regression
python tests/integration/run.py --backend postgres --core-regression
```

实际范围是 `tests/p00` 至 `tests/p20`，另加 `tests/p15_pg`、`tests/p16_real`、`tests/p23` 和本目录。P22 容器部署及恢复演练不在该范围内。默认报告分别为 `.local/p21/core-sqlite.json`、`.local/p21/core-postgres.json` 及同名日志。

部分 P20 专项需要可导入 openpyxl 的独立解释器。若当前运行环境不含该库，可通过已有的 `CUSTOMS_OPENPYXL_PYTHON` 配置实际解释器；`run.py` 会显式保留这个变量。没有相应条件时须查看跳过或失败原因，不能只看新增集成测试的结果。

## 4. 真实 HTTP／Redis／独立 worker 验收

`queue_probe.py` 是单独的有状态演练脚本。它在专用 P21 应用和数据库中创建合成任务，启动自己的 worker 和 dispatcher，并对其子进程注入中断和单行失败。它不是 `run.py` 自动运行的一部分。

先准备 PostgreSQL、Redis、应用进程和 `seed_demo` 演示账号。应用与探针及其子进程必须共享同一套专用文件根、测试密钥和以下明确配置；脚本不会修改已经运行的应用配置：

| 环境变量 | 探针要求的值 |
|---|---|
| `CUSTOMS_DB_BACKEND` | `postgres` |
| `CUSTOMS_PG_NAME` | `customs_p21` |
| `CUSTOMS_PG_USER` | `customs_p21` |
| `CUSTOMS_PG_HOST` | `127.0.0.1` |
| `CUSTOMS_PG_PORT` | `55421` |
| `CUSTOMS_EXECUTOR_MODE` | `rq` |
| `CUSTOMS_ENGINE_MODE` | `mock` |
| `CUSTOMS_NOTIFICATION_MODE` | `mock` |
| `CUSTOMS_REDIS_URL` | `redis://127.0.0.1:63821/0` |
| `CUSTOMS_QUEUE_NAME` | `customs-p21` |
| `CUSTOMS_QUEUE_TIMEOUT` | `15` |
| `CUSTOMS_QUEUE_LEASE_SECONDS` | `20` |
| `CUSTOMS_QUEUE_REDELIVERY_SECONDS` | `3` |
| `CUSTOMS_RETRY_BACKOFF_SECONDS` | `1` |
| `CUSTOMS_ROW_MAX_ATTEMPTS` | `3`（项目默认也为 3） |
| `DJANGO_SECRET_KEY` | 本隔离环境专用的非空测试密钥 |

专用存储位置通过项目现有的 `CUSTOMS_FILE_ROOT` 配置，并在 app 和探针终端中保持一致。探针自身阻止读取 `.env`；准备应用及其他管理进程时也应使用明确的隔离配置，可设置项目已使用的 `PYTHON_DOTENV_DISABLED=1`。不要使用 Python 的 `-O` 或 `PYTHONOPTIMIZE`，探针要求断言启用。

仅在上述独立环境中初始化应用，保持应用在一个终端运行：

```sh
python manage.py migrate --noinput
python manage.py seed_demo
python manage.py runserver 127.0.0.1:8061 --noreload
```

应用地址固定为 `http://127.0.0.1:8061`。探针使用 `supplier_a`、`operator_a`、`expert` 及 `seed_demo` 的本地演示密码。运行探针前停止该专用队列已有的外部 worker 和 dispatcher，避免它们抢走故障注入目标；应用进程和专用 PostgreSQL／Redis 保持运行。在共享同样配置的另一终端执行：

```sh
python tests/integration/queue_probe.py
```

脚本会验证上传先返回、worker 实际执行、被终止 worker 的中断尝试和恢复、提交后自动专家核查、单行最多三次失败及人工重试、同版重跑幂等、原件哈希，以及数据库提交后请求进程退出的恢复。

默认不执行 Redis 服务中断。若需要该项，必须同时传入 `--redis-server` 和 `--redis-data-dir`，参数分别为实际 Redis server 可执行文件和该运行实例专有的数据目录：

```powershell
python tests/integration/queue_probe.py --redis-server $p21RedisExecutable --redis-data-dir $p21RedisDataDir
```

上例两个 PowerShell 变量须先赋为实际绝对路径。数据目录只接受项目 `.local/p21/` 内的目录，或系统临时目录下包含 `customs-p21-runtime-` 路径段的目录；还须与运行中 Redis 的 `CONFIG GET dir` 完全一致。探针在写入合成任务前检查，并在停止 Redis 前再次核对。只给一个参数、目录不匹配或实例身份不符都会拒绝执行。实际中断会保存并重启这个专用 broker；不传参数时，结果中相应检查保留 `not_executed_no_owned_broker_restart_configuration`。

结果写入 `.local/p21/queue/result.json`，子进程日志和合成工作簿也在该目录。每次运行会更新结果文件，需留存上一轮证据时先另存。脚本结束会停止自己启动的 worker／dispatcher；创建的合成业务记录保留，恢复后的专用 Redis 也保持可用。

`--worker-mode`、`--fault-run`、`--marker`、`--commit-crash-result` 是探针生成子进程时使用的参数，通常不需要手动调用。完整参数以实际帮助为准：

```sh
python tests/integration/run.py --help
python tests/integration/queue_probe.py --help
```

## 5. 证据范围与本轮状态

截至 2026-11-20 本次平台修复后的状态：**技术全量回归通过；P21 集成待专业与人工验收**。下表分别记录已完成的专项与历史失败轮；全量运行 1981 项、0 failures/errors、6 skipped，退出 0；跳过范围在下方列明。

| 验证层 | 本轮实际结果 | 结论范围 |
|---|---|---|
| 平台修复后集成，SQLite | 29 项通过，0 跳过，退出 0；16:08 完成 | `.local/p21/integration-sqlite-platform-fixed.{json,log}`；mock AI、inline |
| 平台修复后集成，PostgreSQL | 29 项通过，0 跳过，退出 0；16:08 完成 | `.local/p21/integration-postgres-platform-fixed.{json,log}`；真实 PostgreSQL、mock AI、inline |
| 专用队列探针 | 8 项通过，含实际 broker 中断恢复 | Windows 本机 PostgreSQL＋Redis＋独立 worker／dispatcher，mock AI |
| P12 原生平台专项 | 53 项通过，0 跳过 | 实际 Git Bash 执行脚本、Unicode／空格路径和真实 NTFS junction 越界拒绝；`.local/p21-runtime/p12-platform-sqlite.log` |
| P15 发布／读取专项 | 51 项通过，0 跳过；另有 3 项时序负控制与对应修复回归通过 | Windows 原子 `.current.json`、真实子进程退出、读者固定 generation、CAS、篡改及 junction 拒绝；`.local/p21-runtime/p15-platform-sqlite.log`、`.local/p21/p15-reader-negative-controls.json` |
| P23 原生发布／锁及相关定向专项 | 106/106、0 跳过，含 P23 发布／审计／锁／激活／回滚、P16 reference 与 P14 依赖约束 | `.local/p21/p23-final-gates.log`；实际线程／进程互斥、杀进程后释放、单次固定 release 和提交后读者观察。额外发布门独立负控制 8/8：`.local/p21-runtime/p23-independent-gates.json` |
| Windows 私有报告 ACL | 原生 4/4 通过；真实 PostgreSQL 专项 41 项运行、39 通过、2 条件跳过 | 独立安全描述符读取与受限 token AccessCheck；旧 `0600` 写法被真实 ACL 断言拒绝；`.local/p21/private-report-acl.json`、`p15-private-report-postgres.json` |
| P15 数据库／bundle 输入 | 数据库入口 3/3；bundle 与显式历史入口合跑 36/36，均 0 跳过 | 初次无平面文件、保留旧平面文件、坏指针不回退；`.local/p21/p15-snapshot-input-sqlite.json`、`p16-history-input-sqlite.json` |
| 全量 PostgreSQL 最终复验 | **1981 项，0 failures/errors，6 skipped，退出 0**；648.054 秒 | `.local/p21/core-platform-fixed.{json,log}`；16:11:24–16:22:26，P00–P20、P15_pg、P16_real、P23、P21 |

全量的 6 个条件跳过分别是：2 项 SQLite 专属保护检查（已另用 SQLite 跑过，2/2 通过，见 `.local/p21/sqlite-only-guards.{json,log}`）；P08 可选 Chromium 测试；P15 缺本地真实汇总表；P23 未启用真实原件导入；P05 需要特权的文件 symlink 越界用例。后四项不计为通过，Windows junction 边界另有 P12/P15/P23 的真实 OS 专项证据。

历史失败轮保留如下，均发生于本次 Windows 平台实现与额外门禁修复之前：

| 历史时点（北京时间） | 当时实际结果 | 保留证据 |
|---|---|---|
| 15:16–15:21 首轮 | 1794 项；9 失败、158 错误、10 跳过，退出 1 | `.local/p21/core-postgres.{json,log}`、`core-failures.{json,md}` |
| 15:34–15:38 前一轮修复复验 | 1802 项；3 失败、156 错误、10 跳过，退出 1 | `.local/p21/core-postgres-recheck.{json,log}`、`core-failures-recheck.{json,md}` |

1802 轮当时的 `fcntl` 导入、symlink 权限、直接执行 Bash 和 POSIX `0600`／Windows ACL 语义问题已通过真实平台实现及专项复验处理。该轮数字仅描述当时发现的测试，无法导入模块中未被发现的内部测试不能据此计作通过。机器报告和日志位于 `.local/p21/`、`.local/p21-runtime/`；详见 [P21 回交](../../docs/开发文档/交接/P21.md) 和 [验收记录](../../docs/开发文档/验收记录/P21-独立回归-20261120.md)。

补充的 6 项 journey 覆盖 T01 初次与替换上传的致命解析失败、A12 品名／要素／原 HS／图片变更、A13 仅补事实、A16 已审批版本拆分后再合并，以及 T07 审批和退回后的通知超时与相同请求重放。T07 只在 `MockDwsTransport.send` 边界注入超时，实际走决定事务、站内消息、outbox 和投递失败持久化，并阻止网络连接；它证明通知失败不回滚业务决定和幂等去重，不证明真实 DWS 送达。补充后的 SQLite 报告为 `.local/p21/integration-sqlite-expanded.json` 及同名日志。

本目录的技术通过不替代以下项目：真实模型及专家金标准专业质量、Jay／关务专家签收、完整人工浏览器走查、Microsoft Excel／WPS 客户端兼容、真实 DWS 送达、Linux／Docker 运行和 P22 新环境恢复。A17 在新增 journey 中只覆盖原件和媒体字节子集，完整公式／合并／其他页签保真还需结合 P03／P20 专项与客户端实际验收分别记录。
