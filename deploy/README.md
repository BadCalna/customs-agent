# 云端多人测试环境部署（单台云服务器 · Docker Compose）

> **状态（2026-11-17）**：部署准备与备份恢复演练完成，**项目尚未上线**，也没有部署到任何真实服务器。
> 本目录给出的是一套“在一台云服务器上给少量人员试用”的方案，不是生产方案。
> 本机没有 Docker，**容器相关步骤没有在本机实跑过**；实际跑过的部分见
> [交接/P22.md](../docs/开发文档/交接/P22.md) 的“已验证/未验证”两栏。

---

## 0. 先说清楚：这套环境现在能做什么、不能做什么

| 能力 | 现状 | 说明 |
|---|---|---|
| Django + Ninja 应用 | 可用 | gunicorn 运行，不是 `runserver` |
| PostgreSQL 17 | 可用 | 容器内网，不对外开放端口 |
| 原件/图片/导出文件持久化 | 可用 | 命名卷 `files`，按版本内容寻址 |
| 数据库持久化 | 可用 | 命名卷 `pgdata` |
| 迁移、静态资源、健康检查 | 可用 | 一次性 `migrate` 服务 + `/api/health` |
| 后台任务（Redis + worker） | **不可用** | P13 未交付；Compose 里只是预留位（`profile=queue`） |
| 上传 / 认领 / 正式页面 | **未交付** | P06、P07/P08/P11 还在开发；当前没有上传入口 |
| 演示壳 `/demo/`、工作台 `/workbench/` | **按设计 404** | 两者只在 `DEBUG=1` 时可用，云端必须 `DEBUG=0` |
| AI 引擎 | mock | 真实引擎属 P16/P18 |

**所以“多人测试”现在能测到的是**：`/admin/` 后台、`/api/v1/` 接口、`/api/v1/docs` 接口文档，
以及 P12/P09/P10 已交付的提交/决定接口链路（用管理员在后台或脚本造数据）。

**如果测试人员必须看到工作台页面**，有三个选择，需要 Jay 先定（见第 12 节 D1）：
等 P07/P08/P11 交付正式页面 / 由主控加一个受控开关 / 短时间 `DEBUG=1`（有风险，不推荐）。

---

## 1. 部署前需要 Jay 提供的信息（不需要密码、私钥）

Agent 不接触、也不需要以下任何东西：服务器 root 密码、SSH 私钥、云控制台账号、数据库现有密码。
下面这些是**部署方案要落地时必须知道的配置事实**，可以只回答知道的，其余部署时现场确认：

1. **云厂商 + 实例规格**：CPU/内存/磁盘大小（建议 ≥2C4G、系统盘 ≥40G，另留数据盘）。
2. **CPU 架构**：x86_64（`amd64`）还是 arm64。镜像与 Postgres 镜像都支持两者，但构建机器要与服务器一致或走跨架构构建。
3. **操作系统与版本**：例如 Ubuntu 22.04 / 24.04、Debian 12、CentOS Stream 9、Alibaba Cloud Linux 3。
4. **是否允许安装 Docker**：有没有 sudo 权限；公司/云厂商是否禁止装容器运行时（个别托管环境会禁）。
5. **公网 IP 与访问方式**：服务器公网 IP；测试人员从公网访问还是只走内网/VPN。
6. **安全组/防火墙**：能否开放测试端口（方案默认 8080；也可以改成 80）。
7. **域名**（可选，没有也行）：是否有可解析到该服务器的域名；若在中国大陆，域名是否已备案（未备案的 80/443 会被拦）。
8. **是否需要 HTTPS**：只有“有域名 + 有证书”才做；否则先纯 HTTP 内测，并接受明文传输风险。
9. **数据盘挂载点**：是否已有独立数据盘（如 `/data`），备份打算放同机第二块盘还是对象存储。
10. **同时测试人数**：3～5 人还是 10+ 人（影响 gunicorn worker 数与内存）。
11. **测试数据性质**：只用合成/脱敏样例，还是有人会传真实客户单证（后者需要额外的合规确认，不建议）。
12. **谁负责日常运维**：谁看日志、谁做备份、出问题找谁。

另外两个“不是信息而是决定”的问题，见第 12 节。

---

## 2. 服务器准备（以 Ubuntu 22.04/24.04 为例）

以下命令都在服务器上以有 sudo 权限的账号执行。

```bash
# 2.1 确认系统与架构
lsb_release -a
uname -m            # x86_64 或 aarch64

# 2.2 安装 Docker Engine + Compose 插件（官方脚本，二选一）
#     方式 A：官方 apt 源（推荐，便于升级）
curl -fsSL https://get.docker.com | sudo sh
#     方式 B：按官方文档手动加 apt 源后 apt install docker-ce docker-compose-plugin

# 2.3 确认版本：Compose 需要 v2（本方案用到 service_completed_successfully，建议 v2.20+）
docker --version
docker compose version

# 2.4 让当前用户免 sudo 用 docker（重新登录后生效）
sudo usermod -aG docker "$USER"

# 2.5 磁盘检查：镜像 + 数据 + 备份都要位置
df -h /var/lib/docker
```

**端口与防火墙**（安全组里放行，主机防火墙同步）：

```bash
# 只放行测试端口（默认 8080）；不要把 5432 放出去——数据库本来就不发布端口
sudo ufw allow 8080/tcp
# 如果之后改用 80：sudo ufw allow 80/tcp
```

> 数据库端口 5432 **没有**在 compose 里发布，容器外连不上，这是有意的。

---

## 3. 拉代码与写配置

```bash
# 3.1 选一个部署目录（不要放在 /root 下，也不要放在会被 auto-sync 的目录里）
sudo mkdir -p /srv/customs-agent && sudo chown "$USER" /srv/customs-agent
cd /srv/customs-agent
git clone https://github.com/BadCalna/customs-agent.git .
git checkout main          # 用主分支；P22 的部署文件合并后即可用

# 3.2 生成配置文件
cp deploy/.env.example deploy/.env
```

编辑 `deploy/.env`，**只改这三项必填**（其余保持默认即可）：

```bash
# 生成两个随机值，把输出粘到 deploy/.env 对应行
openssl rand -base64 48 | tr -d '\n'    # → DJANGO_SECRET_KEY
openssl rand -base64 24 | tr -d '\n'    # → CUSTOMS_PG_PASSWORD
```

```ini
DJANGO_SECRET_KEY=<粘贴上面第一行输出>
CUSTOMS_PG_PASSWORD=<粘贴上面第二行输出>
CUSTOMS_ALLOWED_HOSTS=203.0.113.10     # 改成服务器公网 IP；有域名时写成 203.0.113.10,test.example.com
```

确认 `deploy/.env` 不会被提交：

```bash
git check-ignore -v deploy/.env     # 应输出 .gitignore:6:.env  deploy/.env
```

---

## 4. 首次启动

```bash
cd /srv/customs-agent

# 4.1 先做一次配置自检（不启动容器，只解析 compose + 校验变量）
docker compose -f deploy/docker-compose.yml --env-file deploy/.env config >/dev/null && echo "compose 配置 OK"

# 4.2 构建并启动（首次会拉镜像 + 装依赖，5～15 分钟）
docker compose -f deploy/docker-compose.yml up -d --build

# 4.3 看启动顺序是否按预期：db 健康 → migrate 跑完 → app 健康 → web 起来
docker compose -f deploy/docker-compose.yml ps
```

启动后 `ps` 里应看到 `db`(healthy)、`app`(healthy)、`web`(healthy)，`migrate` 状态是 `exited (0)`——
一次性服务跑完就退出是正常的。

```bash
# 4.4 迁移是否真的执行过
docker compose -f deploy/docker-compose.yml logs migrate | tail -20
```

---

## 5. 启动后检查（逐条做，别跳）

```bash
BASE=http://127.0.0.1:8080        # 服务器上用内网地址测；CUSTOMS_HTTP_PORT 改过就跟着改

# 5.1 健康检查
curl -sS "$BASE/api/health"                       # 期望 {"status":"ok"}
# 5.2 接口文档（Ninja 自带）
curl -sS -o /dev/null -w '%{http_code}\n' "$BASE/api/v1/docs"    # 期望 200
# 5.3 管理后台登录页
curl -sS -o /dev/null -w '%{http_code}\n' "$BASE/admin/login/"   # 期望 200
# 5.4 静态资源（nginx 提供，验证 collectstatic 生效）
curl -sS -o /dev/null -w '%{http_code}\n' "$BASE/static/admin/css/base.css"   # 期望 200
# 5.5 确认 DEBUG 确实是关的：这两个入口应返回 404
curl -sS -o /dev/null -w '%{http_code}\n' "$BASE/demo/"           # 期望 404
curl -sS -o /dev/null -w '%{http_code}\n' "$BASE/workbench/"      # 期望 404
# 5.6 从你自己电脑（公网）验证一次
#     浏览器打开 http://<服务器公网IP>:8080/admin/login/
```

如果 5.4 是 404：说明 collectstatic 没跑或 static 卷没挂上，看 `logs migrate` 与 `logs web`。

---

## 6. 账号与密码要求（重要）

**不要**在云端直接跑 `make seed-demo` 然后把仓库里公开记录的演示密码发给测试人员。
那套密码写在仓库文档里，等于公开口令。云端按下面做：

```bash
# 6.1 建管理员（交互式，密码自己输入，不要写进任何文件）
docker compose -f deploy/docker-compose.yml run --rm app python manage.py createsuperuser

# 6.2 需要演示任务结构时再建演示数据（可选）
docker compose -f deploy/docker-compose.yml run --rm app python manage.py seed_demo

# 6.3 立即改掉演示账号的密码（每个账号一次），用强口令
docker compose -f deploy/docker-compose.yml run --rm app python manage.py changepassword supplier_a
docker compose -f deploy/docker-compose.yml run --rm app python manage.py changepassword operator_a
docker compose -f deploy/docker-compose.yml run --rm app python manage.py changepassword expert
```

给测试人员的规矩（建议直接写进通知）：

- 每人一个账号，不要共用；不要用公司其他系统的密码。
- 上传的任何文件都视为“测试数据”，不要传真实客户单证。
- 测试环境随时可能重建、数据可能被清空，不要把它当办公系统用。
- 出了问题把时间和页面截图发给 Jay，不要自己重启容器。

---

## 7. 域名与 HTTPS（可选，没有域名就跳过）

纯 IP + HTTP 也能测。要上 HTTPS 时：

1. 准备证书文件（不要把证书提交进仓库；放到 `deploy/certs/`，该目录已在 `.gitignore`/`.dockerignore` 里排除）。
2. `cp deploy/nginx/https.conf.example deploy/nginx/default.conf`，改里面的 `server_name`。
3. compose 的 `web` 增加 443 映射，并把证书挂进容器：
   ```yaml
   ports: ["${CUSTOMS_HTTPS_PORT:-8443}:443"]
   volumes:
     - ./certs:/etc/nginx/certs:ro
   ```
4. `deploy/.env` 里设置：
   ```ini
   CUSTOMS_HTTPS=1
   CUSTOMS_CSRF_TRUSTED_ORIGINS=https://test.example.com
   CUSTOMS_ALLOWED_HOSTS=test.example.com
   ```
5. `docker compose -f deploy/docker-compose.yml up -d --force-recreate web` 后重新跑第 5 节。

**CSRF 常见坑**：`CUSTOMS_CSRF_TRUSTED_ORIGINS` 必须与浏览器地址栏的协议 + 域名完全一致
（`https://test.example.com`，不要带路径、不要漏协议）。不一致的表现是：登录成功，但一提交就 403。

---

## 8. 日常操作

```bash
DC="docker compose -f deploy/docker-compose.yml"
cd /srv/customs-agent

$DC ps                      # 状态
$DC logs -f app             # 应用日志（gunicorn access/error）
$DC logs -f web             # nginx 日志
$DC stop                    # 停（保留容器与数据）
$DC start                   # 再启
$DC down                    # 删容器与网络，**保留命名卷里的数据**
$DC down -v                 # 连卷一起删 —— 会丢数据，只在确认要清空环境时用
```

### 8.1 升级（按顺序做，别跳）

```bash
cd /srv/customs-agent

# 1) 先备份（见第 9 节；升级前必须有一份可恢复的备份）
scripts/operations/backup.sh --label before-upgrade

# 2) 取新代码
git fetch origin && git checkout main && git pull --ff-only

# 3) 重新构建并滚动重启（migrate 会自动先跑，app 等它成功后启动）
docker compose -f deploy/docker-compose.yml up -d --build

# 4) 按第 5 节复检；再确认本次升级是否新增迁移
docker compose -f deploy/docker-compose.yml logs migrate | tail -20
```

### 8.2 回退（代码回退 + 数据回退，两件事分开）

**代码回退**（新版本有 bug，但数据没问题）：

```bash
cd /srv/customs-agent
git checkout <上一个已知good的提交或标签>
docker compose -f deploy/docker-compose.yml up -d --build
```

> 数据库里已经执行过的新迁移**不会**自动撤销。Django 容忍“库里有代码里没有的迁移记录”，
> 但**不要**用 `migrate --fake` 或降级迁移去折腾业务库；真要退回旧结构，走下面的数据回退。

**数据回退**（升级后数据被写坏，要回到升级前）：不要原地覆盖业务库，用“换指向”的方式：

```bash
# 1) 停写
docker compose -f deploy/docker-compose.yml stop app web

# 2) 把升级前的备份恢复到一个新库 + 新目录（脚本拒绝覆盖已存在的目标）
scripts/operations/restore.sh deploy/backups/<升级前的时间戳>-before-upgrade
#    输出会给出：恢复的库名（如 customs_agent_restore_20261117xxxxxx）与目录（/srv/customs/restore-...）

# 3) 校验恢复结果
scripts/operations/verify_restore.sh deploy/backups/<那个备份> \
  --target-db <上一步的库名> --target-files <上一步的目录>

# 4) 把应用指向恢复出来的库与文件目录（deploy/.env）
#    CUSTOMS_PG_NAME=customs_agent_restore_20261117xxxxxx
#    CUSTOMS_FILE_ROOT=/srv/customs/restore-20261117xxxxxx
#    改完重建 app（不要动 db 容器）
docker compose -f deploy/docker-compose.yml up -d app web
```

原来的库与原件目录**没有被删除**，排查完再决定怎么清理。这是本方案唯一的回退路径，
因为它不会制造“覆盖到一半”的中间状态。

---

## 9. 备份、恢复与演练

### 9.1 备份什么

- **数据库**：`pg_dump -Fc`（自定义格式，用 `pg_restore` 还原）。
- **受控文件**：`/srv/customs` 卷里的 `files/`（原件、提取出的图片、导出副本），排除 `incoming/` 暂存区。
- **元信息**：表行数 + 每个文件的 sha256 清单（`counts.json`）、备份清单（`manifest.sha256`）、`meta.env`。

```bash
scripts/operations/backup.sh              # 默认先短暂停写再备份（最安全）
scripts/operations/backup.sh --no-quiesce # 不停写（演示库可用；数据库与文件不再同一时点）
scripts/operations/backup.sh --label before-upgrade
```

备份落在 `deploy/backups/<UTC时间戳>-<标签>/`（该目录已被 Git 忽略）。

### 9.2 一致性边界（必须理解）

| 做法 | 数据库与文件是否同一时点 | 服务影响 | 适用 |
|---|---|---|---|
| `backup.sh`（默认停写） | **是**：先停 `app`/`web`，再依次 dump 库、打包文件 | 停写期间不可访问（秒级～几分钟，取决于文件体积） | 正式备份、升级前 |
| `backup.sh --no-quiesce` | **否**：两个相邻时点，可能出现“库里引用了还没落盘的文件” | 无 | 演示库、看一眼数量 |
| 只备份数据库 | 只有库 | 无 | 不推荐：文件丢了就再也对不上 |

另外两条边界：

- 备份期间**不删除**任何现有库或文件；脚本里的 `pg_dump`/`tar` 只读。
- 容器内 `incoming/` 是“上传已落暂存、事务未提交”的半成品，**不进备份**；
  恢复后这些半成品会消失，属于预期行为（它们本来就不该被当成业务数据）。

### 9.3 演练（证明备份真的能恢复）

```bash
# 备份 → 恢复到新库 + 新目录 → 逐项比对 → 清理隔离目标
scripts/operations/drill.sh

# 想保留恢复结果手工检查
scripts/operations/drill.sh --keep
```

判定通过的标志：`演练结果：通过`，且比对输出里“表行数与文件哈希均与备份相符”。
判定器会真的失败：改一行行数或改一个文件哈希，`compare_counts.py` 就返回非 0 并列出差异。

### 9.4 恢复到独立新环境（换机器 / 换服务器）

```bash
# 新服务器上：装 Docker、克隆代码、写 deploy/.env（同第 2、3 节）
scripts/operations/backup.sh ...            # 老环境先备份
# 把整个备份目录拷到新服务器（scp/对象存储都可以），然后：
scripts/operations/restore.sh /path/to/backup-dir
scripts/operations/verify_restore.sh /path/to/backup-dir --target-db ... --target-files ...
# 改 deploy/.env 指向恢复出来的库/目录，up -d，再跑第 5 节 + 抽查一条业务数据：
#   任务列表 → V1/V2 → 商品行 → 附图能打开 → 提交快照 → 专家决定 → 历史日志
```

---

## 10. “一台服务器的少量测试” vs “后续生产部署”

当前方案**只解决**：一台机器、几到十几个人、可随时停机维护、数据丢了可以重建的试用场景。

上生产之前必须补的东西（本方案**没有**做，不要以为有了 Docker 就等于生产可用）：

| 主题 | 现在 | 生产需要补 |
|---|---|---|
| 数据库账号 | 官方镜像的 `POSTGRES_USER` 即超级用户 | 业务角色（最小权限，无 SUPERUSER/CREATEDB）+ 独立备份/运维角色；口令轮换 |
| 数据库暴露面 | 只在 compose 内网 | 托管数据库或独立实例、连接池、慢查询与容量监控 |
| TLS | 可选、手工证书 | 自动续期（certbot/ACME）、HSTS 评估、只允许 TLS1.2+ |
| 反向代理 | nginx 单实例同机 | 独立入口、限流、请求体上限、超时与重试策略、真实 IP 传递 |
| 应用运行 | gunicorn 3 worker，单机 | 多实例/滚动发布、健康检查接入负载均衡、优雅重启 |
| 后台任务 | 无（P13 未交付） | Redis 持久化与容量、worker 并发/超时/重试、死信与告警、待派发重扫 |
| 备份 | 手工脚本，同机目录 | 异地/对象存储、加密、保留策略、定期自动演练、RPO/RTO 指标 |
| 日志 | `docker compose logs` | 集中日志、保留期、审计（谁的什么操作）、告警 |
| 监控 | `/api/health` 手工 curl | 存活/就绪探针、指标、告警通道、磁盘水位 |
| 密钥管理 | `deploy/.env` 文件 | 密钥托管（KMS/Secret Manager）、最小授权、审计 |
| 迁移策略 | 启动时自动跑 | 发布流水线里受控执行、可回滚的数据迁移脚本、演练过的降级方案 |
| 权限模型 | 三角色 + 显式任务授权 | 与真实组织/供应商归属对齐（待 Jay 确认业务口径） |
| 合规 | 无 | 真实单证的数据分级、留存与删除、访问审计、跨境传输限制 |

另外，`DEBUG=0` 下演示壳与工作台返回 404 这件事，在生产形态里也必须有答案（正式页面或明确关闭入口），
不能靠“临时开 DEBUG”糊过去。

---

## 11. 故障排查（按症状查）

| 症状 | 先看 | 常见原因 |
|---|---|---|
| `up` 直接报 `CUSTOMS_PG_PASSWORD is missing` | `deploy/.env` | 没填必填项，或 `.env` 放错目录（必须在 `deploy/` 下） |
| 容器起不来，日志里有 `CUSTOMS_ALLOWED_HOSTS` | `logs app` | 没填公网 IP/域名；填了但没重跑 `up -d` |
| 浏览器 400 `Bad Request (400)` | `deploy/.env` | 访问用的 Host 不在 `CUSTOMS_ALLOWED_HOSTS` 里 |
| 登录成功但马上变匿名 / 提交 403 | `deploy/.env` | `CUSTOMS_HTTPS=1` 但实际走 HTTP（Secure cookie 不发送）；或 CSRF 来源不匹配 |
| `/static/...` 404 | `logs migrate`、`logs web` | 没跑 collectstatic / static 卷没挂 |
| `/demo/`、`/workbench/` 404 | —— | **预期行为**：`DEBUG=0` 时演示壳按设计关闭 |
| `db` 一直 `unhealthy` | `logs db` | 数据卷权限、磁盘满、`pg_isready` 用户名/库名不一致 |
| `migrate` 退出码非 0 | `logs migrate` | 迁移冲突、数据库连不上、旧库结构不一致（先备份再处理） |
| 磁盘满 | `df -h`、`docker system df` | 旧镜像堆积：`docker image prune`（确认无回退需求） |

---

## 12. 需要 Jay 决定的 4 件事

- **D1：测试人员要不要看界面？**
  现在只有 `/admin/` 和 API 文档。要页面就得等 P07/P08/P11，或接受“短时间 DEBUG=1”的风险。
  **建议**：先用 `admin + API 文档` 做内部技术验收，页面到位后再开给人看。
- **D2：用 8080 还是 80？** 安全组、防火墙、是否备案都跟这个有关。
- **D3：备份放哪？** 同机第二块盘的目录（简单，但整机故障会一起丢），还是对象存储（多一步配置，更安全）。
- **D4：谁来日常看？** 备份要有人定期跑并确认失败告警，否则等于没有备份。

---

## 13. 与 P06 的关系（冲突面说明）

P22 只**新增**文件，不改 P06 正在改的上传/认领/页面/业务逻辑，也不改公共配置：

- 新增：`deploy/`（Dockerfile、compose、设置模块、nginx、env 模板）、`scripts/operations/`、
  `tests/p22/`、仓库根 `.dockerignore`。
- 未改：`config/settings.py`、`apps/**`、`web/**`、`Makefile`、`.env.example`、`uv.lock`。
  部署专用开关放在 `deploy/settings_cloud.py`（`from config.settings import *` 后覆盖），
  通过 `DJANGO_SETTINGS_MODULE=deploy.settings_cloud` 生效，开发默认行为完全不变。

潜在冲突只有一处：**如果 P06 之后要在 `config/settings.py` 里加 `DEBUG`/`ALLOWED_HOSTS`/
`CSRF_TRUSTED_ORIGINS` 的环境变量支持**，两套逻辑会重叠。届时应由主控决定把部署开关
收进 `config/settings.py`，并把 `deploy/settings_cloud.py` 缩减为只保留 `STATIC_ROOT` 等壳配置。
在此之前，云端用哪个设置模块由 `DJANGO_SETTINGS_MODULE` 决定，互不干扰。

同理，`.dockerignore` 是仓库根的新文件，若后续有其他任务也要加，需要合并成一份而不是各写一份。

---

## 14. 文件索引

| 路径 | 作用 |
|---|---|
| `deploy/Dockerfile` | 应用镜像（python:3.13-slim + uv 锁依赖 + gunicorn + 非 root） |
| `deploy/docker-compose.yml` | db / migrate / app / web；Redis+worker 为 P13 预留（`profile=queue`） |
| `deploy/settings_cloud.py` | 云端设置：DEBUG 默认关、ALLOWED_HOSTS 必填、CSRF/HTTPS/cookie/STATIC_ROOT |
| `deploy/.env.example` | 无真实密钥的配置模板（敏感项留空，逼你自己生成） |
| `deploy/nginx/default.conf` | 反代 + `/static/` + 110MB 上传上限 |
| `deploy/nginx/https.conf.example` | 有域名后再用的 HTTPS 版本（不生效，避免缺证书起不来） |
| `scripts/operations/backup.sh` | 备份（可停写）：库 + 文件 + 行数/哈希清单 + manifest |
| `scripts/operations/restore.sh` | 恢复到**隔离**的新库/新目录，拒绝覆盖 |
| `scripts/operations/verify_restore.sh` | 恢复后逐项比对 |
| `scripts/operations/drill.sh` | 备份→恢复→比对→清理的一键演练 |
| `scripts/operations/table_counts.py` | 表行数 + 文件哈希清单（备份/恢复共用） |
| `scripts/operations/compare_counts.py` | 一致性判定器（一致=0，不一致=1） |
| `tests/p22/` | 部署资产与设置模块的检查（`make test TASK=P22`） |
