#!/usr/bin/env python
"""历史数据导入 P15 的命令行入口。

支持两种输入，最终都写成 P15 ``p15.history.1`` JSON：

1. 旧多页签业务模板（``--input-format legacy-template``）
2. 历史汇总表（``--input-format consolidated``，如《历史成功数据.xlsx》）
   ``--input-format auto`` 只在结构明确时自动判断；歧义时拒绝并提示显式选择。

示例（真实汇总表，需 Jay 明确人工声明）：

    .venv-customs/bin/python scripts/import_history_templates.py import \
      --files "/path/to/历史成功数据.xlsx" \
      --input-format consolidated \
      --dataset-kind real --declared-success --declared-by Jay \
      --out-dir data/private/p15-import/real-consolidated-2026-11-18

示例（旧多页签模板）：

    .venv-customs/bin/python scripts/import_history_templates.py import \
      --files data/import-basis/2026-11-17/historical-success/*.xlsx \
      --manifest data/import-basis/2026-11-17/manifest.json \
      --input-format legacy-template \
      --dataset-kind real --declared-success --declared-by Jay

输出默认写到 Git 忽略的 ``data/private/p15-import/<storage_key>/``。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.sources.history_import import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())