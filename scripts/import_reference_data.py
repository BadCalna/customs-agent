"""官方参考数据导入流水的命令行入口（P23）。

用法（仓库根目录）::

    .venv-customs/bin/python scripts/import_reference_data.py scan \
        --sources data/import-basis/2026-11-17/reference-sources.json

    .venv-customs/bin/python scripts/import_reference_data.py import \
        --sources data/import-basis/2026-11-17/reference-sources.json

    .venv-customs/bin/python scripts/import_reference_data.py publish \
        --snapshot .local/reference-import/candidates/<snapshot_id> \
        --review .local/reference-import/review.json

默认输出到 ``.local/reference-import``（Git 忽略）；不写业务数据库、不改 ``.env``。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from adapters.sources.reference_import.cli import main  # noqa: E402

if __name__ == "__main__":  # pragma: no cover - 脚本入口
    raise SystemExit(main())
