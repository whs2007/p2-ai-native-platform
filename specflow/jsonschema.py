"""轻量 JSON Schema 校验（docs/07 §2.4）。

只支持本项目用到的关键字；遇到 $ref/oneOf 明确报错，避免"静默放过"。
"""
from __future__ import annotations

from typing import Any, Mapping

from specflow.errors import UsageError

_UNSUPPORTED = ("$ref", "oneOf", "anyOf", "allOf", "patternProperties")


def _type_ok(data: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(data, dict)
    if expected == "array":
        return isinstance(data, list)
    if expected == "string":
        return isinstance(data, str)
    if expected == "integer":
        return isinstance(data, int) and not isinstance(data, bool)
    if expected == "number":
        return isinstance(data, (int, float)) and not isinstance(data, bool)
    if expected == "boolean":
        return isinstance(data, bool)
    if expected == "null":
        return data is None
    raise UsageError(f"不支持的 JSON Schema 类型：{expected}")


def validate(data: Any, schema: Mapping[str, Any], *, path: str = "$") -> list[str]:
    """返回错误列表，空表示通过。"""
    if not isinstance(schema, Mapping):
        raise UsageError("JSON Schema 必须是对象")
    for key in _UNSUPPORTED:
        if key in schema:
            raise UsageError(f"本项目的 JSON Schema 不支持 {key}")

    errors: list[str] = []

    if "const" in schema and data != schema["const"]:
        errors.append(f"{path}: 期望常量 {schema['const']!r}，实际 {data!r}")
    if "enum" in schema and data not in schema["enum"]:
        errors.append(f"{path}: 期望取值 {schema['enum']}，实际 {data!r}")

    expected = schema.get("type")
    if isinstance(expected, (list, tuple)):
        if not any(_type_ok(data, item) for item in expected):
            errors.append(f"{path}: 类型应为 {'/'.join(expected)}，实际 {type(data).__name__}")
            return errors
    elif isinstance(expected, str):
        if not _type_ok(data, expected):
            errors.append(f"{path}: 类型应为 {expected}，实际 {type(data).__name__}")
            return errors

    if isinstance(data, str):
        if "minLength" in schema and len(data) < int(schema["minLength"]):
            errors.append(f"{path}: 长度应 ≥ {schema['minLength']}")
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        if "minimum" in schema and data < schema["minimum"]:
            errors.append(f"{path}: 应 ≥ {schema['minimum']}")
        if "maximum" in schema and data > schema["maximum"]:
            errors.append(f"{path}: 应 ≤ {schema['maximum']}")
    if isinstance(data, list):
        if "minItems" in schema and len(data) < int(schema["minItems"]):
            errors.append(f"{path}: 元素数应 ≥ {schema['minItems']}")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(data):
                errors.extend(validate(item, item_schema, path=f"{path}[{index}]"))
    if isinstance(data, dict):
        for key in schema.get("required", ()):  # type: ignore[arg-type]
            if key not in data:
                errors.append(f"{path}: 缺少必填字段 {key}")
        properties = schema.get("properties") or {}
        if isinstance(properties, Mapping):
            for key, sub_schema in properties.items():
                if key in data:
                    errors.extend(validate(data[key], sub_schema, path=f"{path}.{key}"))
    return errors