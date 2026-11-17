# 提交时授权默认专家 Implementation Plan

**Goal:** 修复 PR #22 新上传任务提交后专家 404；有效提交与任务专家授权同事务完成，并能修复既有 pending 任务。

**Architecture:** 保留 TaskAssignment 对象权限。先沿用任务中有效专家；没有时给配置的默认专家建立该任务的显式授权。默认账号不可用则拒绝提交且整体回滚，不扩大全体专家可见范围，不自动创建账号。幂等重放修复仍在当前版本 pending 的旧提交；提供单任务补授权命令。

**Tech Stack:** Django ORM/事务、现有 Ninja API、SQLite 回归；无新依赖或迁移。

## 实施与验证

1. 在 `tests/p09/test_default_expert.py` 构造没有预授权的新任务，先验证默认专家从 404 变为提交后可读、可审批；其它专家仍 404。覆盖已有专家、禁用/错误角色/缺默认、事务回滚、重复提交、旧 pending 修复与旧版不补权。
2. 在 `apps/reviews/services/reviewers.py` 集中处理有效专家与默认账号选择、唯一授权、审计。`submissions.py` 在新提交与有效 pending 重放时调用。
3. `config/settings.py` / `.env.example` 提供 `CUSTOMS_DEFAULT_EXPERT_USERNAME`，开发默认 `expert`，部署可指定已有账号；配置不允许前端覆盖。
4. `repair_pending_expert` 命令只处理显式任务的当前待审提交，默认预览，`--apply` 同事务补权。
5. 跑 P01/P06/P09/P10 受影响回归与系统/迁移/差异检查，更新交接。本机流程页去掉提交前手动指派要求，用真实上传/确认/提交→专家读取验证；对本机既有无专家 pending 任务执行修复，原审批内容不变。

本次不推 main、不合并 PR。修复在独立分支交付，界面验证不冒充 PostgreSQL 并发或专业归类验收。
