"""`max_context_tokens` 预算收缩单测。"""

from __future__ import annotations

from pathlib import Path

from specflow.engine.runner import StageRunner
from specflow.engine.stages import Stage


def _stage(budget: int) -> Stage:
    return Stage(
        id="S1",
        command="/prd-input",
        slug="prd-input",
        title="PRD 输入",
        requires=(),
        produces=("prd.md",),
        human_gate=None,
        max_context_tokens=budget,
        skill_path=Path("SKILL.md"),
    )


def test_small_context_unchanged() -> None:
    runner = StageRunner.__new__(StageRunner)
    stage = _stage(3000)
    context = {"stage": "S1", "command": "/prd-input", "rules": [], "upstream": [], "domain": ""}
    assert runner._fit_context(stage, context) == context


def test_over_budget_shrinks_sections_and_domain() -> None:
    runner = StageRunner.__new__(StageRunner)
    stage = _stage(200)
    context = {
        "stage": "S1",
        "command": "/prd-input",
        "repo": "demo",
        "rules": [f"- R-{i:02d} 规则" for i in range(50)],
        "upstream": [
            {
                "name": "exploration.md",
                "summary": "s" * 2000,
                "sections": ["x" * 2000],
                "path": ".spec/artifacts/exploration.md",
            }
        ],
        "profile": {"language": "Python", "commands": ["pytest"]},
        "domain": "d" * 5000,
        "anchor_hint": "按 path#章节 读取",
    }
    fitted = runner._fit_context(stage, context)
    assert "sections" not in fitted["upstream"][0] if fitted["upstream"] else True
    assert len(fitted["domain"]) < 5000
    assert len(fitted["rules"]) <= 10