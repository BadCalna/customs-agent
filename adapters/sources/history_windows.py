"""Privilege-free Windows publication: one atomic JSON pointer, immutable generations.

No top-level data files are overwritten. Readers must pin the pointer once via
history_publish.pin_history_output/resolve_history_file; POSIX retains symlinks.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from adapters.sources import history_publish as hp

SCHEMA = "p15.current.1"


def _regular(path: Path) -> os.stat_result:
    info = path.lstat()
    if hp._is_redirect(info) or not stat.S_ISREG(info.st_mode):
        raise hp.PublishError(f"输出 {path.name} 不是普通文件（符号链接/reparse 被拒绝）")
    return info


def _generation_files(directory: Path) -> dict[str, str]:
    """Validate every component before reading, including directory reparse points."""
    found = {}
    for root, directories, files in os.walk(directory, followlinks=False):
        for name in directories:
            child = Path(root) / name
            if hp._is_redirect(child.lstat()):
                raise hp.PublishError("generation 包含符号链接/reparse 目录，拒绝读取")
        for name in files:
            child = Path(root) / name
            _regular(child)
            rel = child.relative_to(directory)
            hp._validated_rel(rel, origin=str(child))
            found[rel.as_posix()] = hashlib.sha256(child.read_bytes()).hexdigest()
    return found


def read_current(out_dir: Path) -> Path | None:
    pointer = out_dir / hp.CURRENT_POINTER_NAME
    if os.path.lexists(out_dir / hp.CURRENT_LINK_NAME):
        raise hp.PublishError("Windows 输出包含旧 .current 链接；请显式迁移，不能混合两种指针")
    if not os.path.lexists(pointer):
        return None
    try:
        _regular(pointer)
        if pointer.stat().st_size > 4 * 1024 * 1024:
            raise hp.PublishError("原子指针超出大小限制")
        payload = json.loads(pointer.read_bytes())
        if not isinstance(payload, dict) or set(payload) != {"schema", "generation", "files"}:
            raise hp.PublishError("原子指针结构无效")
        if payload["schema"] != SCHEMA:
            raise hp.PublishError("原子指针版本不受支持")
        name = payload["generation"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,160}", name):
            raise hp.PublishError("原子指针 generation 非法，拒绝越界路径")
        expected = payload["files"]
        if not isinstance(expected, dict) or not expected:
            raise hp.PublishError("原子指针没有完整文件摘要")
        for rel, digest in expected.items():
            if not isinstance(rel, str) or "\\" in rel or ":" in rel:
                raise hp.PublishError("原子指针文件路径非法")
            hp._validated_rel(Path(rel), origin=rel)
            if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
                raise hp.PublishError("原子指针文件摘要非法")
        snapshots = out_dir / hp.SNAPSHOTS_DIR_NAME
        for directory in (out_dir, snapshots, snapshots / name):
            info = directory.lstat()
            if hp._is_redirect(info) or not stat.S_ISDIR(info.st_mode):
                raise hp.PublishError("原子指针 generation 不是真实目录")
        directory = snapshots / name
        if _generation_files(directory) != expected:
            raise hp.PublishError("原子指针文件摘要不匹配，拒绝损坏/不完整快照")
        return directory
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, hp.PublishError):
            raise
        raise hp.PublishError(f"读取原子指针失败：{type(exc).__name__}；不回退旧文件") from exc


def commit_pointer(out_dir: Path, generation_dir: Path) -> None:
    payload = {"schema": SCHEMA, "generation": generation_dir.name,
               "files": _generation_files(generation_dir)}
    if not payload["files"]:
        raise hp.PublishError("不能发布空 generation")
    temporary = out_dir / hp.SNAPSHOTS_DIR_NAME / f".current-{hp._new_token()}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # The only commit point. A killed writer exposes the complete old or new generation.
        os.replace(temporary, out_dir / hp.CURRENT_POINTER_NAME)
    except OSError as exc:
        raise hp.PublishError(f"提交原子指针失败：{exc}；旧输出未改动") from exc
    finally:
        hp._unlink_quietly(temporary)


def _legacy(out_dir: Path, candidate_names: set[Path], current: Path | None) -> Path | None:
    old = []
    for rel in hp._sorted_rels(candidate_names):
        path = out_dir / rel
        # Reject redirected ancestors as well as redirected final components.
        hp._ensure_real_dir(out_dir, rel.parts[:-1])
        if os.path.lexists(path):
            _regular(path)
            old.append(rel)
    if current is not None or not old:
        return current
    generation = hp._install_legacy_generation(out_dir, old)
    hp._checkpoint("legacy-ready")
    directory = out_dir / hp.SNAPSHOTS_DIR_NAME / generation
    commit_pointer(out_dir, directory)
    hp._checkpoint("legacy-current-set")
    # Flat originals remain byte-for-byte intact; the pointer now pins their immutable copy.
    for index, _ in enumerate(old):
        hp._checkpoint(f"legacy-replace-{index}")
    return directory


def _possibly_committed(out_dir: Path, generation_dir: Path) -> bool:
    """An interrupt may arrive after replace but before its caller observes success."""
    try:
        payload = json.loads((out_dir / hp.CURRENT_POINTER_NAME).read_bytes())
        return not isinstance(payload, dict) or payload.get("generation") == generation_dir.name
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True  # Never delete immutable files when commit state is uncertain.


def publish(staging: Path, out_dir: Path, files, *, expected_records_sha256=None) -> Path:
    staging, out_dir = hp._absolute(Path(staging)), hp._absolute(Path(out_dir))
    if not staging.is_dir() or hp._is_redirect(staging.lstat()):
        raise hp.PublishError("暂存目录不是真实目录")
    snapshots, images = hp._collect_files(staging, files)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        info = out_dir.lstat()
        if hp._is_redirect(info) or not stat.S_ISDIR(info.st_mode):
            raise hp.PublishError("输出目录不是真实目录，拒绝符号链接/reparse")
    except OSError as exc:
        raise hp.PublishError(f"无法创建输出目录：{exc}") from exc
    generation_dir = None
    with hp._output_lock(out_dir):
        current = read_current(out_dir)
        # Validate legacy links before baseline checks, even when CAS is not requested.
        candidates = {Path(name) for name in hp.KNOWN_SNAPSHOT_NAMES} | set(snapshots)
        for rel in candidates:
            path = out_dir / rel
            if os.path.lexists(path):
                _regular(path)
        hp._check_records_baseline(out_dir, expected_records_sha256)
        hp._sweep_stale_temps(out_dir)
        try:
            hp._publish_images(out_dir, images)
            current = _legacy(out_dir, candidates, current)
            _, generation_dir = hp._build_generation(out_dir, snapshots, current)
            hp._checkpoint("generation-ready")
            hp._checkpoint("before-commit")
            commit_pointer(out_dir, generation_dir)
        except BaseException as exc:
            if generation_dir is not None and not _possibly_committed(out_dir, generation_dir):
                hp._rollback_publish([], generation_dir)
            if isinstance(exc, (hp.PublishError, KeyboardInterrupt, SystemExit)):
                raise
            raise hp.PublishError(f"原子发布失败：{type(exc).__name__}: {exc}；旧输出未改动") from exc
    hp._checkpoint("after-commit")
    return generation_dir
