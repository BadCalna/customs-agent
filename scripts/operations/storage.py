"""Container/native file operations. No shell interpolation; fail closed on links/escapes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile


def canonical(value):
    path = Path(value)
    if not path.is_absolute() or any(ord(c) < 32 or c in "\"'`" for c in str(value)):
        raise ValueError("file paths must be absolute and contain no control characters or quotes")
    return path.resolve()


def related(a, b):
    return a == b or a in b.parents or b in a.parents


def validate(root, target=None, base=None):
    root = canonical(root)
    if base is not None and canonical(base) not in root.parents:
        raise ValueError("实际文件根不在持久卷/隔离边界内")
    if not root.is_dir() or root == Path("/"):
        raise ValueError(f"文件根目录不存在或不安全: {root}")
    if target is None:
        return root, None
    original_target = Path(target)
    target = canonical(target)
    base = canonical(base or root.parent)
    if target == base or base not in target.parents:
        raise ValueError("恢复目录在持久卷/隔离边界外")
    if related(root, target):
        raise ValueError("恢复目录与生产原件目录相同或有祖先/后代关系")
    # Refuse even symlinks which happen to resolve to an otherwise safe target.
    cursor = Path(target.anchor)
    for part in original_target.parts[1:]:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError("恢复路径不能包含符号链接")
    return root, target


def identity(path):
    stat = path.stat()
    return {"path": str(path.resolve()), "device": stat.st_dev, "inode": stat.st_ino}


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=("probe", "archive", "extract", "remove"))
    p.add_argument("--root", required=True)
    p.add_argument("--target")
    p.add_argument("--base")
    p.add_argument("--identity")
    p.add_argument("--keep-failed-target", action="store_true")
    args = p.parse_args(argv)
    root, target = validate(args.root, args.target, args.base)
    if args.action == "probe":
        print(json.dumps({"root": str(root), "target": str(target) if target else None,
                          "exists": target.exists() if target else True}))
    elif args.action == "archive":
        with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as archive:
            for path in sorted(root.rglob("*")):
                relative = path.relative_to(root)
                if "incoming" in relative.parts:
                    continue
                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                    raise ValueError(f"不支持的文件类型: {path}")
                archive.add(path, arcname=str(relative), recursive=False)
    elif args.action == "extract":
        if target is None:
            raise ValueError("missing target")
        # Exclusive mkdir: an existing empty directory is NOT ours to write/delete.
        target.mkdir(mode=0o700, parents=False, exist_ok=False)
        owned = identity(target)
        try:
            with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as archive:
                for member in archive:
                    relative = Path(member.name)
                    if relative == Path(".") and member.isdir():
                        continue  # compatibility with older tar -C root . archives
                    if relative.is_absolute() or ".." in relative.parts or not (member.isdir() or member.isfile()):
                        raise ValueError(f"不安全的 tar 成员: {member.name}")
                    output = target / relative
                    if member.isdir():
                        output.mkdir(mode=0o700, parents=True, exist_ok=True)
                    else:
                        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        with output.open("xb") as handle, archive.extractfile(member) as source:
                            shutil.copyfileobj(source, handle)
            print(json.dumps(owned))
        except BaseException:
            if not args.keep_failed_target and target.exists() and identity(target) == owned:
                shutil.rmtree(target)
            raise
    else:
        if target is None or not args.identity or identity(target) != json.loads(args.identity):
            raise ValueError("拒绝清理：目标不再是本次创建的目录")
        shutil.rmtree(target)


if __name__ == "__main__":
    main()
