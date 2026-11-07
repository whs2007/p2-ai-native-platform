"""规则回灌：只追加 / 去重 / 冲突 / 上限 / 版本（docs/07 §4.6、docs/04 §7）。"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from specflow.artifacts.feedback import apply_rules
from specflow.errors import ArtifactError, UsageError

AGENTS = """# AGENTS.md — demo 基线规则

> 版本：v0.2 ｜ 更新：2026-10-30 ｜ 由 specflow 维护，人工可编辑

## 1. 仓库画像
- 语言/运行时：Python 3.11

## 2. 工程规则（每条都必须可判定）
- R-01 新增函数必须有类型标注；公共函数必须有 docstring
- R-02 文件读写使用 `pathlib` 与 `encoding="utf-8"`
- R-03 不引入新依赖，除非在 `design.md` 选型表中列出理由

## 3. 流程规则
- 进入 `/plan-task` 前 `design.md` 必须 approved

## 4. 命令基线
| 用途 | 命令 |
|---|---|
| 测试 | `pytest -q` |

## 5. 变更记录
- v0.2 ← 回灌 R-05、R-06（来源：需求 old-feature）
- v0.1 ← 初始化（`/project-init`）
"""

PROPOSALS = [
    {
        "id": "R-07",
        "text": "新增接口必须提供 400/409 两种失败用例",
        "source": "REQ-02",
        "criteria": "接口测试缺少失败用例",
        "counterexample": "只测 200",
    },
    {
        "id": "R-08",
        "text": "金额字段一律使用 Decimal，不接受 float",
        "source": "REQ-02",
        "criteria": "出现 float 金额",
        "counterexample": "amount: float",
    },
]


def write_agents(tmp_path: Path, text: str = AGENTS) -> Path:
    path = tmp_path / "AGENTS.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_apply_rules_appends_and_bumps_version(tmp_path: Path) -> None:
    agents = write_agents(tmp_path)
    backup = tmp_path / "backup" / "AGENTS.v0.2.md"

    result = apply_rules(
        agents,
        PROPOSALS,
        owner="tester",
        source_req="visit-import",
        backup_path=backup,
    )

    assert result["version"] == "v0.3"
    assert result["added"] == ["R-07", "R-08"]
    assert result["appended"] == 2
    assert result["skipped_duplicate"] == []
    assert result["conflicts"] == []

    text = agents.read_text(encoding="utf-8")
    assert "- R-07 新增接口必须提供 400/409 两种失败用例" in text
    assert "- R-08 金额字段一律使用 Decimal，不接受 float" in text
    assert "判据：" in text and "反例：" in text
    assert "- v0.3 ← 回灌 R-07、R-08（来源：需求 visit-import）" in text
    assert "版本：v0.3" in text
    # 新规则落在 §2 内、§3 之前
    assert text.index("## 2. 工程规则") < text.index("- R-07") < text.index("## 3. 流程规则")
    # §5 最新在上
    assert text.index("- v0.3") < text.index("- v0.2")
    # 只追加：旧规则与其它章节原样保留
    for line in (
        "- R-01 新增函数必须有类型标注；公共函数必须有 docstring",
        '- R-02 文件读写使用 `pathlib` 与 `encoding="utf-8"`',
        "## 4. 命令基线",
        "| 测试 | `pytest -q` |",
        "- 进入 `/plan-task` 前 `design.md` 必须 approved",
    ):
        assert line in text
    # 备份是回灌前的原文
    assert backup.read_text(encoding="utf-8") == AGENTS


def test_duplicate_rule_is_skipped(tmp_path: Path) -> None:
    agents = write_agents(tmp_path)
    duplicate = {
        "id": "R-07",
        "text": "不引入新依赖，除非在 `design.md` 选型表中列出理由",
        "source": "REQ-02",
        "criteria": "新增依赖",
        "counterexample": "直接 pip install",
    }
    result = apply_rules(agents, [duplicate], owner="tester", source_req="visit-import")

    assert result["added"] == []
    assert result["appended"] == 0
    assert result["skipped_duplicate"] == ["R-07"]
    assert result["version"] == "v0.2"
    assert agents.read_text(encoding="utf-8") == AGENTS


def test_opposite_rule_reported_as_conflict(tmp_path: Path) -> None:
    text = AGENTS.replace(
        "- R-03 不引入新依赖，除非在 `design.md` 选型表中列出理由",
        "- R-03 必须为每个接口写失败用例",
    )
    agents = write_agents(tmp_path, text)
    conflicting = {
        "id": "R-07",
        "text": "禁止为每个接口写失败用例",
        "source": "REQ-02",
        "criteria": "用例太多",
        "counterexample": "每个接口都写",
    }
    result = apply_rules(agents, [conflicting], owner="tester", source_req="visit-import")

    assert result["added"] == []
    assert result["conflicts"], "语义相反的规则必须交人工，不自动合并"
    assert "R-03" in result["conflicts"][0]
    assert agents.read_text(encoding="utf-8") == text


def test_existing_rule_id_is_reassigned(tmp_path: Path) -> None:
    agents = write_agents(tmp_path)
    proposal = {
        "id": "R-01",
        "text": "所有金额字段使用 Decimal 表示",
        "source": "REQ-03",
        "criteria": "出现 float 金额",
        "counterexample": "amount: float",
    }
    result = apply_rules(agents, [proposal], owner="tester", source_req="visit-import")

    assert result["added"] == ["R-04"], "已占用编号应分配下一个空位"
    assert "- R-04 所有金额字段使用 Decimal 表示" in agents.read_text(encoding="utf-8")


def test_rule_limit_raises_usage_error(tmp_path: Path) -> None:
    rules = "\n".join(f"- R-{index:02d} 规则 {index}" for index in range(1, 31))
    text = AGENTS.replace(
        "- R-01 新增函数必须有类型标注；公共函数必须有 docstring\n"
        '- R-02 文件读写使用 `pathlib` 与 `encoding="utf-8"`\n'
        "- R-03 不引入新依赖，除非在 `design.md` 选型表中列出理由",
        rules,
    )
    agents = write_agents(tmp_path, text)

    with pytest.raises(UsageError) as excinfo:
        apply_rules(agents, PROPOSALS, owner="tester", source_req="visit-import")
    assert "30" in str(excinfo.value)
    assert agents.read_text(encoding="utf-8") == text, "超限不得部分写入"


def test_missing_section_raises(tmp_path: Path) -> None:
    no_section_2 = AGENTS.replace("## 2. 工程规则（每条都必须可判定）", "## 2b. 其它")
    agents = write_agents(tmp_path, no_section_2)
    with pytest.raises(UsageError):
        apply_rules(agents, PROPOSALS, owner="tester", source_req="x")

    no_section_5 = AGENTS.replace("## 5. 变更记录", "## 5b. 其它")
    agents = write_agents(tmp_path, no_section_5)
    with pytest.raises(UsageError):
        apply_rules(agents, PROPOSALS, owner="tester", source_req="x")


def test_missing_agents_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ArtifactError):
        apply_rules(tmp_path / "AGENTS.md", PROPOSALS, owner="tester", source_req="x")


def test_empty_proposals_is_noop(tmp_path: Path) -> None:
    agents = write_agents(tmp_path)
    result = apply_rules(agents, [], owner="tester", source_req="x")
    assert result["added"] == []
    assert result["appended"] == 0
    assert result["version"] == "v0.2"
    assert agents.read_text(encoding="utf-8") == AGENTS


def test_partial_duplicate_and_new(tmp_path: Path) -> None:
    agents = write_agents(tmp_path)
    duplicate = {
        "id": "R-07",
        "text": "新增函数必须有类型标注；公共函数必须有 docstring",
        "source": "REQ-01",
        "criteria": "缺标注",
        "counterexample": "def f(x)",
    }
    new_rule = {
        "id": "R-08",
        "text": "所有日期使用 date.fromisoformat 解析",
        "source": "REQ-02",
        "criteria": "手写日期解析",
        "counterexample": "date(int(...))",
    }
    result = apply_rules(agents, [duplicate, new_rule], owner="tester", source_req="visit-import")
    assert result["added"] == ["R-08"]
    assert result["skipped_duplicate"] == ["R-07"]
    text = agents.read_text(encoding="utf-8")
    assert "- R-08 所有日期使用 date.fromisoformat 解析" in text
    assert "- R-07 " not in text.split("## 2. 工程规则")[1].split("## 3. 流程规则")[0]


def test_backup_path_optional(tmp_path: Path) -> None:
    agents = write_agents(tmp_path)
    result = apply_rules(agents, PROPOSALS, owner="tester", source_req="visit-import")
    assert result["appended"] == 2
    assert not (tmp_path / "backup").exists()