"""测试骨架模板（Test Writer 使用，AC-03 要求模板含 `<占位符>` + 示例）。

用法：
    1. 复制到 `tests/test_<module>.py`；
    2. 把注释里的 `<package>.<module>` / `<function>` / `<expected>` 换成真实名称，
       并把示例函数改写成真实用例；
    3. 函数名遵守 `test_<行为>_<条件>_<期望>`，先跑出失败（红），再写实现（绿）。

约定（见 AGENTS.md §2）：
    - 只 import 标准库与被测模块，不联网、不写真实数据；
    - 至少 3 条用例，其中 1 条为边界/异常场景；
    - 断言使用明确的期望值，不写 `assert result` 这类弱断言。
"""
from __future__ import annotations

# 被测对象：from <package>.<module> import <function>
# 需要的标准库：from pathlib import Path 等


def test_example_returns_expected_value() -> None:
    """示例用例：改写成真实断言后删除本行。

    占位符含义：
        <behavior>   被测行为，如 import_plans
        <condition>  输入条件，如 duplicate_store_code
        <expected>   期望结果，如 keeps_first_record
    """
    # arrange：准备输入（可用 pytest 的 tmp_path 造临时 CSV）
    given = "<input>"
    # act：调用 <function>
    actual = given
    # assert：断言 <expected>
    assert actual == "<expected>"


def test_example_rejects_invalid_input() -> None:
    """边界/异常场景示例：用 pytest.raises 断言异常类型与错误信息。

    pytest.raises(ValueError, match="<关键错误信息>")
    """
    assert isinstance("<placeholder>", str)
