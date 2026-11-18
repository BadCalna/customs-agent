# 正式上线清单

本清单适用于现有 Linux 服务器上的 Docker Compose。`deploy/docker-compose.yml` 将 nginx 后端默认绑定在 `127.0.0.1:8080`，须由现有 TLS 反向代理提供公网入口。数据库和 Redis 不开放宿主机端口。

## 需要现场提供

- 公司批准用于真实单证的 AI 服务地址、模型名和接口协议。当前代码支持 OpenAI 兼容的 `/chat/completions`；若服务协议不同，需要先适配并完成验收。
- 服务器的 Linux 发行版、CPU 架构、Docker/Compose 版本、域名、证书和现有反向代理配置。
- 真实供应商、业务员、专家账号的开通与任务归属策略。不要使用 `seed_demo` 账号处理真实业务。
- 数据分级、模型服务的数据留存与传输边界、备份目的地和故障值班人。

密钥只写入服务器上的 `deploy/.env`，文件权限设为 `0600`，不得提交到 Git。`CUSTOMS_AI_API_BASE` 填 OpenAI 兼容服务的 API 根路径（例如以 `/v1` 结尾），`CUSTOMS_AI_MODEL` 填实际模型名，`CUSTOMS_AI_API_KEY` 填该环境专用密钥。HTTP 内网模型服务需显式设置 `CUSTOMS_AI_ALLOW_HTTP=1`。

## 部署顺序

1. 为数据库和受控文件建立可恢复备份，完成一次隔离恢复演练；确认当前代码与既有数据迁移兼容。
2. 配置 TLS 入口，将公网 HTTPS 请求转发到 `127.0.0.1:8080`，保留原始 `Host`，设置 `X-Forwarded-Proto: https`。不要开放 8080 到公网。
3. 根据 `deploy/.env.example` 写 `deploy/.env`。正式环境要求 `CUSTOMS_ENGINE_MODE=openai`、`CUSTOMS_PAGE_MODE=live`、`CUSTOMS_HTTPS=1`；Compose 固定启用 RQ 和生产启动检查。
4. 执行 `docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build`。确认 `migrate` 成功，`app`、`web`、`redis`、`worker`、`dispatcher` 健康。
5. 使用三个真实测试账号，在隔离业务数据上完成：上传新任务、后台导入、认领、AI 预审、逐行确认、整批提交、专家 AI 辅助核查、人工通过或退回、补料新版本、失败行重试、原件下载和历史回查。
6. 在目标服务器执行 `scripts/operations/drill.sh`，保留演练记录；再核对 TLS、CSRF、权限隔离、队列失败恢复、日志、磁盘水位和备份告警。

任何一步失败都不要切换真实业务流量。AI 建议当前不连接权威税则检索，所有依据状态保持待核；附图会展示给人工审核者，但当前模型调用只包含附图状态与位置，不传图片像素，不能视为 AI 图像核验。最终决定必须由有权限的业务员和专家作出。
