"""拜访计划 CSV 导入器。

本文件由 `/add --task T1` 的多角色 TDD 流程实现：先由 Test Writer 写
`tests/test_importer.py`，再由 Executor 在声明的文件范围内补全实现。
初始状态保持 `NotImplementedError`，保证演示时能看到「测试先失败 → 实现转绿」。
"""
from __future__ import annotations

from pathlib import Path

from visit_plan.models import Plan

#: CSV 表头，顺序固定；缺列视为非法输入（REQ-03）。
HEADER_FIELDS = ("store_code", "store_name", "plan_date", "owner")


def import_plans(path: Path) -> list[Plan]:
    """读取 UTF-8 CSV 并返回去重后的拜访计划列表。

    约定（docs/prd/visit-import.md）：
    - REQ-01 表头固定为 ``store_code,store_name,plan_date,owner``；
    - REQ-02 按 ``store_code`` 去重，保留首次出现的记录；
    - REQ-03 跳过空行；表头缺失抛 ``ValueError``；日期用
      ``datetime.date.fromisoformat`` 解析。

    尚未实现：由 TDD 流程补齐。
    """
    raise NotImplementedError("import_plans 由 /add --task T1 的 TDD 流程实现")
