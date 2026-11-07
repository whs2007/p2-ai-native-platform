"""产物管理器：front-matter / store / schema / indexer / lock / feedback（docs/07 §4）。

依赖方向（docs/07 §1）：`artifacts` 只依赖最底层的 config/errors/util/jsonschema，
不得 import `engine`/`hooks`（否则形成反向依赖）。
"""
from __future__ import annotations

#: 产物状态机取值（docs/07 §3.1）
ARTIFACT_STATUSES: tuple[str, ...] = ("draft", "reviewed", "approved", "superseded")

__all__ = ["ARTIFACT_STATUSES"]