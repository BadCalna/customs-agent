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
- 旧 release 目录不删除，``current`` 指针与 ``current.json`` 决定当前使用哪一个，
  可随时 ``activate`` 回到之前的版本。

**不修改 ``.env``，不切换运行环境的数据来源**：这里只产出目录与指针，
环境变量由使用方自己指过来（D6 第 2.7 节）。
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Iterable, Mapping

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
    scope_raw = payload.get("scope") or []
    if not isinstance(scope_raw, list):
        raise PublishError("复核文件的 scope 必须是数组")
    scope = tuple(visible_text(item) for item in scope_raw if visible_text(item))
    unknown = [
        item
        for item in scope
        if item not in {dataset.value for dataset in DatasetId}
    ]
    if unknown:
        raise PublishError("复核文件的 scope 含未知数据类别：" + "、".join(unknown))
    confirmations_raw = payload.get("confirmations") or {}
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
        left_payload = read_json(left_path)
        right_payload = read_json(right_path)
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


def _verification_for(out_root: Path, snapshot_id: str) -> dict[str, Any] | None:
    path = out_root / "verification" / f"{snapshot_id}.json"
    if not path.is_file():
        return None
    return read_json(path)


_GLOBAL_LOCK = threading.Lock()
_THREAD_LOCKS: dict[Path, threading.RLock] = {}
_LOCK_DEPTH: dict[tuple[int, Path], int] = {}


@contextmanager
def _publish_lock(out_root: Path) -> Iterator[None]:
    """跨线程 + 跨进程发布排他锁；同线程重入不自锁。"""

    key = out_root.resolve()
    ident = threading.get_ident()
    with _GLOBAL_LOCK:
        rlock = _THREAD_LOCKS.setdefault(key, threading.RLock())
    rlock.acquire()
    try:
        with _GLOBAL_LOCK:
            depth = _LOCK_DEPTH.get((ident, key), 0)
            _LOCK_DEPTH[(ident, key)] = depth + 1
        lock_handle = None
        if depth == 0:
            published_dir = out_root / "published"
            published_dir.mkdir(parents=True, exist_ok=True)
            lock_path = published_dir / ".publish.lock"
            lock_handle = lock_path.open("a+", encoding="utf-8")
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if lock_handle is not None:
                try:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                finally:
                    lock_handle.close()
            with _GLOBAL_LOCK:
                remaining = _LOCK_DEPTH.get((ident, key), 1) - 1
                if remaining <= 0:
                    _LOCK_DEPTH.pop((ident, key), None)
                else:
                    _LOCK_DEPTH[(ident, key)] = remaining
    finally:
        rlock.release()


def _active_release_id(out_root: Path) -> str | None:
    """读取当前激活的 release_id；若 current.json 与 current 软链都存在但指向不同版本，报指针不一致。"""

    published_dir = out_root / "published"
    pointer = published_dir / "current.json"
    link = published_dir / "current"
    pointer_id: str | None = None
    if pointer.is_file():
        payload = read_json(pointer)
        if isinstance(payload, Mapping):
            pointer_id = visible_text(payload.get("release_id")) or None
    link_id: str | None = None
    if link.is_symlink():
        link_id = Path(os.readlink(link)).name or None
    if pointer_id and link_id and pointer_id != link_id:
        raise PublishError(
            f"当前版本指针不一致：current.json 指向 {pointer_id}，"
            f"current 软链指向 {link_id}；请先用 activate 显式指定版本"
        )
    return pointer_id or link_id


def _current_release_dir(out_root: Path) -> Path | None:
    release_id = _active_release_id(out_root)
    if not release_id:
        return None
    release_dir = out_root / "published" / "releases" / release_id
    return release_dir if release_dir.is_dir() else None


def _check_content_change(
    previous: Path | None,
    candidate_dir: Path,
    *,
    allow_content_change: bool,
    reason: str,
) -> tuple[dict[str, Any], bool]:
    diff: dict[str, Any] = {}
    changed = False
    if previous is not None:
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
    if not verification.get("all_passed", False):
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
    release_dir = releases_dir / release_id
    temp_dir = releases_dir / f".tmp-{release_id}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    created_release_dir = False
    activated = False
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
        observed_previous = _current_release_dir(out_root)
        diff, changed = _check_content_change(
            observed_previous,
            temp_dir,
            allow_content_change=request.allow_content_change,
            reason=reason,
        )
        with _publish_lock(out_root):
            previous = _current_release_dir(out_root)
            if previous != observed_previous:
                diff, changed = _check_content_change(
                    previous,
                    temp_dir,
                    allow_content_change=request.allow_content_change,
                    reason=reason,
                )
            while True:
                after_diff_previous = _current_release_dir(out_root)
                if after_diff_previous == previous:
                    break
                previous = after_diff_previous
                diff, changed = _check_content_change(
                    previous,
                    temp_dir,
                    allow_content_change=request.allow_content_change,
                    reason=reason,
                )
            previous_release_id = previous.name if previous is not None else None
            if previous_release_id == release_id and release_dir.is_dir():
                existing_meta = read_json(release_dir / "release.json")
                previous_release_id = existing_meta.get("previous_release")
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
            if release_dir.exists():
                shutil.rmtree(temp_dir)
                existing_release = read_json(release_dir / "release.json")
                if (
                    existing_release.get("previous_release") != previous_release_id
                    or existing_release.get("content_changed") != changed
                    or (reason and existing_release.get("reason") != reason)
                ):
                    write_json_atomic(release_dir / "release.json", release)
                else:
                    release = existing_release
            else:
                try:
                    os.replace(temp_dir, release_dir)
                    created_release_dir = True
                except OSError:
                    # 并发发布同一 release：谁先到谁生效，另一方读现成的，不抛裸 OSError。
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    if not release_dir.is_dir():
                        raise PublishError(
                            f"发布目录落盘失败，且目标不存在：{release_dir.name}"
                        )
                    release = read_json(release_dir / "release.json")
            if request.activate:
                latest_previous = _current_release_dir(out_root)
                if latest_previous != previous and (
                    latest_previous is None or latest_previous.name != release_id
                ):
                    try:
                        diff, changed = _check_content_change(
                            latest_previous,
                            release_dir,
                            allow_content_change=request.allow_content_change,
                            reason=reason,
                        )
                    except Exception:
                        if created_release_dir:
                            shutil.rmtree(release_dir, ignore_errors=True)
                        raise
                    previous = latest_previous
                    release["previous_release"] = (
                        previous.name if previous is not None else None
                    )
                    release["content_changed"] = changed
                    release["content_diff"] = diff
                    write_json_atomic(release_dir / "release.json", release)
                _activate(out_root, release_id)
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


def _restore_file_bytes(path: Path, old_bytes: bytes | None) -> None:
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
        restore_temp.write_bytes(old_bytes)
        os.replace(restore_temp, path)
    except OSError:
        restore_temp.unlink(missing_ok=True)
        path.write_bytes(old_bytes)


def _activate(out_root: Path, release_id: str) -> None:
    with _publish_lock(out_root):
        releases_dir = out_root / "published" / "releases"
        if not _RELEASE_ID.match(release_id or ""):
            raise PublishError(f"release_id 形态不合法：{release_id!r}（只接受 rel-... 形式）")
        release_dir = releases_dir / release_id
        if not release_dir.is_dir():
            raise PublishError(f"release 不存在：{release_id}")
        release = read_json(release_dir / "release.json")
        published_dir = out_root / "published"
        published_dir.mkdir(parents=True, exist_ok=True)
        pointer = published_dir / "current.json"
        link = published_dir / "current"
        temp_link = published_dir / f".current-{os.getpid()}-{uuid.uuid4().hex[:8]}"

        old_pointer_bytes = pointer.read_bytes() if pointer.is_file() else None
        old_link_target = os.readlink(link) if link.is_symlink() else None
        pointer_payload = {
            "release_id": release_id,
            "release_fingerprint": release.get("release_fingerprint"),
            "business_fingerprint": release.get("business_fingerprint"),
            "review_status": release.get("review_status"),
            "activated_at": utc_now(),
        }
        try:
            # 先原子切换 current 软链，再原子写 current.json；任一步失败均回滚两端，
            # 防止中断后出现 current.json 与 current 软链指向不同版本。
            if temp_link.exists() or temp_link.is_symlink():
                temp_link.unlink()
            os.symlink(os.path.join("releases", release_id), temp_link)
            os.replace(temp_link, link)
            write_json_atomic(pointer, pointer_payload)
        except BaseException:
            if temp_link.exists() or temp_link.is_symlink():
                temp_link.unlink(missing_ok=True)
            _restore_symlink(link, old_link_target)
            _restore_file_bytes(pointer, old_pointer_bytes)
            raise


def activate_release(out_root: str | Path, release_id: str) -> dict[str, Any]:
    """显式回到（或切到）某个已发布版本；只改指针，不动数据文件。"""

    root = Path(out_root)
    _activate(root, release_id)
    return read_json(root / "published" / "current.json")


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
    release_dir = root / "published" / "releases" / release_id
    release = read_json(release_dir / "release.json")
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
    "dataset_paths",
    "diff_snapshots",
    "list_releases",
    "load_review",
    "publish_release",
]
