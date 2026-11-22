# 系统管理（01）运行说明

## 初始化

1. 在目标环境的 PostgreSQL 备份后执行 `python manage.py migrate`，并按原部署流程执行 `python manage.py collectstatic --noinput`。迁移创建权限目录和初始系统组，不创建账户，也不会给已有用户授权。
2. 由有权限的应急维护员在原生 `/admin/` 给指定运维账号授予 `ops_system_admin`，至少保留两名已验证可登录的管理员。`is_superuser`、`is_staff`、业务角色均不等于系统管理权限。将不得由日常运维修改的部署账号列入 `CUSTOMS_OPS_RESERVED_USERS`（逗号分隔）。
3. 日常入口是 `/ops/admin/`。未配置可信邮件时使用一次性线下交付链接，请通过受管渠道移交。链接片段不会随 HTTP 请求发送，收件页读取后立即清除。配置 `CUSTOMS_OPS_TRUSTED_EMAIL=1` 时，须设置 `CUSTOMS_OPS_SECRET_PUBLIC_URL`（固定可信 HTTPS 域名，路径 `/ops/admin/secret/`）以及 `CUSTOMS_OPS_SMTP_HOST`、`CUSTOMS_OPS_SMTP_FROM`；按邮件政策设置对应的 `CUSTOMS_OPS_SMTP_PORT`、`CUSTOMS_OPS_SMTP_USER`、`CUSTOMS_OPS_SMTP_PASSWORD`。上线前用真实允许地址验证投递和 TLS，不要记录链接或票据。
4. 将 `python manage.py maintain_ops_admin` 接入定时运维任务，用于清理过期票据与限流窗口、回收导出缓存和过期的幂等回执。它不删除审计事件。

## 联装系统调试（02）与数据中心（03）

这是代码部署集成，不允许在 API/数据库中配置 Python import 路径。将交付模块添加到部署的 `INSTALLED_APPS` 后，在部署设置中配置 `OPS_ADMIN_MODULE_INSTALLERS`（受信任 `install()` 的 dotted path 列表）。清单字段：`module_key`、`label`、`nav_path`、`urlconf`（相对于系统分配的 `/ops/debug/` 或 `/ops/data/` 前缀）、`permissions`、`event_types`；可选 `install_version`、`url_name`、`nav_permissions`、`api_router`（相对 `/api/v1/ops/debug/` 或 `/api/v1/ops/data/`）。模块代码仍需在自身端点校验身份与业务权限。

执行迁移同步元数据后，模块默认停用。按各自发布方案完成集成验证，再用有权限的账号在「模块入口」启用；停用立即隐藏导航并关闭公共入口，不删权限和业务数据。事件通过 `customs.ops.events` logger 的 `extra={'ops_event': envelope}` 上报，无需 import 本应用。源模块必须声明其事件类型并保持 `event_id` 稳定，02 的采集失败会抛回生产者供 outbox 重试。

## 验证与回滚

- 开发回归：`python manage.py test tests.ops_admin --noinput`；要跑并发保护用例，使用隔离 PostgreSQL 而非 SQLite。完整回归：`python manage.py test tests --noinput`。
- 发布门禁：分别与真实 02/03 版本验证启停、权限、稳定深链及事件入库；用目标环境可信邮件和隔离 PostgreSQL 验证设密、并发和上一版本回滚。未联装或使用 fixture 的结果不作为上线验收。
- 代码回滚：先停止 01 入口流量，再回退应用版本；保留新增表和审计，不直接逆迁移。模块可先停用或从部署配置撤下；不要删除权限记录。账号和组异常按审计发起新的补偿操作，不做静默反向 SQL。
