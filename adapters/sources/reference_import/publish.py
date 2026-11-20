"""显式发布：复核门、发布指纹、保留旧版本与回滚（P23）。

口径
----
- **候选快照**（``candidates/<snapshot_id>/``）是程序产物，永远不自动变成业务来源；
- **发布**（``published/releases/<release_id>/``）必须显式执行，且要满足：
  1. 快照自检完整（每个输出文件的 SHA-256 与快照索引一致）；
  2. 查询验证已跑过且全部通过；
  3. 批次是**真实**数据（合成批次一律不允许发布）；
  4. 若提供复核文件：``decision=approved``、复核人有名有姓、``reviewed_at`` 是带时区
     的 ISO 时间，并且对本快照的**每条业务假设逐项确认**；
- 没有复核文件时**允许**发布，但数据集的 ``review_status`` 保持 ``unreviewed``，
  P14/P17 因此仍然不会给出“可作依据”的结论——发布不等于复核；
- 程序不会替你填复核人，也不会把“程序校验通过”写成“专业复核通过”；
- 覆盖已发布内容需要 ``--allow-content-change`` 并写明理由，同时输出内容差异报告；
- 旧 release 目录不删除，``release.json`` 保留首次发布审计且永不改写；
  重复发布/显式回滚分别追加 ``activations/<event_id>.json`` 与
  ``activation-results/<event_id>.json``，本次理由、前驱和差异属于激活事件。
- POSIX 的 ``current`` 软链是唯一激活提交点，``current.json`` 是其派生链接；
  Windows 使用带版本标识的普通 ``current.json`` 原子指针，不创建软链。
  读取方先解析一次指针，再读取固定 release；可随时 ``activate`` 回滚。

**不修改 ``.env``，不切换运行环境的数据来源**：这里只产出目录与指针，
环境变量由使用方自己指过来（D6 第 2.7 节）。
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
import shutil
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Iterable, Mapping

from adapters.files.process_lock import acquire_exclusive, release_exclusive
from adapters.sources.reference_import.model import (
    RELEASE_SCHEMA_VERSION,
    REVIEW_SCHEMA_VERSION,
    DatasetId,
    DatasetKind,
    fingerprint,
    is_iso_datetime,
    read_json,
    sha256_file,
    utc_now,
    visible_text,
    write_json_atomic,
)


class PublishError(RuntimeError):
    """发布被拒绝；调用方必须把原因原样报给使用者。"""


POINTER_BACKEND = "atomic_json" if os.name == "nt" else "symlink"
CURRENT_SCHEMA_VERSION = "p23.current.1"
_DATASET_SCHEMAS = {
    DatasetId.P14_TARIFF: "p14.tariff.1",
    DatasetId.P14_ELEMENTS: "p14.elements.1",
    DatasetId.P17_TAX_RATES: "p17.taxrate.1",
    DatasetId.P17_REGULATORY: "p17.regulatory.1",
    DatasetId.TARIFF_COMMENTARY: "p23.commentary.1",
    DatasetId.REGULATORY_CODE_LEDGER: "p23.regulatory-codes.1",
}
_ACTIVATION_HOOK = None


def _activation_checkpoint(stage: str) -> None:
    """No-op unless a fault-injection test installs a process-local hook."""
    if _ACTIVATION_HOOK is not None:
        _ACTIVATION_HOOK(stage)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def require_plain_path(path: str | Path) -> None:
    """Reject redirection before resolving or writing any publication path.

    Inspect every existing ancestor, including symlinks and all Windows reparse
    tags. Nonexistent final components are allowed for newly created directories.
    """
    candidate = Path(path).absolute()
    for entry in reversed((candidate, *candidate.parents)):
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PublishError("发布路径不可检查") from exc
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise PublishError("发布路径及祖先不得为软链、junction 或其他 reparse point")


def _write_current_pointer(path: Path, payload: Mapping[str, Any]) -> None:
    """Windows' sole commit operation: fsynced temporary file + atomic replace."""
    write_json_atomic(path, {
        "schema_version": CURRENT_SCHEMA_VERSION, "pointer_backend": "atomic_json", **payload,
    })


@dataclass(frozen=True)
class ReviewRecord:
    """一份人工复核记录；程序只校验格式与完整性，不校验“人是谁”。"""

    reviewed_by: str
    reviewed_at: str
    decision: str
    scope: tuple[str, ...]
    confirmations: Mapping[str, bool]
    notes: str = ""
    path: str = ""
    sha256: str = ""

    def covers(self, dataset: DatasetId) -> bool:
        return not self.scope or dataset.value in self.scope

    def as_dict(self) -> dict[str, Any]:
        return {
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
            "decision": self.decision,
            "scope": list(self.scope),
            "confirmations": dict(self.confirmations),
            "notes": self.notes,
            "file": self.path,
            "file_sha256": self.sha256,
        }


_REVIEW_DECISIONS = ("approved", "rejected", "changes_requested")


def load_review(path: str | Path) -> ReviewRecord:
    """读取并校验复核文件；缺字段/时间不带时区/决策不明一律拒绝。"""

    review_path = Path(path)
    try:
        payload = read_json(review_path)
    except OSError as exc:
        raise PublishError(f"复核文件不可读：{review_path.name}（{type(exc).__name__}）") from exc
    except ValueError as exc:
        raise PublishError(f"复核文件不是合法 JSON：{review_path.name}（{exc}）") from exc
    if not isinstance(payload, Mapping):
        raise PublishError("复核文件顶层必须是对象")
    if payload.get("schema_version") != REVIEW_SCHEMA_VERSION:
        raise PublishError(
            f"复核文件结构版本不受支持：{payload.get('schema_version')!r}；"
            f"需要 {REVIEW_SCHEMA_VERSION}"
        )
    reviewed_by = visible_text(payload.get("reviewed_by"))
    if not reviewed_by:
        raise PublishError("复核文件必须写明 reviewed_by：程序不会替你填复核人")
    reviewed_at = visible_text(payload.get("reviewed_at"))
    if not is_iso_datetime(reviewed_at):
        raise PublishError("复核文件的 reviewed_at 必须是带时区的 ISO 时间")
    decision = visible_text(payload.get("decision"))
    if decision not in _REVIEW_DECISIONS:
        raise PublishError(
            f"复核结论必须是 {'/'.join(_REVIEW_DECISIONS)} 之一，实际 {decision!r}"
        )
    scope_raw = payload.get("scope", [])
    if not isinstance(scope_raw, list):
        raise PublishError("复核文件的 scope 必须是数组")
    if any(not isinstance(item, str) or not visible_text(item) for item in scope_raw):
        raise PublishError("复核文件的 scope 每项必须是非空数据类别")
    scope = tuple(visible_text(item) for item in scope_raw)
    unknown = [
        item
        for item in scope
        if item not in {dataset.value for dataset in DatasetId}
    ]
    if unknown:
        raise PublishError("复核文件的 scope 含未知数据类别：" + "、".join(unknown))
    confirmations_raw = payload.get("confirmations", {})
    if not isinstance(confirmations_raw, Mapping):
        raise PublishError("复核文件的 confirmations 必须是对象")
    confirmations: dict[str, bool] = {}
    for key, value in confirmations_raw.items():
        if not isinstance(value, bool):
            raise PublishError(f"复核确认项 {key} 必须是布尔值")
        confirmations[visible_text(key)] = value
    return ReviewRecord(
        reviewed_by=reviewed_by,
        reviewed_at=reviewed_at,
        decision=decision,
        scope=scope,
        confirmations=confirmations,
        notes=visible_text(payload.get("notes")),
        path=review_path.name,
        sha256=sha256_file(review_path),
    )


# ---------------------------------------------------------------------------
# 快照自检
# ---------------------------------------------------------------------------


def _load_snapshot(snapshot_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest_path = snapshot_dir / "manifest.json"
    report_path = snapshot_dir / "report.json"
    if not manifest_path.is_file() or not report_path.is_file():
        raise PublishError(f"不是完整的快照目录（缺 manifest.json/report.json）：{snapshot_dir.name}")
    manifest = read_json(manifest_path)
    report = read_json(report_path)
    if int(report.get("exit_code", 2)) == 2:
        raise PublishError("该快照的导入结果是失败（exit_code=2），不允许发布")
    outputs = manifest.get("outputs") or {}
    if not outputs:
        raise PublishError("快照索引里没有输出文件，不能发布空快照")
    for name, info in outputs.items():
        path = snapshot_dir / info["file"]
        if not path.is_file():
            raise PublishError(f"快照缺输出文件：{info['file']}")
        if sha256_file(path) != info["sha256"]:
            raise PublishError(f"输出文件 {info['file']} 的 SHA-256 与快照索引不一致，拒绝发布")
    return manifest, report


def _assumption_keys(assumptions: Iterable[str]) -> list[str]:
    """从假设文本里取出可确认的键：``key：说明`` 形式的前缀。"""

    keys: list[str] = []
    for item in assumptions:
        prefix, sep, _ = item.partition("：")
        candidate = prefix.strip()
        if sep and candidate and all(char.isalnum() or char in "_-" for char in candidate):
            keys.append(candidate)
    return keys


def _require_confirmations(
    manifest: Mapping[str, Any], review: ReviewRecord
) -> None:
    required = _assumption_keys(manifest.get("assumptions") or [])
    missing = [key for key in required if review.confirmations.get(key) is not True]
    if missing:
        raise PublishError(
            "复核文件没有逐项确认这些业务假设：" + "、".join(missing)
        )


def _apply_review(payload: Mapping[str, Any], review: ReviewRecord) -> dict[str, Any]:
    """把复核信息写进 dataset 段（只改元数据，不动业务记录）。"""

    updated = dict(payload)
    dataset = updated.get("dataset")
    if not isinstance(dataset, Mapping):
        return updated
    dataset = dict(dataset)
    dataset["review_status"] = "reviewed"
    dataset["reviewed_by"] = review.reviewed_by
    dataset["reviewed_at"] = review.reviewed_at
    dataset["notes"] = (
        (visible_text(dataset.get("notes")) + "；" if dataset.get("notes") else "")
        + f"复核记录：{review.reviewed_by} @ {review.reviewed_at}"
        + (f"（{review.notes}）" if review.notes else "")
    )
    updated["dataset"] = dataset
    return updated


def _business_keys(payload: Mapping[str, Any]) -> dict[str, str]:
    """把一份数据集压成 ``业务键 → 内容指纹``，用于差异报告。"""

    if not isinstance(payload, Mapping):
        raise PublishError("发布数据不是对象，无法计算差异")
    keys: dict[str, str] = {}
    versions = payload.get("versions")
    if isinstance(versions, list):
        for index, item in enumerate(versions):
            if not isinstance(item, Mapping):
                continue
            key = visible_text(item.get("version")) or f"versions[{index}]"
            slot = f"versions:{key}"
            if slot in keys:
                slot = f"versions:{key}[{index}]"
            keys[slot] = fingerprint(item)
    version_meta = payload.get("version")
    if isinstance(version_meta, Mapping):
        key = visible_text(version_meta.get("version")) or "default"
        keys[f"version:{key}"] = fingerprint(version_meta)
    for field_name, key_field in (
        ("codes", "code"),
        ("schemas", "hs_code"),
        ("rates", "hs_code"),
        ("records", None),
    ):
        items = payload.get(field_name)
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                continue
            key = visible_text(item.get(key_field)) if key_field else ""
            if not key:
                key = visible_text(item.get("tariff_no")) or f"{field_name}[{index}]"
            keys[f"{field_name}:{key}"] = fingerprint(item)
    return keys


def diff_snapshots(left: Path, right: Path) -> dict[str, Any]:
    """比较两份发布/快照的业务内容；用于“不能静默覆盖已发布版本”。"""

    summary: dict[str, Any] = {"datasets": {}, "changed": False}
    names = {
        DatasetId.P14_TARIFF.filename: DatasetId.P14_TARIFF.value,
        DatasetId.P14_ELEMENTS.filename: DatasetId.P14_ELEMENTS.value,
        DatasetId.P17_TAX_RATES.filename: DatasetId.P17_TAX_RATES.value,
        DatasetId.TARIFF_COMMENTARY.filename: DatasetId.TARIFF_COMMENTARY.value,
        # 监管代码台账也是发布物；漏掉它就能无理由覆盖已发布的台账内容。
        DatasetId.REGULATORY_CODE_LEDGER.filename: DatasetId.REGULATORY_CODE_LEDGER.value,
        DatasetId.P17_REGULATORY.filename: DatasetId.P17_REGULATORY.value,
    }
    for filename, label in names.items():
        left_path = left / filename
        right_path = right / filename
        if not left_path.is_file() and not right_path.is_file():
            continue
        if not left_path.is_file() or not right_path.is_file():
            summary["datasets"][label] = {
                "only_in": "right" if right_path.is_file() else "left",
                "changed": True,
            }
            summary["changed"] = True
            continue
        try:
            left_payload = read_json(left_path)
            right_payload = read_json(right_path)
        except (OSError, ValueError, UnicodeError) as exc:
            raise PublishError(f"差异计算失败：{label} 的发布数据不可读") from exc
        for side, payload in (("左侧", left_payload), ("右侧", right_payload)):
            if not isinstance(payload, Mapping):
                raise PublishError(
                    f"差异计算失败：{label} 的{side}发布数据不是对象"
                )
        left_keys = _business_keys(left_payload)
        right_keys = _business_keys(right_payload)
        added = sorted(set(right_keys) - set(left_keys))
        removed = sorted(set(left_keys) - set(right_keys))
        changed = sorted(
            key for key in set(left_keys) & set(right_keys) if left_keys[key] != right_keys[key]
        )
        if added or removed or changed:
            summary["changed"] = True
            summary["datasets"][label] = {
                "added": len(added),
                "removed": len(removed),
                "changed": len(changed),
                "samples": {
                    "added": added[:5],
                    "removed": removed[:5],
                    "changed": changed[:5],
                },
            }
    return summary


# ---------------------------------------------------------------------------
# 发布
# ---------------------------------------------------------------------------


@dataclass
class PublishRequest:
    snapshot_dir: Path
    out_root: Path
    review_path: Path | None = None
    allow_content_change: bool = False
    reason: str = ""
    activate: bool = True


@dataclass
class PublishResult:
    release_id: str
    release_dir: Path
    release: dict[str, Any]
    activated: bool
    changed: bool = False
    diff: dict[str, Any] = field(default_factory=dict)
    activation: dict[str, Any] | None = None
    activation_result: dict[str, Any] | None = None


def _verification_for(out_root: Path, snapshot_id: str) -> dict[str, Any] | None:
    path = out_root / "verification" / f"{snapshot_id}.json"
    require_plain_path(path)
    if not path.is_file():
        return None
    report = _read_release_json(path)
    if (report.get("schema_version") != "p23.query-check.1"
            or report.get("snapshot_dir") != snapshot_id):
        raise PublishError("查询验证报告结构版本或快照身份不匹配")
    checks = report.get("checks")
    if not isinstance(checks, list) or not checks:
        raise PublishError("查询验证报告必须包含非空 checks")
    counts = {"pass": 0, "fail": 0, "skip": 0}
    ids = set()
    for check in checks:
        if (not isinstance(check, dict) or not isinstance(check.get("id"), str)
                or not check["id"].strip() or check["id"] in ids
                or not isinstance(check.get("status"), str) or check["status"] not in counts):
            raise PublishError("查询验证报告的检查身份/状态非法或重复")
        ids.add(check["id"])
        counts[check["status"]] += 1
    declared = report.get("counts")
    if (not isinstance(declared, dict) or set(declared) != set(counts)
            or any(type(declared[key]) is not int or declared[key] != counts[key] for key in counts)):
        raise PublishError("查询验证报告 counts 与实际检查不一致")
    if (type(report.get("all_passed")) is not bool
            or report["all_passed"] != (counts["fail"] == 0)
            or (counts["pass"] == 0 and counts["fail"] == 0)):
        raise PublishError("查询验证报告 all_passed 必须是与实际检查一致的布尔值")
    return report


_GLOBAL_LOCK = threading.Lock()
_THREAD_LOCKS: dict[Path, threading.RLock] = {}
_LOCK_DEPTH: dict[tuple[int, Path], int] = {}


@contextmanager
def _publish_lock(out_root: Path, *, timeout: float | None = 30.0) -> Iterator[None]:
    """跨线程 + 跨进程发布排他锁；同线程重入不自锁。"""

    require_plain_path(out_root / "published" / ".publish.lock")
    key = out_root.resolve()
    ident = threading.get_ident()
    if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
        raise ValueError("lock timeout must be finite and nonnegative, or None")
    deadline = None if timeout is None else time.monotonic() + timeout
    with _GLOBAL_LOCK:
        rlock = _THREAD_LOCKS.setdefault(key, threading.RLock())
    thread_acquired = rlock.acquire() if timeout is None else rlock.acquire(timeout=timeout)
    if not thread_acquired:
        raise PublishError("等待发布线程锁超时")
    try:
        with _GLOBAL_LOCK:
            depth = _LOCK_DEPTH.get((ident, key), 0)
        lock_handle = None
        acquired = False
        try:
            if depth == 0:
                published_dir = out_root / "published"
                published_dir.mkdir(parents=True, exist_ok=True)
                lock_path = published_dir / ".publish.lock"
                lock_handle = lock_path.open("a+b")
                try:
                    remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                    acquire_exclusive(lock_handle.fileno(), timeout=remaining)
                except (OSError, TimeoutError) as exc:
                    raise PublishError("获取发布文件锁失败或超时") from exc
                acquired = True
            with _GLOBAL_LOCK:
                _LOCK_DEPTH[(ident, key)] = depth + 1
            yield
        finally:
            try:
                if lock_handle is not None:
                    try:
                        if acquired:
                            release_exclusive(lock_handle.fileno())
                    finally:
                        lock_handle.close()
            finally:
                with _GLOBAL_LOCK:
                    remaining = _LOCK_DEPTH.get((ident, key), 1) - 1
                    if remaining <= 0:
                        _LOCK_DEPTH.pop((ident, key), None)
                    else:
                        _LOCK_DEPTH[(ident, key)] = remaining
    finally:
        rlock.release()


def _read_release_json(path: Path) -> dict[str, Any]:
    require_plain_path(path)
    try:
        payload = read_json(path)
    except (OSError, ValueError, UnicodeError) as exc:
        raise PublishError(f"发布元数据不可读：{path.name}") from exc
    if not isinstance(payload, dict):
        raise PublishError(f"发布元数据必须是对象：{path.name}")
    return payload


def _release_metadata(release_dir: Path, *, require_pointer: bool = False) -> dict[str, Any]:
    release = _read_release_json(release_dir / "release.json")
    if release.get("schema_version") != RELEASE_SCHEMA_VERSION:
        raise PublishError("发布元数据 schema_version 不受支持")
    if release.get("release_id") != release_dir.name:
        raise PublishError("发布元数据 release_id 与目录不匹配")
    for field_name in ("release_fingerprint", "business_fingerprint"):
        value = release.get(field_name)
        if not isinstance(value, str) or not __import__("re").fullmatch(r"[0-9a-f]{64}", value):
            raise PublishError(f"发布元数据 {field_name} 非法")
    expected_id = f"rel-{str(release.get('snapshot_id', '')).split('-')[-1]}-{release['release_fingerprint'][:12]}"
    if expected_id != release_dir.name:
        raise PublishError("发布指纹、快照与 release 目录不匹配")
    pointer = release_dir / ".current.json"
    if pointer.exists() or pointer.is_symlink():
        if pointer.is_symlink():
            raise PublishError("release 内的 .current.json 不得为软链")
        meta = _read_release_json(pointer)
        for field_name in ("release_id", "release_fingerprint", "business_fingerprint", "review_status"):
            if meta.get(field_name) != release.get(field_name):
                raise PublishError(f".current.json 的 {field_name} 与发布元数据不匹配")
    elif require_pointer:
        raise PublishError("当前 release 缺少 .current.json")
    return release


def _validate_activation_target(release_dir: Path) -> dict[str, Any]:
    """提交前校验完整发布物；不得把损坏版本通过 activate 绕过发布门。"""

    require_plain_path(release_dir)
    if _is_link(release_dir):
        raise PublishError("release 目录不得为软链或 junction")
    release = _release_metadata(release_dir)
    if release.get("dataset_kind") != DatasetKind.REAL.value:
        raise PublishError("激活目标必须是真实数据发布")
    review = release.get("review")
    expected_review_status = "reviewed" if review is not None else "unreviewed"
    if release.get("review_status") != expected_review_status:
        raise PublishError("发布复核状态与复核记录不匹配")
    if review is not None and (
        not isinstance(review, dict) or review.get("decision") != "approved"
        or not visible_text(review.get("reviewed_by"))
        or not is_iso_datetime(review.get("reviewed_at"))
        or not isinstance(review.get("scope"), list)
    ):
        raise PublishError("发布复核记录非法")
    files = release.get("files")
    if not isinstance(files, dict) or not files:
        raise PublishError("发布元数据缺少非空 files")
    output_fingerprints = {}
    for name, info in files.items():
        try:
            dataset = DatasetId(name)
        except ValueError as exc:
            raise PublishError(f"未知发布数据类别：{name}") from exc
        if not isinstance(info, dict) or info.get("file") != dataset.filename:
            raise PublishError(f"发布文件映射不合法：{name}")
        path = release_dir / dataset.filename
        try:
            if path.is_symlink() or not path.is_file():
                raise PublishError(f"发布数据文件缺失或不是普通文件：{name}")
            if path.stat().st_size != info.get("bytes") or sha256_file(path) != info.get("sha256"):
                raise PublishError(f"发布数据文件 SHA-256/字节数不匹配：{name}")
            try:
                payload = _read_release_json(path)
            except PublishError as exc:
                raise PublishError(f"发布数据文件不可读：{name}（{exc}）") from exc
        except OSError as exc:
            raise PublishError(f"发布数据文件不可读：{name}") from exc
        if payload.get("schema_version") != _DATASET_SCHEMAS[dataset]:
            raise PublishError(f"发布数据 schema_version 不受支持：{name}")
        dataset_meta = payload.get("dataset")
        if not isinstance(dataset_meta, dict) or dataset_meta.get("synthetic") is not False:
            raise PublishError(f"发布数据性质与真实批次不匹配：{name}")
        scope = review.get("scope", []) if review else []
        reviewed = review is not None and (not scope or name in scope)
        expected = "reviewed" if reviewed else "unreviewed"
        if dataset_meta.get("review_status") != expected or info.get("review_status") != expected:
            raise PublishError(f"发布文件复核状态与发布元数据不匹配：{name}")
        if reviewed and (
            dataset_meta.get("reviewed_by") != review["reviewed_by"]
            or dataset_meta.get("reviewed_at") != review["reviewed_at"]
        ):
            raise PublishError(f"发布文件复核人与时间不匹配：{name}")
        output_fingerprints[name] = fingerprint(payload)
    declared_files = {info["file"] for info in files.values()}
    for dataset in DatasetId:
        if dataset.filename not in declared_files:
            extra = release_dir / dataset.filename
            if extra.exists() or extra.is_symlink():
                raise PublishError(f"发布目录存在未声明的数据文件：{dataset.value}")
    calculated = fingerprint({
        "snapshot_business_fingerprint": release["business_fingerprint"],
        "converter_version": release.get("converter_version"),
        "review": release.get("review"),
        "outputs": output_fingerprints,
    })
    if calculated != release["release_fingerprint"]:
        raise PublishError("发布元数据与内容的 release_fingerprint 不匹配")
    return release


def _activation_result_requires_repair(result_file: Path) -> bool:
    """只有明确落盘为 failed 的激活结果文件才不视作历史激活。"""

    if result_file.is_symlink() or not result_file.is_file():
        return True
    try:
        result_payload = read_json(result_file)
    except (OSError, ValueError, UnicodeError):
        return True
    return not isinstance(result_payload, Mapping) or result_payload.get("status") != "failed"


def _has_prior_activation(out_root: Path, *, published_dir: Path | None = None) -> bool:
    """判断发布目录是否曾发生过激活（或存在遗留/未确认的激活指针）。

    若仅以 ``--no-activate`` 生成过 release 且从未激活过任何版本，
    ``current`` 按设计为空，不应把这种情况误判成“指针丢失或损坏”。
    """

    published_dir = published_dir if published_dir is not None else out_root / "published"
    pointer = published_dir / "current.json"
    if pointer.exists() or pointer.is_symlink():
        return True

    releases_dir = published_dir / "releases"
    if releases_dir.is_dir():
        for entry in releases_dir.iterdir():
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            release_pointer = entry / ".current.json"
            if release_pointer.exists() or release_pointer.is_symlink():
                return True

    activations_dir = published_dir / "activations"
    results_dir = published_dir / "activation-results"
    for dir_path in (activations_dir, results_dir):
        if (dir_path.exists() or dir_path.is_symlink()) and not dir_path.is_dir():
            return True

    if activations_dir.is_dir():
        for event_file in sorted(activations_dir.iterdir()):
            if event_file.name.startswith(".") or event_file.suffix != ".json":
                continue
            if event_file.is_symlink() or not event_file.is_file():
                return True
            if _activation_result_requires_repair(results_dir / event_file.name):
                # 写过激活意图但缺少结果文件（如强杀中断）或非 failed，不能视为从未激活。
                return True

    if results_dir.is_dir():
        for result_file in sorted(results_dir.iterdir()):
            if result_file.name.startswith(".") or result_file.suffix != ".json":
                continue
            if _activation_result_requires_repair(result_file):
                return True

    return False


def _active_release_id(out_root: Path, *, published_dir: Path | None = None) -> str | None:
    """只解析一次 current，随后只读固定 release 目录，不再穿过 current.json。"""

    require_plain_path(published_dir if published_dir is not None else out_root / "published")
    if POINTER_BACKEND == "atomic_json":
        return _active_json_release_id(out_root, published_dir=published_dir)
    published_dir = published_dir if published_dir is not None else out_root / "published"
    link = published_dir / "current"
    pointer = published_dir / "current.json"
    try:
        target = os.readlink(link)
    except FileNotFoundError:
        # 首次发布是“先建 current.json、后切 current”。若指针已出现而 current 尚未
        # 切好，属于发布进行中的瞬时窗口：短暂重试，而不是报“没有发布”或指针损坏。
        try:
            # 当前版本缺失，但还留着一个普通 JSON 指针：这是需要显式修复的遗留状态，
            # 不能当成“从未发布”，否则会绕过换版门禁。
            if pointer.exists() and not pointer.is_symlink():
                raise PublishError(
                    "current.json 必须是 current/.current.json 的派生软链；可用 activate 修复"
                )
            if pointer.is_symlink() and os.readlink(pointer) != os.path.join("current", ".current.json"):
                raise PublishError("current.json 的派生软链目标不合法；可用 activate 修复")
            if pointer.is_symlink() or _has_prior_activation(out_root, published_dir=published_dir):
                import time

                for _ in range(5):
                    time.sleep(0.01)
                    try:
                        target = os.readlink(link)
                        break
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        raise PublishError("current 必须是发布目录软链") from exc
                else:
                    return None
            else:
                return None
        except PublishError:
            raise
        except OSError as exc:
            raise PublishError("读取 current/current.json 指针失败") from exc
    except OSError as exc:
        raise PublishError("current 必须是发布目录软链") from exc
    release_id = Path(target).name
    if not _RELEASE_ID.fullmatch(release_id) or target != os.path.join("releases", release_id):
        raise PublishError("current 的发布目录目标不合法")
    release_dir = published_dir / "releases" / release_id
    _release_metadata(release_dir, require_pointer=True)
    # current.json 只是兼容派生路径。首次发布时它可能尚未创建（读取与发布切换重叠），
    # 不能因此把已经可解析的 current 误报成“指针损坏”。它若存在，则必须是正确软链；
    # 遗留的普通 JSON 文件仍要求显式 activate 修复。指针读取异常统一转 PublishError。
    try:
        if pointer.is_symlink():
            if os.readlink(pointer) != os.path.join("current", ".current.json"):
                raise PublishError("current.json 的派生软链目标不合法；可用 activate 修复")
        elif pointer.exists():
            raise PublishError("current.json 必须是 current/.current.json 的派生软链；可用 activate 修复")
    except PublishError:
        raise
    except OSError as exc:
        raise PublishError("读取 current/current.json 指针失败") from exc
    return release_id


def _active_json_release_id(out_root: Path, *, published_dir: Path | None = None) -> str | None:
    """Windows: read one atomic pointer, then validate only its pinned release."""
    published = published_dir if published_dir is not None else out_root / "published"
    legacy = published / "current"
    pointer = published / "current.json"
    if legacy.exists() or _is_link(legacy):
        raise PublishError("Windows current 旧软链/目录布局不受支持；请显式迁移后 activate")
    if _is_link(pointer):
        raise PublishError("Windows current.json 必须是普通原子指针文件；请 activate 修复")
    if not pointer.exists():
        return None
    meta = _read_release_json(pointer)
    if (meta.get("schema_version") != CURRENT_SCHEMA_VERSION
            or meta.get("pointer_backend") != "atomic_json"):
        raise PublishError("Legacy current.json 缺少原子指针版本；请 activate 显式修复")
    release_id = meta.get("release_id")
    if not isinstance(release_id, str) or not _RELEASE_ID.fullmatch(release_id):
        raise PublishError("current.json 的 release_id 不合法")
    release_dir = published / "releases" / release_id
    try:
        release = _validate_activation_target(release_dir)
        _release_metadata(release_dir, require_pointer=True)
    except PublishError as exc:
        raise PublishError(f"当前已发布版本（{release_id}）损坏：{exc}；请 activate 恢复") from exc
    for field_name in ("release_id", "release_fingerprint", "business_fingerprint", "review_status"):
        if meta.get(field_name) != release.get(field_name):
            raise PublishError(f"current.json 的 {field_name} 与发布元数据不匹配")
    return release_id


def _current_release_dir(out_root: Path) -> Path | None:
    release_id = _active_release_id(out_root)
    if not release_id:
        return None
    release_dir = out_root / "published" / "releases" / release_id
    return release_dir if release_dir.is_dir() else None


def current_release_dir(out_root: str | Path) -> Path:
    """Pin the active release once and validate its complete published contents.

    Consumers must read returned immutable paths, not construct ``current/file``;
    Windows intentionally exposes only a versioned atomic ``current.json``.
    """
    return published_release_dir(Path(out_root) / "published")


def published_release_dir(published_dir: str | Path) -> Path:
    """Pin exactly the configured published directory; never search sibling roots."""
    published = Path(published_dir)
    release_id = _active_release_id(published.parent, published_dir=published)
    if release_id is None:
        raise PublishError("还没有可读取的 current 发布指针")
    release_dir = published / "releases" / release_id
    _validate_activation_target(release_dir)
    return release_dir


def _previous_for_publish(out_root: Path) -> Path | None:
    """发布入口的前驱：没有当前版本时，只有“从未激活过任何版本”才允许无前驱。

    若已激活过版本但 ``current`` 丢失/损坏，不能当成首次发布，
    否则内容不同的新版本会绕过换版门禁并以“无前驱、内容未变”记审计；
    若仅以 ``--no-activate`` 生成过 release，``current`` 按设计为空，应允许继续发布/重试。
    """

    try:
        previous = _current_release_dir(out_root)
    except PublishError as exc:
        if "activate" in str(exc):
            raise
        raise PublishError(
            f"当前已发布版本指针或元数据损坏：{exc}；请先运行 activate 指定完好版本恢复，再发布新版本"
        ) from exc
    if previous is not None:
        return previous
    try:
        prior_activation = _has_prior_activation(out_root)
    except OSError as exc:
        raise PublishError("读取发布指针与激活记录失败") from exc
    if prior_activation:
        raise PublishError(
            "当前版本指针丢失或损坏（已有激活记录，但 current 无法解析）；"
            "请先运行 activate 指定版本修复，再发布新版本"
        )
    return None


def _validate_previous_for_publish(previous: Path) -> None:
    """在计算差异前校验当前活动版本完整性；损坏时拒绝普通 publish 并提示先用 activate 恢复。"""

    try:
        _validate_activation_target(previous)
    except PublishError as exc:
        raise PublishError(
            f"当前已发布版本（{previous.name}）损坏或与发布清单不一致：{exc}；"
            "请先运行 activate 切到完好版本恢复，再发布新版本"
        ) from exc


def _validate_existing_release_match(
    release_dir: Path,
    release_id: str,
    release_fingerprint: str,
    files: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """校验既有 release 目录完整性并确认与本次候选发布物完全一致，再允许复用。"""

    if release_dir.is_symlink() or not release_dir.is_dir():
        raise PublishError(f"已存在 release 路径不是目录：{release_id}")
    release = _validate_activation_target(release_dir)
    if release.get("release_fingerprint") != release_fingerprint:
        raise PublishError(f"已存在 release 的发布指纹不匹配：{release_id}")
    if set((release.get("files") or {}).keys()) != set(files.keys()):
        raise PublishError(f"已存在 release 的数据类别清单不匹配：{release_id}")
    for name, info in files.items():
        path = release_dir / info["file"]
        if not path.is_file() or sha256_file(path) != info["sha256"]:
            raise PublishError(f"已存在 release 的数据文件损坏：{name}")
    return release


def _validate_and_diff_previous(
    previous: Path | None,
    candidate_dir: Path,
    *,
    allow_content_change: bool,
    reason: str,
) -> tuple[dict[str, Any], bool]:
    """校验前驱发布物完整性后计算差异，防止基于损坏前驱跳过换版门禁。"""

    diff: dict[str, Any] = {}
    changed = False
    if previous is not None:
        _validate_previous_for_publish(previous)
        diff = diff_snapshots(previous, candidate_dir)
        changed = bool(diff.get("changed"))
        if changed:
            if not allow_content_change:
                raise PublishError(
                    "待发布内容与当前已发布版本不同；默认不覆盖已发布数据。"
                    "确认要换版请加 --allow-content-change 并写明 --reason。"
                    "差异：" + json.dumps(diff, ensure_ascii=False)
                )
            if not reason:
                raise PublishError(
                    "待发布内容与当前已发布版本不同；已开启 --allow-content-change，"
                    "但未填写换版理由（--reason 不能为空）。"
                )
    return diff, changed


def _check_content_change(
    previous: Path | None,
    candidate_dir: Path,
    *,
    allow_content_change: bool,
    reason: str,
) -> tuple[dict[str, Any], bool]:
    return _validate_and_diff_previous(
        previous,
        candidate_dir,
        allow_content_change=allow_content_change,
        reason=reason,
    )


def publish_release(request: PublishRequest) -> PublishResult:
    """把候选快照发布成一个不可变 release；必要时切换 current 指针。"""

    snapshot_dir = Path(request.snapshot_dir)
    out_root = Path(request.out_root)
    reason = visible_text(request.reason)
    if request.allow_content_change and not reason:
        raise PublishError(
            "允许换版（--allow-content-change）时必须同时填写非空换版理由（--reason）"
        )
    manifest, report = _load_snapshot(snapshot_dir)
    snapshot_id = visible_text(manifest.get("snapshot_id")) or snapshot_dir.name
    if manifest.get("dataset_kind") != DatasetKind.REAL.value:
        raise PublishError(
            "合成批次不允许发布成业务来源；请在来源清单里把 dataset_kind 设为 real 并确认真实原件"
        )
    verification = _verification_for(out_root, snapshot_id)
    if verification is None:
        raise PublishError(
            f"没有 {snapshot_id} 的查询验证报告：先跑 verify，未验证的快照不能发布"
        )
    if verification["all_passed"] is not True:
        failed = [
            item["id"] for item in verification.get("checks", []) if item["status"] == "fail"
        ]
        raise PublishError("查询验证未通过，拒绝发布：" + "、".join(failed[:8]))

    review: ReviewRecord | None = None
    if request.review_path is not None:
        review = load_review(request.review_path)
        if review.decision != "approved":
            raise PublishError(
                f"复核结论是 {review.decision}，不是 approved；不允许发布成已复核数据"
            )
        _require_confirmations(manifest, review)

    # ---- 计算发布内容与指纹
    outputs = manifest.get("outputs") or {}
    payloads: dict[str, dict[str, Any]] = {}
    for name, info in outputs.items():
        dataset = DatasetId(name)
        payload = read_json(snapshot_dir / info["file"])
        if review is not None and review.covers(dataset):
            payload = _apply_review(payload, review)
        payloads[name] = payload
    release_fingerprint = fingerprint(
        {
            "snapshot_business_fingerprint": manifest.get("business_fingerprint"),
            "converter_version": manifest.get("converter_version"),
            "review": review.as_dict() if review else None,
            "outputs": {
                name: fingerprint(payload) for name, payload in sorted(payloads.items())
            },
        }
    )
    suffix = release_fingerprint[:12]
    review_tag = "reviewed" if review is not None else "unreviewed"
    release_id = f"rel-{snapshot_id.split('-')[-1]}-{suffix}"
    releases_dir = out_root / "published" / "releases"
    require_plain_path(releases_dir)
    release_dir = releases_dir / release_id
    temp_dir = releases_dir / f".tmp-{release_id}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    activated = False
    activation = None
    activation_result = None
    try:
        files: dict[str, Any] = {}
        for name, payload in sorted(payloads.items()):
            filename = DatasetId(name).filename
            write_json_atomic(temp_dir / filename, payload)
            files[name] = {
                "file": filename,
                "sha256": sha256_file(temp_dir / filename),
                "bytes": (temp_dir / filename).stat().st_size,
                "review_status": (payload.get("dataset") or {}).get("review_status"),
            }
        # 先在锁外做一次预检，兼容在 _current_release_dir/diff_snapshots 上设屏障的并发测试；
        # 真正落盘 release.json 与切换 current 必须在排他锁内复核当前指针，防止交错覆盖。
        observed_previous = _previous_for_publish(out_root)
        diff, changed = _check_content_change(
            observed_previous,
            temp_dir,
            allow_content_change=request.allow_content_change,
            reason=reason,
        )
        with _publish_lock(out_root):
            previous = _previous_for_publish(out_root)
            while True:
                if previous != observed_previous:
                    diff, changed = _check_content_change(
                        previous,
                        temp_dir,
                        allow_content_change=request.allow_content_change,
                        reason=reason,
                    )
                    observed_previous = previous
                elif previous is not None:
                    # 即使前驱路径未变，锁内仍须重验前驱完整性并重算差异，消除锁前到锁内的 TOCTOU 窗口。
                    diff, changed = _validate_and_diff_previous(
                        previous,
                        temp_dir,
                        allow_content_change=request.allow_content_change,
                        reason=reason,
                    )
                after_diff_previous = _previous_for_publish(out_root)
                if after_diff_previous == previous:
                    break
                previous = after_diff_previous
            previous_release_id = previous.name if previous is not None else None
            release = {
                "schema_version": RELEASE_SCHEMA_VERSION,
                "release_id": release_id,
                "snapshot_id": snapshot_id,
                "dataset_kind": manifest.get("dataset_kind"),
                "converter_version": manifest.get("converter_version"),
                "business_fingerprint": manifest.get("business_fingerprint"),
                "release_fingerprint": release_fingerprint,
                "review_status": review_tag,
                "review": review.as_dict() if review else None,
                "published_at": utc_now(),
                "source_snapshot": snapshot_id,
                "files": files,
                "content_changed": changed,
                "content_diff": diff,
                "reason": reason,
                "previous_release": previous_release_id,
            }
            write_json_atomic(temp_dir / "release.json", release)
            write_json_atomic(temp_dir / "report.json", report)
            if release_dir.exists() or release_dir.is_symlink():
                # 内容寻址的 release 保留首次发布审计，绝不能把本次激活的审计写回去；
                # 复用前必须完整校验既有 release 的结构、指纹与全部数据文件，拒绝复用被篡改目录。
                release = _validate_existing_release_match(
                    release_dir,
                    release_id,
                    release_fingerprint,
                    files,
                )
                shutil.rmtree(temp_dir)
            else:
                try:
                    os.replace(temp_dir, release_dir)
                except OSError as exc:
                    raise PublishError(f"发布目录落盘失败：{release_id}") from exc
            if request.activate:
                activation, activation_result = _activate(
                    out_root, release_id, operation="publish", reason=reason,
                    content_diff=diff, content_changed=changed,
                )
                activated = True
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    return PublishResult(
        release_id=release_id,
        release_dir=release_dir,
        release=release,
        activated=activated,
        changed=changed,
        diff=diff,
        activation=activation,
        activation_result=activation_result,
    )


_RELEASE_ID = __import__("re").compile(r"^rel-[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


def _restore_symlink(link: Path, old_target: str | None) -> None:
    if old_target is None:
        if link.is_symlink() or link.exists():
            link.unlink(missing_ok=True)
        return
    if link.is_symlink() and os.readlink(link) == old_target:
        return
    restore_temp = link.parent / f".restore-current-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        if restore_temp.exists() or restore_temp.is_symlink():
            restore_temp.unlink(missing_ok=True)
        os.symlink(old_target, restore_temp)
        os.replace(restore_temp, link)
    except OSError:
        restore_temp.unlink(missing_ok=True)
        link.unlink(missing_ok=True)
        os.symlink(old_target, link)


def _restore_file_bytes(path: Path, old_bytes: bytes | None, *, atomic_only: bool = False) -> None:
    for leftover in path.parent.glob(f".{path.name}.tmp-*"):
        leftover.unlink(missing_ok=True)
    if old_bytes is None:
        if path.is_file() or path.exists():
            path.unlink(missing_ok=True)
        return
    if path.is_file() and path.read_bytes() == old_bytes:
        return
    restore_temp = path.with_name(f".{path.name}.restore-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        with restore_temp.open("wb") as handle:
            handle.write(old_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(restore_temp, path)
    except OSError:
        restore_temp.unlink(missing_ok=True)
        if atomic_only:
            raise
        path.write_bytes(old_bytes)


def _activate(
    out_root: Path, release_id: str, *, operation: str = "activate", reason: str = "",
    content_diff: dict[str, Any] | None = None, content_changed: bool | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    with _publish_lock(out_root):
        releases_dir = out_root / "published" / "releases"
        if not _RELEASE_ID.match(release_id or ""):
            raise PublishError(f"release_id 形态不合法：{release_id!r}（只接受 rel-... 形式）")
        release_dir = releases_dir / release_id
        if not release_dir.is_dir():
            raise PublishError(f"release 不存在：{release_id}")
        release = _validate_activation_target(release_dir)
        published_dir = out_root / "published"
        published_dir.mkdir(parents=True, exist_ok=True)
        guarded_paths = [published_dir / "activations", published_dir / "activation-results"]
        if POINTER_BACKEND == "atomic_json":
            guarded_paths.append(published_dir / "current.json")
        for guarded in guarded_paths:
            require_plain_path(guarded)
        pointer = published_dir / "current.json"
        link = published_dir / "current"
        if POINTER_BACKEND == "atomic_json":
            if link.exists() or _is_link(link):
                raise PublishError("Windows current 旧软链/目录布局不受支持；请显式迁移后 activate")
            if _is_link(pointer) or (pointer.exists() and not pointer.is_file()):
                raise PublishError("Windows current.json 必须是普通文件，拒绝跟随链接")
        release_pointer = release_dir / ".current.json"
        temp_link = published_dir / f".current-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        temp_pointer_link = published_dir / f".current-json-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        pointer_rel_target = os.path.join("current", ".current.json")

        old_release_pointer_bytes = (
            release_pointer.read_bytes() if release_pointer.is_file() else None
        )
        old_link_target = os.readlink(link) if link.is_symlink() else None
        old_pointer_is_symlink = pointer.is_symlink()
        old_pointer_link_target = os.readlink(pointer) if old_pointer_is_symlink else None
        old_pointer_bytes = (
            pointer.read_bytes()
            if (not old_pointer_is_symlink and pointer.is_file())
            else None
        )
        pointer_payload = {
            "release_id": release_id,
            "release_fingerprint": release.get("release_fingerprint"),
            "business_fingerprint": release.get("business_fingerprint"),
            "review_status": release.get("review_status"),
            "activated_at": utc_now(),
        }
        # 显式 activate 仍可修复遗留双指针不一致：前驱以权威 current 软链为准。
        # publish 的前置门禁继续严格拒绝不一致，不因该恢复入口放宽。
        previous_source = "current"
        previous_note = ""
        if operation == "activate":
            if POINTER_BACKEND == "atomic_json":
                previous = None
                if pointer.is_file():
                    try:
                        previous_meta = _read_release_json(pointer)
                        previous_id = visible_text(previous_meta.get("release_id"))
                        if _RELEASE_ID.fullmatch(previous_id) and (releases_dir / previous_id).is_dir():
                            previous = releases_dir / previous_id
                            previous_source = ("current.json" if previous_meta.get("schema_version")
                                               == CURRENT_SCHEMA_VERSION else "legacy_current_json")
                        else:
                            previous_note = "current.json 缺少存在的合法 release_id"
                    except PublishError as exc:
                        previous_note = f"current.json 不可读：{exc}"
            elif link.is_symlink():
                previous_id = Path(os.readlink(link)).name
                previous = releases_dir / previous_id
            else:
                # 显式 activate 也承担修复遗留指针的职责。current 软链缺失时，
                # 若 current.json 是普通 JSON 且指向存在版本，仍把它记为真实前驱，
                # 不能把“修复指针”写成“无前驱”。
                legacy_pointer = published_dir / "current.json"
                previous = None
                if legacy_pointer.is_file() and not legacy_pointer.is_symlink():
                    try:
                        legacy_payload = _read_release_json(legacy_pointer)
                        legacy_id = visible_text(legacy_payload.get("release_id"))
                        if legacy_id and _RELEASE_ID.fullmatch(legacy_id):
                            candidate = releases_dir / legacy_id
                            if candidate.is_dir():
                                previous = candidate
                                previous_source = "legacy_current_json"
                            else:
                                previous_note = f"遗留 current.json 指向不存在的版本：{legacy_id}"
                        else:
                            previous_note = "遗留 current.json 缺少合法 release_id"
                    except PublishError as exc:
                        previous_note = f"遗留 current.json 不可读：{exc}"
                if previous is None and not previous_note:
                    previous_note = "current 软链缺失，按无前驱处理"
        else:
            previous = _current_release_dir(out_root)
            previous_source = "current.json" if POINTER_BACKEND == "atomic_json" else "current"
        diff_status = "computed"
        diff_error = ""
        if content_diff is None:
            if previous is None:
                content_diff = {}
                content_changed = False
                diff_status = "no_previous"
            else:
                try:
                    # 差异必须基于可信的前驱发布物；否则 SHA 不匹配但 JSON 仍可解析的
                    # 坏文件会被当作正常内容算出 “computed” 差异。前驱不完整时降级。
                    _validate_activation_target(previous)
                    content_diff = diff_snapshots(previous, release_dir)
                    content_changed = bool(content_diff.get("changed"))
                except (PublishError, OSError, ValueError, UnicodeError, KeyError, TypeError, IndexError, AttributeError) as exc:
                    # 显式回滚的语义是“切到已校验的目标版本”，不能因为当前版本自身损坏就
                    # 让操作员无路可退；差异算不出来就记录原因，切换仍按目标完整性放行。
                    content_diff = {}
                    content_changed = None
                    diff_status = "unavailable"
                    diff_error = f"{type(exc).__name__}: {exc}"
        elif previous is None and not content_diff and content_changed is False:
            diff_status = "no_previous"
        if content_changed is None and diff_status == "computed":
            content_changed = bool(content_diff.get("changed"))
        # 目标校验与差异计算之间可能被外部改动；切换前再核一次目标完整性，
        # 防止把已损坏的目标切为当前版本。差异不可算只降级差异，不降低目标要求。
        _validate_activation_target(release_dir)
        event_id = f"activation-{uuid.uuid4().hex}"
        activation = {
            "schema_version": "p23.activation.1",
            "event_id": event_id,
            "operation": operation,
            "release_id": release_id,
            "release_fingerprint": release.get("release_fingerprint"),
            "previous_release": previous.name if previous else None,
            "reason": reason,
            "content_changed": content_changed,
            "content_diff": content_diff,
            "content_diff_status": diff_status,
            "previous_source": previous_source,
            "requested_at": utc_now(),
        }
        if diff_error:
            activation["content_diff_error"] = diff_error
        if previous_note:
            activation["previous_note"] = previous_note
        # 意图先追加落盘，结果另存；强制退出时无结果的事件明确表示结果尚未确认。
        try:
            write_json_atomic(published_dir / "activations" / f"{event_id}.json", activation)
        except OSError as exc:
            raise PublishError(f"激活意图无法落盘，未切换 current（{event_id}）") from exc
        try:
            # 已存在 release 中的元数据也不可改写：激活时刻属于事件，不属于 release。
            if not release_pointer.is_file():
                write_json_atomic(release_pointer, pointer_payload)
            if POINTER_BACKEND == "atomic_json":
                # Windows has one ordinary-file commit point; immutable release paths
                # replace POSIX's derived current/ and current.json symlink paths.
                _activation_checkpoint("prepared")
                _write_current_pointer(pointer, pointer_payload)
            # 2. 确保 published/current.json 是指向 current/.current.json 的派生软链。
            elif not (old_pointer_is_symlink and old_pointer_link_target == pointer_rel_target):
                if temp_pointer_link.exists() or temp_pointer_link.is_symlink():
                    temp_pointer_link.unlink(missing_ok=True)
                os.symlink(pointer_rel_target, temp_pointer_link)
                os.replace(temp_pointer_link, pointer)
            # 3. 单一权威提交点：原子替换 published/current → releases/<release_id>。
            #    切换瞬间 current 与 current.json 同步指向新版本，不存在双版本窗口。
            if POINTER_BACKEND != "atomic_json":
                if temp_link.exists() or temp_link.is_symlink():
                    temp_link.unlink(missing_ok=True)
                os.symlink(os.path.join("releases", release_id), temp_link)
                _activation_checkpoint("prepared")
                os.replace(temp_link, link)
        except BaseException as exc:
            temp_link.unlink(missing_ok=True)
            temp_pointer_link.unlink(missing_ok=True)
            _restore_file_bytes(release_pointer, old_release_pointer_bytes)
            _restore_symlink(link, old_link_target)
            if old_pointer_is_symlink:
                _restore_symlink(pointer, old_pointer_link_target)
            elif old_pointer_bytes is not None:
                if pointer.is_symlink():
                    pointer.unlink(missing_ok=True)
                _restore_file_bytes(pointer, old_pointer_bytes,
                                    atomic_only=POINTER_BACKEND == "atomic_json")
            elif old_link_target is None:
                pointer.unlink(missing_ok=True)
            try:
                write_json_atomic(
                    published_dir / "activation-results" / f"{event_id}.json",
                    {"schema_version": "p23.activation-result.1", "event_id": event_id,
                     "status": "failed", "finished_at": utc_now(), "error_type": type(exc).__name__},
                )
            except OSError as audit_exc:
                raise PublishError(
                    f"激活失败且结果审计无法落盘，current 已恢复；请核查事件 {event_id}"
                ) from audit_exc
            if isinstance(exc, OSError) and not isinstance(exc, PublishError):
                raise PublishError(f"激活 release 失败（{release_id}）：{exc}") from exc
            raise
        activation_result = {
            "schema_version": "p23.activation-result.1", "event_id": event_id,
            "status": "committed", "finished_at": utc_now(),
        }
        try:
            # The pointer has committed. Failures from this point must never
            # restore a version that readers may already have observed.
            _activation_checkpoint("committed")
            write_json_atomic(
                published_dir / "activation-results" / f"{event_id}.json", activation_result,
            )
        except OSError as exc:
            # 唯一提交点已成功，不能谎报“激活失败”或回滚已经对外可见的提交。
            # 返回真实提交结果及显式审计缺口；意图已持久化，可按 event_id 和 current 核查。
            activation_result["audit_warning"] = (
                f"current 已提交，但激活结果文件写入失败（{type(exc).__name__}）；"
                f"请按事件 {event_id} 核查指针，勿把缺少结果文件视为未激活"
            )
        return activation, activation_result


def activate_release(out_root: str | Path, release_id: str) -> dict[str, Any]:
    """显式回到（或切到）某个已发布版本；只改指针，不动数据文件。"""

    root = Path(out_root)
    with _publish_lock(root):
        event, result = _activate(root, release_id)
        current = dict(_read_release_json(
            root / "published" / "releases" / release_id / ".current.json"
        ))
        current["activation_event_id"] = event["event_id"]
        if event.get("content_diff_status") == "unavailable":
            current["content_diff_warning"] = event.get("content_diff_error", "")
        if result.get("audit_warning"):
            current["audit_warning"] = result["audit_warning"]
        return current


def list_releases(out_root: str | Path) -> list[dict[str, Any]]:
    root = Path(out_root)
    releases_dir = root / "published" / "releases"
    if not releases_dir.is_dir():
        return []
    current = _active_release_id(root)
    items: list[dict[str, Any]] = []
    for entry in sorted(releases_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        release_file = entry / "release.json"
        if not release_file.is_file():
            continue
        release = read_json(release_file)
        items.append(
            {
                "release_id": release.get("release_id"),
                "snapshot_id": release.get("snapshot_id"),
                "review_status": release.get("review_status"),
                "release_fingerprint": release.get("release_fingerprint"),
                "published_at": release.get("published_at"),
                "active": release.get("release_id") == current,
                "datasets": sorted((release.get("files") or {}).keys()),
            }
        )
    return items


def dataset_paths(out_root: str | Path, *, release_id: str | None = None) -> dict[str, str]:
    """返回各数据类别在当前（或指定）release 下的文件路径，供环境变量使用。"""

    root = Path(out_root)
    if release_id is None:
        release_id = _active_release_id(root)
        if not release_id:
            raise PublishError("还没有发布任何 release：先 publish")
    if not _RELEASE_ID.fullmatch(release_id):
        raise PublishError("release_id 形态不合法")
    release_dir = root / "published" / "releases" / release_id
    release = _validate_activation_target(release_dir)
    return {
        name: str(release_dir / info["file"])
        for name, info in (release.get("files") or {}).items()
    }


__all__ = [
    "PublishError",
    "PublishRequest",
    "PublishResult",
    "ReviewRecord",
    "activate_release",
    "current_release_dir",
    "published_release_dir",
    "require_plain_path",
    "dataset_paths",
    "diff_snapshots",
    "list_releases",
    "load_review",
    "publish_release",
]
