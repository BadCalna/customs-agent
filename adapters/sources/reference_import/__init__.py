"""官方参考数据导入流水线（P23）。

把用户已取得的官方资料转换成 P14/P17 能真实查询的数据，并提供可重复运行、
可追溯、可复核的候选快照与显式发布流程。

入口
----
- CLI：``python -m adapters.sources.reference_import.cli`` 或 ``scripts/import_reference_data.py``；
- 编程接口：:func:`build_snapshot`（生成候选快照）、:func:`run_query_checks`
  （用真实适配器验证，见 :mod:`~adapters.sources.reference_import.query_check`）、
:func:`publish_release`（显式发布）。

边界
----
不写业务数据库、不改 ``.env``、不联网、不调用模型、不合并 PR。真实数据与报告写入
Git 忽略目录（默认 ``.local/reference-import``）。
"""

from __future__ import annotations

from adapters.sources.reference_import.convert_catalog import (
    Versioning,
    build_catalog,
    read_hs_code,
)
from adapters.sources.reference_import.convert_commentary import build_commentary
from adapters.sources.reference_import.convert_tax import build_tax_rates, rate_from_cell
from adapters.sources.reference_import.model import (
    CONVERTER_VERSION,
    DatasetId,
    DatasetKind,
    DatasetStatus,
    ExitCode,
    RecordStatus,
    SOURCES_SCHEMA_VERSION,
)
from adapters.sources.reference_import.publish import (
    PublishError,
    PublishRequest,
    activate_release,
    dataset_paths,
    list_releases,
    load_review,
    publish_release,
)
from adapters.sources.reference_import.snapshot import (
    DEFAULT_OUT_ROOT,
    SnapshotBuild,
    SnapshotFailed,
    SnapshotRequest,
    build_snapshot,
)
from adapters.sources.reference_import.sources import (
    LayoutError,
    SourceError,
    SourceManifest,
    load_source_manifest,
    verify_sources_unchanged,
)

__all__ = [
    "CONVERTER_VERSION",
    "DEFAULT_OUT_ROOT",
    "DatasetId",
    "DatasetKind",
    "DatasetStatus",
    "ExitCode",
    "LayoutError",
    "PublishError",
    "PublishRequest",
    "RecordStatus",
    "SOURCES_SCHEMA_VERSION",
    "SnapshotBuild",
    "SnapshotFailed",
    "SnapshotRequest",
    "SourceError",
    "SourceManifest",
    "Versioning",
    "activate_release",
    "build_catalog",
    "build_commentary",
    "build_snapshot",
    "build_tax_rates",
    "dataset_paths",
    "list_releases",
    "load_review",
    "load_source_manifest",
    "publish_release",
    "rate_from_cell",
    "read_hs_code",
    "verify_sources_unchanged",
]
