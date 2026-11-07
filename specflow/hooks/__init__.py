"""specflow.hooks —— Hook Runtime（协议 / 分发 / 四张规则表 / 四个事件 handler）。

对外常用入口：

- `specflow.hooks.protocol`：`HookInput` / `HookOutcome` / `inject` / `block` / `write_outcome`
- `specflow.hooks.manager.run_hook(event)`：`python -m specflow hook <Event>` 的实现
- `specflow.hooks.rules.check_tool_use` / `check_path_scope`：规则引擎（供 PreToolUse 与 TDD 复用）

本包只依赖 `config/errors/util`；对 `artifacts/` 与 `engine/` 使用函数内延迟导入，
保证 hook 进程冷启动快、且不因上层模块缺失而 import 失败（07 §1 依赖方向）。
"""
from __future__ import annotations

from specflow.hooks.protocol import (
    EXIT_BLOCK,
    EXIT_NEEDS_HUMAN,
    EXIT_OK,
    HookInput,
    HookOutcome,
    block,
    inject,
    needs_human,
    write_outcome,
)

__all__ = [
    "EXIT_BLOCK",
    "EXIT_NEEDS_HUMAN",
    "EXIT_OK",
    "HookInput",
    "HookOutcome",
    "block",
    "inject",
    "needs_human",
    "write_outcome",
]