"""阶段输出 JSON Schema（runner 与 mock 共用的唯一形状）。

约定：所有 LLM 阶段的返回都必须是这里的 schema 描述的对象；runner 校验后才渲染 Markdown，
mock_data 也必须满足同一 schema（测试会双向校验）。
"""
from __future__ import annotations

from typing import Any

_DOMAIN = {
    "type": "object",
    "required": ["domain", "rules", "terms", "boundaries"],
    "properties": {
        "domain": {"type": "string", "minLength": 10},
        "rules": {
            "type": "array",
            "minItems": 5,
            "items": {
                "type": "object",
                "required": ["id", "rule", "counterexample"],
                "properties": {
                    "id": {"type": "string"},
                    "rule": {"type": "string"},
                    "counterexample": {"type": "string"},
                },
            },
        },
        "terms": {
            "type": "array",
            "minItems": 8,
            "items": {
                "type": "object",
                "required": ["term", "meaning", "confusable"],
                "properties": {
                    "term": {"type": "string"},
                    "meaning": {"type": "string"},
                    "confusable": {"type": "string"},
                },
            },
        },
        "boundaries": {"type": "array", "items": {"type": "string"}},
    },
}

_PRD = {
    "type": "object",
    "required": ["title", "background", "requirements", "out_of_scope"],
    "properties": {
        "title": {"type": "string"},
        "background": {"type": "string", "minLength": 10},
        "requirements": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "title", "desc", "acceptance"],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "desc": {"type": "string"},
                    "acceptance": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                },
            },
        },
        "out_of_scope": {"type": "array", "items": {"type": "string"}},
    },
}

_EXPLORE = {
    "type": "object",
    "required": ["modules", "constraints", "unknowns"],
    "properties": {
        "modules": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["path", "purpose"],
                "properties": {"path": {"type": "string"}, "purpose": {"type": "string"}},
            },
        },
        "constraints": {"type": "array", "items": {"type": "string"}},
        "unknowns": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["item", "status"],
                "properties": {
                    "item": {"type": "string"},
                    "status": {"type": "string", "enum": ["待确认", "已确认", "无"]},
                },
            },
        },
        "deps": {"type": "array", "items": {"type": "string"}},
    },
}

_DESIGN = {
    "type": "object",
    "required": ["summary", "options", "changes", "interfaces", "risks"],
    "properties": {
        "summary": {"type": "string", "minLength": 5},
        "options": {
            "type": "array",
            "minItems": 2,
            "items": {
                "type": "object",
                "required": ["name", "pros", "cons"],
                "properties": {
                    "name": {"type": "string"},
                    "pros": {"type": "string"},
                    "cons": {"type": "string"},
                },
            },
        },
        "changes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["path", "change"],
                "properties": {"path": {"type": "string"}, "change": {"type": "string"}},
            },
        },
        "interfaces": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "detail"],
                "properties": {"name": {"type": "string"}, "detail": {"type": "string"}},
            },
        },
        "risks": {
            "type": "array",
            "minItems": 3,
            "items": {
                "type": "object",
                "required": ["id", "desc", "mitigation", "rollback"],
                "properties": {
                    "id": {"type": "string"},
                    "desc": {"type": "string"},
                    "mitigation": {"type": "string"},
                    "rollback": {"type": "string"},
                },
            },
        },
    },
}

_TASKS = {
    "type": "object",
    "required": ["feature", "tasks", "order"],
    "properties": {
        "feature": {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
        },
        "tasks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "title", "desc", "files", "test_first", "estimate_h", "acceptance"],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "desc": {"type": "string"},
                    "files": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                    "test_first": {"type": "boolean"},
                    "estimate_h": {"type": "number", "minimum": 0.25, "maximum": 4},
                    "acceptance": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                },
            },
        },
        "order": {"type": "array", "items": {"type": "string"}},
    },
}

_ARCHIVE = {
    "type": "object",
    "required": ["summary", "changes", "rules_proposed", "reusable"],
    "properties": {
        "summary": {"type": "string", "minLength": 10},
        "changes": {"type": "array", "items": {"type": "string"}},
        "rules_proposed": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "text", "source", "criteria", "counterexample"],
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "source": {"type": "string"},
                    "criteria": {"type": "string"},
                    "counterexample": {"type": "string"},
                },
            },
        },
        "reusable": {"type": "array", "items": {"type": "string"}},
    },
}

_TDD_PLAN = {
    "type": "object",
    "required": ["steps"],
    "properties": {
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "required": ["n", "goal", "files", "verify"],
                "properties": {
                    "n": {"type": "integer", "minimum": 1},
                    "goal": {"type": "string"},
                    "files": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                    "verify": {"type": "string"},
                },
            },
        }
    },
}

_TDD_TESTS = {
    "type": "object",
    "required": ["files", "cases"],
    "properties": {
        "files": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["path", "content"],
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            },
        },
        "cases": {"type": "integer", "minimum": 3},
    },
}

_TDD_IMPL = {
    "type": "object",
    "required": ["files"],
    "properties": {
        "files": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["path", "content"],
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            },
        }
    },
}

OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "domain-input": _DOMAIN,
    "prd-input": _PRD,
    "explore": _EXPLORE,
    "tech-design": _DESIGN,
    "plan-task": _TASKS,
    "archive": _ARCHIVE,
    "tdd-plan": _TDD_PLAN,
    "tdd-tests": _TDD_TESTS,
    "tdd-impl": _TDD_IMPL,
}