"""`visit_plan.models` 的既有测试：测试风格样板。

命名约定：``test_<行为>_<条件>_<期望>``；每个用例只断言一个行为，
边界用例单独一条（datetime 不是 date 的子类陷阱）。
"""
from __future__ import annotations

from datetime import date, datetime

from visit_plan.models import Plan


def test_plan_equality_same_fields_returns_equal_plans() -> None:
    left = Plan("ST-001", "望京门店", date(2026, 11, 2), "lin.xu")
    right = Plan("ST-001", "望京门店", date(2026, 11, 2), "lin.xu")
    assert left == right


def test_plan_keeps_all_four_fields_as_given() -> None:
    plan = Plan("ST-002", "中关村门店", date(2026, 11, 3), "chen.yu")
    assert plan.store_code == "ST-002"
    assert plan.store_name == "中关村门店"
    assert plan.plan_date == date(2026, 11, 3)
    assert plan.owner == "chen.yu"


def test_plan_date_is_pure_date_not_datetime() -> None:
    plan = Plan("ST-003", "西单门店", date(2026, 11, 4), "lin.xu")
    assert isinstance(plan.plan_date, date)
    assert not isinstance(plan.plan_date, datetime)
