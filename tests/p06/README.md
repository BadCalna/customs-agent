# P06 验证入口

所有默认样本均为完全合成的 OOXML，不含业务文件/图片。真实样本仅通过参数/环境变量提供，禁止添加到 Git。

- `make test TASK=P06`：真实 HTTP 视图 + SQLite ORM + 文件系统 + inline mock；PostgreSQL 四项竞争测试在 SQLite 明确跳过。
- `.venv-customs/bin/python tests/p06/check_crash.py`：在自动清理的独立 SQLite 目录用 `os._exit(91)` 实测发布后中断、恢复与去重。
- `.venv-customs/bin/python tests/p06/manual_check.py`：生成 3 商品/3 装箱成员，图片 available/absent/read_failed 各一的合成模板，以及重排/缺页签/缺关键字段/归属歧义版本，放在忽略目录 `data/private/p06/`。
- `make migrate && make seed-demo && make dev BIND=127.0.0.1:8006`，打开 `/uploads/`；页面没有 fixture 模式。
- `BASE=http://127.0.0.1:8006 NODE_PATH=<含playwright的node_modules绝对路径> node tests/p06/browser_check.cjs`：真实浏览器与 HTTP 上传链路。用 `P06_SAMPLE=/absolute/template.xlsx` 切换真实模板；真实模板时默认跳过重排，可把 `P06_REORDER` 设为另一个经过业务允许的重排模板路径。输出截图默认在 `.local/`。
- `.venv-customs/bin/python tests/p06/verify_template.py /absolute/template.xlsx`：向当前开发库通过真实上传视图新增任务，逐行核对 P04 来源/成员/原文及 P05 图片状态/字节哈希，验证原件未改写。只在隔离开发库运行，不代表专业归类验收，也不等同浏览器验收。
- PostgreSQL：配置独立 `CUSTOMS_PG_*` 后 `CUSTOMS_DB_BACKEND=postgres .venv-customs/bin/python manage.py test tests/p06`，四项测试使用 Barrier + ThreadPoolExecutor + 独立连接。SQLite 顺序通过不替代这项验收。

实际执行记录及环境限制见 `docs/开发文档/交接/P06.md`。
