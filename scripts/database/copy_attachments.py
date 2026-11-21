"""Copy the cutover's referenced files between explicit roots, without overwrites.

The source manifest is captured against the upgraded SQLite snapshot. The source
and target deployments must stay stopped for the whole cutover. Unreferenced
exports/incoming files are outside this migration. Unfinished imports are rejected
before capturing the manifest; complete P22 backups also exclude incoming and
reject unfinished imports under their consistency boundary.
"""
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from adapters.files.storage import resolve_path  # noqa: E402


def checked_manifest(manifest):
    if not isinstance(manifest, list):
        raise ValueError("附件清单必须为数组")
    seen = set()
    for entry in manifest:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise ValueError("附件清单字段无效")
        path = entry["path"]
        if not isinstance(path, str) or PurePosixPath(path).as_posix() != path or path in seen:
            raise ValueError("附件清单路径不规范或重复")
        # resolve_path also rejects absolute paths, traversal and backslashes.
        resolve_path(REPO_ROOT, path)
        if type(entry["size"]) is not int or entry["size"] < 0:
            raise ValueError("附件清单大小无效")
        if not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            raise ValueError("附件清单摘要无效")
        seen.add(path)
    return manifest


def file_fingerprint(root, relative):
    candidate = resolve_path(root, relative)
    if not candidate.is_file():
        raise ValueError(f"附件不是可读文件：{relative}")
    digest, size = hashlib.sha256(), 0
    with candidate.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    return {"path": relative, "size": size, "sha256": digest.hexdigest()}


def verify_manifest(root, manifest):
    checked_manifest(manifest)
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("附件根必须为已有目录")
    for entry in manifest:
        if file_fingerprint(root, entry["path"]) != entry:
            raise ValueError(f"附件与迁移前指纹不一致：{root}: {entry['path']}")
    return root


def _target_path(root, relative):
    # Refuse even in-root symlinks: publishing must use the exact recorded path.
    candidate = root
    for part in PurePosixPath(relative).parts:
        candidate = candidate / part
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            metadata = None
        # Windows directory junctions are reparse points but is_symlink() is
        # false. Publishing must reject those redirects just like POSIX links,
        # including junctions whose target happens to stay inside the root.
        reparse = metadata is not None and bool(
            getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        if candidate.is_symlink() or reparse:
            raise ValueError(f"目标附件路径不允许符号链接：{relative}")
    return resolve_path(root, relative)


def copy_attachments(source_root, target_root, manifest):
    source = verify_manifest(source_root, manifest)
    target = Path(target_root).resolve()
    if source != target and (source in target.parents or target in source.parents):
        raise ValueError("源/目标附件根不能互相嵌套")
    if target.exists() and not target.is_dir():
        raise ValueError("目标附件根不是目录")
    # Check ALL collisions before publishing any file or touching the database.
    for entry in manifest:
        dest = _target_path(target, entry["path"])
        if dest.exists() and file_fingerprint(target, entry["path"]) != entry:
            raise ValueError(f"目标附件已存在且内容不同，拒绝覆盖：{entry['path']}")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    copied, reused = 0, 0
    for entry in manifest:
        relative = entry["path"]
        dest = _target_path(target, relative)
        if dest.exists():
            reused += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        dest = _target_path(target, relative)
        descriptor, temporary = tempfile.mkstemp(prefix=".p12-copy-", dir=dest.parent)
        temporary = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as output, resolve_path(source, relative).open("rb") as input_file:
                shutil.copyfileobj(input_file, output)
                output.flush()
                os.fsync(output.fileno())
            if file_fingerprint(temporary.parent, temporary.name) != dict(entry, path=temporary.name):
                raise ValueError(f"复制中附件变化：{relative}")
            try:
                os.link(temporary, dest)  # Atomic publication; never truncate existing data.
                copied += 1
            except FileExistsError:
                if file_fingerprint(target, relative) != entry:
                    raise ValueError(f"目标附件并发出现且内容不同：{relative}")
                reused += 1
        finally:
            if temporary.resolve().parent != dest.parent.resolve() or not temporary.name.startswith(".p12-copy-"):
                raise ValueError("临时附件清理路径异常")
            temporary.unlink()
    verify_manifest(source, manifest)
    verify_manifest(target, manifest)
    return {"source_files_root": str(source), "target_files_root": str(target),
            "copied": copied, "reused": reused, "referenced_files": len(manifest)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-files-root", required=True)
    parser.add_argument("--target-files-root", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--json", required=True)
    args = parser.parse_args()
    result = copy_attachments(args.source_files_root, args.target_files_root,
                              json.loads(Path(args.manifest).read_text(encoding="utf-8")))
    with Path(args.json).open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False))
