# 01/02/03 运维模块注册与本地演示

2026-11-23。本文件说明 01 系统管理、02 Agent 调试台、03 基础数据中心如何注册进同一套应用，以及本地演示/录屏的操作步骤和边界。模块自身的业务说明见 `apps/agent_debug/install.md`、`install-manifest-03.md` 与各自目录下的 README。

## 1. 注册关系

模块入口不由数据库决定，而由代码声明：

- `config/settings.py` 的 `INSTALLED_APPS` 依次追加 `apps.reference_browser`、`apps.agent_debug`、`apps.ops_admin`。01 最后装载，才能读到前两者的清单。
- `OPS_ADMIN_MODULE_INSTALLERS` 指向 `apps.reference_browser.install_manifest.installer` 与 `apps.agent_debug.install_manifest.installer`。清单是纯字典，不含模型导入。
- 01 的 `apps/ops_admin/catalog.py` 在启动时校验清单：权限码必须与 `CONTRACTS` 完全一致，`nav_path` 必须落在 `/ops/<area>/`，模块 `urls.py` 必须以相对路径解析出入口，事件名必须能归属到唯一模块。任一条不满足即 `ImproperlyConfigured`，不降级启动。
- 装载结果生成 `/ops/data/`、`/ops/debug/` 页面路由（`web_patterns`）与 `/api/v1/ops/data/`、`/api/v1/ops/debug/` API 路由（`add_api_routers`）。
- 注册 ≠ 启用。`ModuleRegistration` 由迁移登记为 `disabled`，此时 `OpsMiddleware.module_gate` 让页面与 API 一律 404，导航也不显示。启用必须由持 `ops_admin.manage_module_registry` 的账号在 01 的模块登记里改成 `active`，并留下 `ops.module.changed` 审计。

02/03 的 `urls.py` 都只用相对路径；隔离测试环境自行补回前缀（见 `optional_tests/ops_debug/urls.py`）。

## 2. 本地演示一键脚本

```sh
make setup          # 首次：依赖（含 cryptography、httpx）与本地密钥
make ops-demo       # 迁移 + seed_demo + seed_ops_demo + 启动服务
```

`scripts/operations/ops_demo.sh` 会：

1. 在仓库外生成演示密钥：`$HOME/.local/state/customs-agent/ops-demo/ops-debug-keys.env`（`0600`），以及密文目录 `$HOME/.local/state/customs-agent/ops-debug-secrets`（`0700`）。可用 `CUSTOMS_OPS_DEMO_STATE`、`CUSTOMS_OPS_DEBUG_SECRET_STORE` 改位置；**不要**放进仓库或同步盘。
2. 导出 `CUSTOMS_REFERENCE_FIXTURE=1`、`CUSTOMS_OPS_DEBUG_ENABLED=1`，并注入 `OPS_DEBUG_DATA_KEY` / `OPS_DEBUG_MASTER_KEY`。
3. 执行 `migrate`、`seed_demo`、`seed_ops_demo`，最后 `runserver`。

三个入口（登录后导航里可直接点）：

| 入口 | 演示什么 |
| --- | --- |
| `http://127.0.0.1:8000/ops/admin/` | 运行概览、账号与组、审计事件、异常日志、模块登记 |
| `http://127.0.0.1:8000/ops/data/` | 只读基础数据：快照选择、税则注释、HS 申报要素、历史参考 |
| `http://127.0.0.1:8000/ops/debug/` | 样例选择 → 发送前预览 → 确认运行 → 执行时间线（mock） |

演示账号 `ops_demo`，口令与 `make seed-demo` 的本地演示口令相同（`seed_ops_demo` 直接复用）。`seed_ops_demo` 授权 `ops_system_admin` 组加四个 `agent_debug.*`、三个 `reference_browser.*`，并把 02/03 从 `disabled` 改成 `active`，同时写一条 `ops.module.changed` 系统审计。重复执行不会重置口令、不会重复授权、不会重复审计。

## 3. 演示边界

- 只使用打包的合成样例与 mock 预览：`OPS_DEBUG_LIVE_ENABLED` 固定为 `False`，`OPS_DEBUG_ENDPOINTS` 为空，不发任何外网请求，不写真实供应商凭据。
- `/ops/data/` 的样例来自 `apps/reference_browser/fixtures/`，页面自带“合成样例 · 非官方，未经业务复核”标识。`REFERENCE_BROWSER_FIXTURE` 只在 `DEBUG` 下装载数据，`manage.py check --deploy` 会因该开关报 `reference_browser.E001`。
- 调试台的预览、运行输入用 `OPS_DEBUG_DATA_KEY` 加密入库；重启后沿用同一份演示密钥即可读回，删掉密钥文件则旧预览无法解密，属预期。
- 09:00 演示前建议先跑一次 `make ops-demo` 确认三条入口都能打开，再开始录屏；录屏期间不要清空 `.local/db.sqlite3`。

## 4. 生产接入差异

- 生产 `DEBUG=False`：关掉 `CUSTOMS_REFERENCE_FIXTURE` 与 `CUSTOMS_OPS_DEBUG_ENABLED`，改用真实发布目录（`OPS_DEBUG_PUBLISHED_DIR`、`OPS_DEBUG_EVIDENCE_MAP_ROOT`）与 C 方 `OPS_DEBUG_SNAPSHOT_ACCESS` checker。
- `OPS_DEBUG_SECRET_STORE` 由部署注入绝对路径（仓库与同步盘之外），密钥由服务密钥管理下发；不要沿用演示密钥。
- 模块启用走 01 页面（需 `ops_admin.manage_module_registry`），不要在生产执行 `seed_ops_demo`。

## 5. 验证

```sh
make check
make test TASK=ops_registration     # 清单装载、URL/API 挂载、门禁 404→200、导航过滤、演示账号
make test TASK=ops_admin            # 01 自身回归
python -m pytest -c optional_tests/ops_debug/pytest.ini optional_tests/ops_debug -q
python -m pytest -c optional_tests/ops_data/pytest.ini optional_tests/ops_data -q
make contracts                      # 新增两个 router 后会更新 contracts/openapi/v1.json
```

`tests/ops_registration/test_registration.py` 直接使用真实清单（不打桩），因此任何一次清单写错都会在测试阶段暴露，而不是在演示现场 404。