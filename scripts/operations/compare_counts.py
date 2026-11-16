"""比较两个 table_counts.py 输出，判断恢复后的库/文件是否与备份一致（P22）。

只比较“应该一致”的部分：
- 表行数：备份时有的表必须存在且行数相同；恢复库里多出来的表（例如迁移新加的）会单独提示，不算失败。
- 文件清单：相对路径 + 字节数 + sha256 必须完全一致（受控存储是内容寻址，同名同内容才算还原成功）。

用法::

    python scripts/operations/compare_counts.py 备份/counts.json 恢复/counts.json

退出码：0 一致，1 有差异（差异明细写 stdout）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法：compare_counts.py <备份 counts.json> <恢复 counts.json>", file=sys.stderr)
        return 2

    expected = load(argv[0])
    actual = load(argv[1])
    problems: list[str] = []

    print(f"备份：{expected.get('generated_at')}  库={expected.get('db_name')}  后端={expected.get('db_vendor')}")
    print(f"恢复：{actual.get('generated_at')}  库={actual.get('db_name')}  后端={actual.get('db_vendor')}")
    if expected.get("db_vendor") != actual.get("db_vendor"):
        problems.append(f"数据库后端不同：{expected.get('db_vendor')} → {actual.get('db_vendor')}")

    exp_tables = expected.get("tables", {})
    act_tables = actual.get("tables", {})
    missing = sorted(set(exp_tables) - set(act_tables))
    extra = sorted(set(act_tables) - set(exp_tables))
    changed = sorted(
        name for name in set(exp_tables) & set(act_tables) if exp_tables[name] != act_tables[name]
    )

    print(f"\n表：期望 {len(exp_tables)} 张，实际 {len(act_tables)} 张")
    if missing:
        problems.append(f"恢复库缺少表：{', '.join(missing)}")
    if changed:
        for name in changed:
            problems.append(f"表 {name} 行数不同：{exp_tables[name]} → {act_tables[name]}")
    if extra:
        print(f"提示：恢复库多出 {len(extra)} 张表（可能是更新的迁移引入，不算失败）：{', '.join(extra)}")

    if "files" in expected:
        exp_files = {item["path"]: item for item in expected.get("files", [])}
        act_files = {item["path"]: item for item in actual.get("files", [])}
        missing_files = sorted(set(exp_files) - set(act_files))
        extra_files = sorted(set(act_files) - set(exp_files))
        diff_files = sorted(
            path
            for path in set(exp_files) & set(act_files)
            if exp_files[path]["sha256"] != act_files[path]["sha256"]
        )
        print(
            f"\n文件：期望 {len(exp_files)} 个（{expected.get('file_bytes', 0)} 字节），"
            f"实际 {len(act_files)} 个（{actual.get('file_bytes', 0)} 字节）"
        )
        if missing_files:
            problems.append(f"恢复后缺少文件（{len(missing_files)}）：{', '.join(missing_files[:10])}")
        if diff_files:
            problems.append(f"文件内容不同（{len(diff_files)}）：{', '.join(diff_files[:10])}")
        if extra_files:
            print(f"提示：恢复后多出 {len(extra_files)} 个文件：{', '.join(extra_files[:10])}")

    if problems:
        print("\n结论：不一致")
        for item in problems:
            print(f"  - {item}")
        return 1

    print("\n结论：一致（表行数与文件哈希均与备份相符）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
