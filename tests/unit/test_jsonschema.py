"""轻量 JSON Schema 校验单元测试（docs/07 §2.4）。"""
from __future__ import annotations

import pytest

from specflow.errors import UsageError
from specflow.jsonschema import validate


def test_required_and_types() -> None:
    schema = {
        "type": "object",
        "required": ["name", "count"],
        "properties": {
            "name": {"type": "string", "minLength": 2},
            "count": {"type": "integer", "minimum": 1},
            "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        },
    }
    assert validate({"name": "ab", "count": 2, "tags": ["x"]}, schema) == []
    errors = validate({"name": "a", "count": 0}, schema)
    assert any("name".__contains__("name") and "长度" in item for item in errors)
    assert any("count" in item for item in errors)


def test_enum_and_const() -> None:
    schema = {"type": "string", "enum": ["待确认", "已确认"]}
    assert validate("待确认", schema) == []
    assert validate("未知", schema)


def test_nested_object_errors() -> None:
    schema = {
        "type": "object",
        "properties": {"risk": {"type": "object", "required": ["rollback"]}},
    }
    errors = validate({"risk": {}}, schema)
    assert errors == ["$.risk: 缺少必填字段 rollback"]


def test_bool_is_not_integer() -> None:
    assert validate(True, {"type": "integer"})
    assert validate(True, {"type": "boolean"}) == []


def test_unsupported_keyword_raises() -> None:
    with pytest.raises(UsageError):
        validate({}, {"$ref": "#/definitions/x"})
    with pytest.raises(UsageError):
        validate({}, {"oneOf": []})