# P22 follow-up：PR #24 补审修复与复审证据

## 状态与权限边界

- 原 PR：[#24](https://github.com/BadCalna/customs-agent/pull/24)，已合入 main `13b6835`；事后补审发现部署、备份恢复阻断。
- 本轮：`fix/P22-deploy-review`，从远端 main `13b6835` 起分支；只提交 follow-up PR，不合并、不部署、不访问真实服务器。
- 用户授权：修复，创建 follow-up PR，再独立对抗性审查。没有授权上线或改公开仓库可见性。
- 本文中的测试次数、审查结论及证据会在执行后补齐，未执行的项目保持未验证。

## 原报告纠错（不能把审查推断当实测）

1. 原补审摘要写“4 条 P1”但实际列了 5 条；本轮按五个阻断场景逐一测试。
2. 原摘要称备份权限 777/644，实际按默认 umask 是 **0755/0644**，本轮目标是目录 0700/文件 0600。
3. “本机没有 Docker 运行时”与“曾单独下载 Compose CLI 做 config”并非逻辑矛盾；只是原交接没有归档二进制/版本/命令输出，**缺证据不等于造假**。本轮不将作者未留痕自述算作已复验。
4. 空附件目录可以是合法业务状态；不能简单规定 file_count=0 即失败。必须区分“显式空清单”与“未统计/目录不存在/统计错目录”。
5. 恢复库名本来就应与备份源库名不同；比对器不能以二者不等认定失败。正确做法是确认实际连接的库名/文件根符合本次请求的恢复目标，再校验内容。
6. SQL/shell 字符串插值构成危险输入面，但原报告举例的 DROP DATABASE 在 PostgreSQL 多语句事务中未必可执行；本轮以输入拒绝测试作为防线，不谎称已复现删除生产库。
7. 仓库为 PUBLIC，既有业务底稿入库风险是独立事件；不在本 PR 中偷偷改可见性、清 Git 历史或对外复制业务文件。

## 修复范围

| 原问题 | 修复验收条件 |
|---|---|
| health Host=127.0.0.1 被公网白名单拒绝 | 必填真实白名单 + 回环探针行为测试；缺失、通配与占位输入明确拒绝 |
| stop app 后 exec 导致备份失败 | 停写期间用一次性容器读取共享卷，不启动写服务；行为桩检查调用顺序 |
| EXIT trap stdout 污染目录协议 | stdout 只输出机器结果，日志走 stderr；演练不解析混杂日志 |
| 文件根硬编码，回退后备错目录 | 配置与实际应用文件根一致；校验恢复数据库和文件根身份 |
| 备份未 gitignore、镜像漏根 .env | Git 与镜像边界分别覆盖，测试命中；本机配置存在不等于入库 |
| 恢复注入、路径逃逸、覆盖和失败残留 | 参数白名单、隔离目标、可验证所有权；失败只清本次创建的对象 |
| 清单假通过、行数相同无法检测内容变化 | 强校验清单结构和文件清单；稳定内容指纹，保留 datetime 微秒 |
| 权限、并发、留痕 | 私有权限，互斥保护，服务原状态恢复，失败证据保留 |
| TLS/日志/卷边界 | Host/Origin 端口一致、探针不被重定向、日志轮转、持久文件根约束 |
| 证据表述错误 | /static/、Compose config、迁移计数降级/更正；下表仅记录本轮实测 |

## 本轮实测环境

- 临时克隆：`/tmp/ca-review`，不依赖 OneDrive 工作区；不改其他 worktree 的未提交内容。
- Python 3.14（锁文件允许 >=3.12,<3.15）；项目 `.venv`，Django runner。
- Homebrew PostgreSQL 17；新建隔离实例，监听 **127.0.0.1:55529**。角色与数据库均 `p22_followup`，仅合成数据，无生产连接。
- 本机无 Docker 运行时。假 Docker 桩可以证明命令/状态处理逻辑，不能证明真实镜像构建、容器卷权限和网络行为。

## 执行记录

### 已执行的前置与对照（不代表修复版本验收）

- 非 P22 业务基线：显式列出 p00/p01/p02/p03/p04/p05/p07/p08/p09/p10/p11/p12/p14/p15/p20，**864 项通过，15 项 PostgreSQL 专项跳过**；日志 `p22-followup-baseline-other.log`（本机临时证据，未提交包含账号输出的原始日志）。
- 隔离 PostgreSQL 17：新建库执行迁移 **33 条**（Django 内置 + 项目迁移）成功；仅创建一个合成用户、一个合成任务和两个附件，原件路径含中文与空格。
- 合成任务时间精度：created_at `2026-11-17T03:00:00.123456Z`，updated_at `2026-11-17T03:00:00.654321Z`，用于确认恢复/内容指纹保留六位微秒。

### 修复版本验收

- 本轮 **真实 native PostgreSQL 17** `drill.sh --keep` 返回 JSON `status=passed`，备份与隔离恢复成功：**26 张表（包含 django_migrations、跳过 django_session）、2 个合成附件**，文件名含中文/空格；`incoming/` 被排除。
- 26 表与原交接“22 张模型表”口径不同：新版直接 introspection 枚举库中表，不只取注册模型；本轮清单实际 26，不改写成旧数字。
- 实际查恢复库：created_at/updated_at 分别保留 `.123456`/`.654321` 六位微秒；备份目录实测 0700，db.dump 0600。
- 负向对照 1：保持 reviews_task 行数不变，仅修改 title，verify 返回 1，错误明确为“数据库逐行内容指纹不同”。恢复原值再次 verify 返回 0。
- 负向对照 2：附件字节数不变，仅改一字节，verify 返回 1，错误明确为“文件内容不同（路径、大小或 SHA256 不一致）”。
- 原生演练不能证明真实 Compose 卷权限；主控已在静态复核时额外发现 app UID 对卷根无 mkdir 权限的问题，要求将恢复目标固定到专门可写 restores 子树后再独立复审。
- 冻结实现后显式全仓回归：**928 项通过（15 项仅 PostgreSQL 专项跳过）**；日志 `/tmp/p22-followup-full-suite.log`，22.892 秒。
- 最终 native `drill.sh`（不加 keep）再次 `status=passed, kept=false`；主控再查 `pg_database` 与目录，确认只清理本次恢复库/目录，源库/源原件保留。
- 最终 P22 集成：**64 项通过**，16.395 秒，含真实 shell subprocess 的 fake Docker/PG 流程、SQLite 清单回归和 Host/CSRF 行为用例；日志 `/tmp/p22-followup-specialist-final.log`。
- 隔离 PostgreSQL 后端全仓亦已运行（精确次数见下轮日志核对）；不把 SQLite 的15跳过等同于PG失败。
- `manage.py check` 无问题；`makemigrations --check --dry-run` 无变化；`git diff --check` 无空白错误。

## 对抗性审查

### 第一轮（head 5221a1e）结论：不通过，进入修复

两名未参与实现的独立审查员分别审 operations 与 deploy，固定读取 `5221a1e`，不修改源文件。不是 GitHub 人类 reviewer approval，不能冒充平台审批。

- **O-01 P2**：原件源根已丢失时，restore/verify 仍强制其存在，灾备被阻断。审查员在独立 audit_ PostgreSQL 库真实复现。
- **O-02 P2**：仅校验表数据和附件漏掉序列状态。恢复库 `setval(...,1,false)` 后 verify 仍返回 true，而 INSERT 立即主键重复；应补序列与 schema 元数据校验。
- **O-03 P2**：锁路径取 TMPDIR，不同环境的同一 deployment 可绕过互斥；审查员真实同机竞争复现。
- **D-01 P2**：README HTTPS 只复制配置而未明确替换 nginx 挂载，照文档会仍加载 HTTP。
- **D-02 P2**：旧卷根曾授 app，重建镜像不能修已有卷属主；升级说明缺精确 root 所有权迁移。
- **D-03 P2**：新服务器恢复直接 exec db，缺先启动并等待 db healthy 的步骤。

以上六项均接受为本轮必须闭环的问题，先补行为/文档回归后复审。两个附加建议：所有 Compose 命令统一显式 env-file（宿主shell同名变量仍需排查），合理 Host 后缀点兼容性留作改进；均不伪造真实 Compose 复现。

### 第二轮修复（尚待固定新 head 复审）

- D-01/D-02/D-03 已补：HTTPS 挂载替换、旧卷 root 所有权迁移、灾备先构建镜像/启动 db 等待 healthy 后再恢复；部署文档回归 17 项通过。
- O-01/O-02/O-03 已补行为测试，独立审查员在真实 PostgreSQL 复测三项均闭合：缺源根可恢复，sequence/constraint篡改被拒，不同TMPDIR竞争仍被拒。
- 主控完整 Django 迁移库复跑又发现正常 dump/restore 的物理 attnum、CHECK ARRAY cast 改写会产生假差异。改用逻辑列顺序与受限 builtin varchar字面量数组规范后，真实应用库恢复通过。
- 随后独立复审抓出规范化器可能修改 SQL 字符串值、把不同 CHECK 判成相同的 P2。新增 SQL lexer 跳过单/双引号、E-string、dollarquote与注释，保持字面量原文；精确反例已加回归，待独立复验确认。
- 此次审查总共发现并跟进7项P2（含新增规范化器问题）；不隐藏中间失败，不拿旧测试结果替代最终head。
- 初版 head 5221a1e 两种后端全仓均跑过 928 项；SQLite 15 项 PG专项跳过，隔离 PostgreSQL **928 项全过无跳过**（24.984秒）。这只是第一轮 head 的证据，不作为第二轮最终结果。
- 最终精确 head、专项/全仓计数与独立复审结论仍待补齐。

## 仍未验证（不得当作通过）

- 真实 Docker build/up、镜像运行用户和命名卷实际权限、app→web healthcheck 收敛。
- 真实 Compose 停写→备份→恢复→内容比对→服务恢复全过程。
- nginx 静态服务、TLS 证书和浏览器带端口 CSRF、HTTP→HTTPS 跳转。
- Docker PostgreSQL locale/collation、目标架构 Python 3.13 运行差异、gunicorn 锁文件外依赖。
- 独立服务器灾备恢复、异地加密备份、最小权限生产账号、队列 P13 接入。
- 工作台/台账若无法可靠读写，保留未同步，不得直接覆盖 JSON 或用本报告冒充同步成功。
