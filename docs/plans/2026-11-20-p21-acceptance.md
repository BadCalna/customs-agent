# P21 Independent Acceptance Implementation Plan

**Goal:** 在 main@353300b 上建立可复跑的独立验收入口，验证跨模块流程和关键安全不变量，记录真实运行证据、缺陷和业务待验项。

**Architecture:** 复用已发布 HTTP API、合成工作簿和现有引擎模式。新增 Django 集成测试，以真实 ORM 和临时磁盘验证跨模块行为；独立 PostgreSQL、Redis、worker 和 dispatcher 验证异步链路，浏览器实际操作正式页面。流程技术、真实模型专业质量、人工体验签收分别记录。

**Tech Stack:** Django/Ninja、unittest、PostgreSQL、Redis/RQ、Node、现有 Windows 本地运行时。

## 范围和选择

- 选择跨模块 HTTP 集成测试与独立进程队列演练，复用现有专项作为补充。
- 仅运行旧专项不能证明当前 main 的完整流程；仅浏览器走查又不足以确定版本、幂等和并发边界。因此保留三层证据。
- 原生 PostgreSQL + Windows SimpleWorker 属于本地技术验收；Linux/Docker 恢复演练归 P22，真实模型及关务金标准保持独立待验。
- 使用现有干净 linked worktree `.customs-agent-p21`，分支 `feature/P21-adversarial-review`，基线 `353300b`。Codex 附件接口未接受此已有 Git worktree，所有命令显式指定该绝对工作目录。

## Task 1: 隔离运行入口（主控）

文件：`tests/integration/run.py`、`tests/integration/README.md`、`Makefile`。

1. 提供 `make test TASK=P21` 与 `make test-integration`，以及跨平台直接 Python 命令。
2. 运行前固定 mock/inline、关闭 dotenv 读取，隔离数据库及文件根；PostgreSQL 仅接受显式本机 P21 测试库。
3. 缺环境、未发现测试或失败必须非零退出；输出日志和 JSON 报告，记录跳过及验证边界。

## Task 2: 业务流程集成（journeys Agent）

文件：`tests/integration/__init__.py`、`journey_support.py`、`test_review_journeys.py`。

1. 真实上传/认领/逐行确认/提交/人工决定；验证提交快照与独立建议分离。
2. 通过及专家改码；退回业务员/供应商；新版本重排增删、重新全表预审且不继承旧确认与通过。
3. 导出不改审批或通知；原件哈希、历史快照和消息收件人正确。

## Task 3: 对抗性边界（boundaries Agent）

文件：`tests/integration/test_adversarial_boundaries.py`。

1. 真实会话/CSRF、对象权限、文件图片及消息权限。
2. 幂等重放与冲突、旧 revision/跨版本/跨 Run 请求拒绝。
3. 无图/损坏不混淆、缺来源不升为已核、失败重试及迟到回写不污染新轮。

## Task 4: 实际队列与浏览器（runtime Agent + 主控）

本机证据写入 Git 忽略的 `.local/p21-runtime/`、`.local/p21/`；可复跑入口收口到 `tests/integration/`，浏览器步骤收口到 `tests/browser/`。

1. 新建 P21 专用 PostgreSQL 集群/Redis/任务队列，启动独立 app/worker/dispatcher。
2. 通过真实 HTTP 完整流程，验证队列重投/进程中断/恢复和局部失败重试。
3. 浏览器实际登录三个角色，观察上传/认领/确认/提交/审核/历史/导出及消息；记录可见实际结果。

## Task 5: 独立复核与回交（主控 + 未参与实现的审查 Agent）

文件：`docs/开发文档/交接/P21.md`、`docs/开发文档/验收记录/P21-独立回归-20261120.md`、D5 验收索引与任务状态。

1. 运行新增 SQLite/PG 集成及受影响核心专项；系统检查、迁移漂移和 diff 检查。
2. 独立审查测试是否真验证行为、是否能暴露错误，修复发现的问题并复验。
3. 映射 S01–S03、A01–A18、T01–T09：记录技术通过、未运行、业务待定；T08 引向 P22，不自动替人工签收。
4. 回交代码差异、实际命令/数量/退出码、环境、证据路径和接续步骤。用户已授权直接执行 P21，并在验收后于 2026-11-20 授权提交并推送当前 P21 分支；main 合并、上线及真实外部通知另计。

## 2026-11-20 继续收敛 Windows 错误

用户要求修复剩余错误，扩大本轮实现范围至 P12/P15/P23 的实际平台适配。目标是恢复原有安全和一致性语义并复跑全部测试，不以跳过/伪造平台功能收口。

1. P12：通过已安装 Git 的 Bash 显式执行 POSIX 入口，保持原守卫断言，移除对未安装 shasum 的不必要依赖（同算法 hashlib）。
2. P23：真正的跨进程文件锁，Windows 单文件原子激活指针并固定不可变 release；POSIX 保留原 symlink。覆盖进程互斥、超时、异常释放、篡改、回滚及审计。
3. P15：Windows 不依赖需特权的 symlink，使用 `.current.json` 单次原子提交和不可变 generation，读者通过显式 resolver 固定 generation；现有平面文件保留为 legacy，不能逐个覆盖冒充原子发布。POSIX 原实现保留。原 ImportOutcome 指向真实发布文件，配置/验证/检索入口接同一 resolver。
4. 报告权限：Windows 在 CREATE_NEW 时设置受保护的当前用户 DACL，原生验证读写访问；POSIX 保留 0600。失败不写报告、不得覆盖已有文件。
5. 依次运行各负责路径专项、独立反例/审查和扩大 PostgreSQL 回归；记录最终实际失败/跳过。更新 P21 交接为修复后的真实状态。

选择依据：实际 NTFS 探针证明 junction 可无特权创建/更新，但不能转换非空旧目录，也不能代替稳定文件 symlink。单指针/固定快照读者能保留崩溃一致性，避免改变系统开发者模式/权限或引入逐文件更新窗口。

## 最终执行证据（2026-11-20）

- 四类 Windows 适配已落地。独立复查补齐 P15 提交后中断、CAS、返回自身快照、数据库和显式 bundle 读者，并关闭 P23 路径重定向、未知 schema、非法复核范围及验证报告门禁缺口。
- 16:11:24–16:22:26 冻结产品代码上运行完整 PostgreSQL 核心回归：**1981 tests，0 failures/errors，6 skipped，退出 0**，648.054 秒。报告 `.local/p21/core-platform-fixed.{json,log}`；跳过原因逐条列在 P21 验收记录，两项 SQLite 专属检查已另用 SQLite 2/2 通过。
- 平台修复后 SQLite/PG 集成各 29/29、0 跳过；P12 53/53，P15 发布 51/51、时序守卫 3/3，数据库入口 3/3，bundle 36/36，P23 最后定向 106/106 及独立门禁负控制 8/8 均通过。私有报告原生 ACL 和受限 token 正反例通过。
- compileall、Django check、迁移漂移、JS/Bash 语法均通过，证据 `.local/p21/system-checks-platform-fixed.json`。历史失败报告保留，专业金标准／客户端／人工签收及 P22 恢复按各自范围接续。
