# 如何独立重跑

环境：macOS Chrome stable（本次 157.0.8103.80，headless），Python 3.14，Playwright 1.65.2。浏览器环境与 Django 锁定依赖的解释器分开。实际入口为 `/`，不是 `/workbench/` 或 fixture 模式。每轮新建随机回环端口、SQLite、临时 Django 密钥和演示账户密码；不读取用户业务库，不访问外部 AI，也不覆盖既有证据目录。

```sh
# 在仓库根目录运行；如果已有项目虚拟环境，设置 APP_PY 指向其 python。
uv sync --locked
APP_PY="$PWD/.venv/bin/python"
RUN=$(mktemp -d)
uv venv "$RUN/browser" --python 3.14
uv pip install --python "$RUN/browser/bin/python" playwright==1.65.2
HELPER="$HOME/.agents/skills/webapp-testing/scripts/with_server.py"
"$RUN/browser/bin/python" tests/optimization/audit_run.py baseline "$RUN/before" --django-python "$APP_PY" --helper "$HELPER"
"$RUN/browser/bin/python" tests/optimization/audit_run.py candidate "$RUN/after" --django-python "$APP_PY" --helper "$HELPER"
"$RUN/browser/bin/python" tests/optimization/audit_score.py "$RUN/before"
"$RUN/browser/bin/python" tests/optimization/audit_score.py "$RUN/after"
```

需要已安装 Chrome；也可 `"$RUN/browser/bin/python" -m playwright install chromium` 并将 `--channel chromium` 加到两次运行命令。`audit_run.py` 使用 `git archive f66c8fa`，不 checkout 或改写原仓库。候选版本仅覆盖当前运行源码（web、apps、adapters、config）；两侧均 `migrate`、`seed_demo`、`seed_contracts`，不搬运 `.env` 或 `.local`。记录 runtime 源树 SHA256。每轮合成 UUID 与日期不同，截图字节不要求跨轮相同，但同份报告引用的图片应与该轮 JSON 哈希一致。

每个设备独立浏览器 context，1440×900 桌面或 390×844 触摸移动模拟，均 1× 像素，中文、上海时区、减少动态效果。实拍为浏览器视口，不是生成图；数据画面先滚动到当前版本概览（旧版没有概览，则滚动到运行进度），商品画面滚动到打开的商品详情。主题组只在候选实际提供控件时采集。HTTP 注意：初次未登录/退出后的 me 401、错密 401、seed_contracts 没有原始导入作业导致 import 404，属于预期。既不能把这些响应算作服务宕机，也不能把模拟分类结论算作业务正确。

`measurements.json` 同时保留 API 回执字段和 DOM 展示、主题色值、控件操作、页面级几何、JS 错误以及截图哈希；`source.json` 为源树和基线信息。两类测量截图为 Chromium，不覆盖真实 iOS Safari、辅助技术人工审查、任务完成时间、负载、线上服务或生产模型准确率。
