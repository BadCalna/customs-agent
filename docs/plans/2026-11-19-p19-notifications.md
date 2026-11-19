# P19 Notifications Implementation Plan

**Goal:** 实现本人站内消息、固定版本定位、业务节点提醒及可恢复的 mock DWS 投递。

**Architecture:** 业务事务内登记消息与独立投递 outbox；数据库唯一约束去重，提交后尝试投递，扫描命令恢复中断与有限重试。复用三角色、任务授权、session/CSRF 和现有工作台。用户明确本批不接真实通道，mock 不产生网络请求，也不显示为真实送达。

**Tech Stack:** Django 5.2 / Ninja / PostgreSQL 与 SQLite / 原生 ES modules。

## 基线与设计取舍

- `origin/main@ec504b5e14c8b9cbf9680b3a4fdc6c1830323586`，独立 `feature/P19-notifications` worktree。
- 选用持久 outbox；纯内存提交回调存在提交后进程退出丢提醒问题，同步外部发送会拖住业务请求。
- 提交提醒精确的已授权专家；通过提醒提交业务员和供应商；退回按 `return_to` 定位业务员/供应商；业务员退回提醒供应商。供应商仅站内，业务员/专家额外登记 mock DWS。
- 不按全局角色广播，不向尚未认领任务猜测业务员。通知与版本/提交/决定绑定，旧消息保持历史目标。
- 外发失败不回滚业务，最多三次尝试，租约防并发发布；真实通道幂等能力未确认，不宣称真实 exactly-once。

## 实施步骤

1. 新建 `tests/p19/`，以业务事件幂等、事务回滚、收件人隔离、已读CSRF、分页与错误游标为主要行为用例。
2. 扩展 `apps/notifications/models.py`，追加迁移：消息目标引用、事件唯一键、Delivery/DeliveryAttempt。旧消息保持可读。
3. 实现 `apps/notifications/services.py` / `api.py` / `schemas.py`；主控更新 `config/urls.py`、占位接口与契约导出，新增兼容消息DTO。
4. 实现 `adapters/notifications/dws.py` mock边界和 `dispatch_notifications` 扫描命令，配置仅支持 mock/disabled；验证失败上限、过期租约和重复扫描。
5. 主控接线 `submissions.py`、`decisions.py`、`returns.py`；通知写入同一业务事务，投递仅提交后发生。
6. 页面子任务实现 `message-list.js`、`messages.css`、client方法及两个正式工作台挂载；验证会话切换晚响应、分页、错误与固定版本导航。
7. 执行 P19 专项、P01/P02/P06/P09/P10/P13/P20 回归、Node行为检查、Django check、迁移一致性、契约生成及 diff check；记录平台跳过项。
8. 独立审查权限、去重、版本定位与重试边界，修复后复验；补齐 P19 交接与可操作手测步骤。

## 需要用户提供

本批无须提供材料。真实 DWS 接入留待后续提供接口文档、授权测试通道及账号人员映射；本批不发送真实消息。
