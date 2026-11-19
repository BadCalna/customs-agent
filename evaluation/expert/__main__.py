"""CLI entry point for running P18 expert-check evaluation reproducibly."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence


def _assert_isolated_env() -> None:
    if os.environ.get("DJANGO_SETTINGS_MODULE") == "config.settings":
        raise RuntimeError(
            "评估禁止使用 config.settings（会读 .env 并触发真实 DB/LLM）；请用 evaluation.expert.settings"
        )
    if not os.environ.get("DJANGO_SECRET_KEY", "").strip():
        raise RuntimeError("请显式提供临时 DJANGO_SECRET_KEY（无需读取项目 .env）")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "evaluation.expert.settings")


def main(argv: Sequence[str] | None = None) -> int:
    _assert_isolated_env()
    from .runner import evaluate_cases, evaluate_gold_suite

    parser = argparse.ArgumentParser(description="运行 P18 专家 AI 核查离线评估并生成混淆矩阵报告")
    parser.add_argument(
        "--gold-suite",
        type=Path,
        default=None,
        help="可选：Jay/关务专家 D-11 金标准 JSON 文件路径（校验 gold_standard=true 且 synthetic=false）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="可选：输出报告 JSON 路径；不填则输出到 stdout",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.gold_suite is not None:
        raw = json.loads(args.gold_suite.read_text(encoding="utf-8"))
        report = evaluate_gold_suite(raw)
    else:
        report = evaluate_cases()

    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)

    verdict = report["threshold_verdict"]
    if report["failed_cases"]:
        return 1
    if verdict["gold_standard"] and verdict["status"] != "passed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
