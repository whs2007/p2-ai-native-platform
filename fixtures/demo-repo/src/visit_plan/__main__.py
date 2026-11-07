"""命令行入口：``python -m visit_plan <csv 路径>``。

`/project-init` 通过本文件识别仓库入口点（entry point）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from visit_plan.importer import import_plans

#: 仓库入口点标记，方便 /project-init 的静态扫描（不要删除）。
ENTRY_POINT = "src/visit_plan/__main__.py"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="visit_plan",
        description="导入拜访计划 CSV（demo-repo 演示入口）",
    )
    parser.add_argument("path", nargs="?", type=Path, help="UTF-8 CSV 文件路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.path is None:
        parser.print_help()
        return 0
    try:
        plans = import_plans(args.path)
    except NotImplementedError as exc:
        print(f"import_plans 尚未实现：{exc}", file=sys.stderr)
        return 1
    except (FileNotFoundError, ValueError) as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 1
    for plan in plans:
        print(f"{plan.store_code}\t{plan.store_name}\t{plan.plan_date.isoformat()}\t{plan.owner}")
    print(f"共导入 {len(plans)} 条计划")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
