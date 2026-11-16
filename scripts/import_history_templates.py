#!/usr/bin/env python
"""历史模板导入 P15 的命令行入口。

示例（真实模板，需 Jay 明确人工声明）：

    .venv-customs/bin/python scripts/import_history_templates.py import \
      --files data/import-basis/2026-11-17/historical-success/*.xlsx \
      --manifest data/import-basis/2026-11-17/manifest.json \
      --dataset-kind real --declared-success --declared-by Jay

输出默认写到 Git 忽略的 ``data/private/p15-import/<batch_id>/``。
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