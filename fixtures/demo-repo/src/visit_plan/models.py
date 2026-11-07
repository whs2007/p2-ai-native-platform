"""拜访计划领域模型（演示仓库最小实现）。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass
class Plan:
    """一条待执行的拜访计划。

    与 CSV 的一行对应，字段顺序即表头顺序：
    ``store_code, store_name, plan_date, owner``。
    """

    store_code: str
    store_name: str
    plan_date: date
    owner: str
