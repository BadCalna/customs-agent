"""Fail-closed counts schema validation and backup/restore comparison.

Database names deliberately differ. Target identity is checked by operations.py,
not by comparing production and restore database names.
"""
from __future__ import annotations
import json
from pathlib import Path, PurePosixPath
import re
import sys


def validate(payload):
    if not isinstance(payload, dict):
        raise ValueError("counts 必须是 JSON object")
    for key in ("db_vendor", "db_name", "file_root"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise ValueError(f"缺少/无效字段: {key}")
    if not Path(payload["file_root"]).is_absolute():
        raise ValueError("file_root 必须绝对路径")
    tables = payload.get("tables")
    if not isinstance(tables, dict) or not tables:
        raise ValueError("缺少/无效 tables")
    if any(not isinstance(k, str) or not k or type(v) is not int or v < 0 for k, v in tables.items()):
        raise ValueError("无效表行数")
    hashes = payload.get("table_sha256")
    if hashes is not None and (not isinstance(hashes, dict) or set(hashes) != set(tables)
                              or any(not digest(v) for v in hashes.values())):
        raise ValueError("无效 table_sha256")
    files = payload.get("files")
    if not isinstance(files, list):
        raise ValueError("缺少/无效 files")
    seen = set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise ValueError("无效文件条目")
        path = item["path"]
        if not isinstance(path, str) or not path or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts or path in seen:
            raise ValueError("无效/重复文件路径")
        seen.add(path)
        if type(item["size"]) is not int or item["size"] < 0 or not digest(item["sha256"]):
            raise ValueError("无效文件大小/哈希")
    if type(payload.get("file_count")) is not int or payload["file_count"] != len(files):
        raise ValueError("file_count 不匹配")
    if type(payload.get("file_bytes")) is not int or payload["file_bytes"] != sum(f["size"] for f in files):
        raise ValueError("file_bytes 不匹配")
    identity = payload.get("file_identity")
    if identity is not None and (not isinstance(identity, dict) or identity.get("path") != payload["file_root"]
                                 or any(type(identity.get(k)) is not int for k in ("device", "inode"))):
        raise ValueError("无效 file_identity")
    return payload


def digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def load(path):
    return validate(json.loads(Path(path).read_text(encoding="utf-8")))


def compare(expected, actual):
    validate(expected)
    validate(actual)
    problems = []
    if expected["db_vendor"] != actual["db_vendor"]:
        problems.append("数据库后端不同")
    missing = set(expected["tables"]) - set(actual["tables"])
    extra = set(actual["tables"]) - set(expected["tables"])
    if missing:
        problems.append(f"恢复库缺少表: {sorted(missing)}")
    if extra:
        problems.append(f"恢复库多出表: {sorted(extra)}")
    for name in sorted(set(expected["tables"]) & set(actual["tables"])):
        if expected["tables"][name] != actual["tables"][name]:
            problems.append(f"表 {name} 行数不同")
    if "table_sha256" in expected:
        if expected["table_sha256"] != actual.get("table_sha256"):
            problems.append("数据库逐行内容指纹不同")
    else:
        print("警告：此旧备份仅验证数据库行数，不证明字段内容完整一致")
    exp_files = {f["path"]: f for f in expected["files"]}
    act_files = {f["path"]: f for f in actual["files"]}
    if exp_files != act_files:
        problems.append("文件内容不同（路径、大小或 SHA256 不一致）")
    return problems


def main(argv):
    if len(argv) != 2:
        print("用法: compare_counts.py <expected> <actual>", file=sys.stderr)
        return 2
    try:
        expected, actual = map(load, argv)
        problems = compare(expected, actual)
    except (ValueError, TypeError, OSError) as exc:
        print(f"无效清单: {exc}", file=sys.stderr)
        return 2
    for problem in problems:
        print(problem)
    print("结论：不一致" if problems else "结论：一致（表行数/可用内容指纹与文件哈希）")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
