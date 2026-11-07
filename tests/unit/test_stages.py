"""阶段口径单测：9 个命令 = 2 初始化 + 7 主链路。"""

from __future__ import annotations

from pathlib import Path

from specflow.engine.stages import (
    MAIN_PHASES,
    PREPARE_COMMANDS,
    REQUIRED_COMMANDS,
    load_stages,
)

SKILLS_DIR = Path(__file__).resolve().parents[2] / "specflow" / "skills"


def test_stage_groups_are_2_plus_7() -> None:
    assert PREPARE_COMMANDS == ("/project-init", "/domain-input")
    assert len(MAIN_PHASES) == 7
    assert MAIN_PHASES[0] == "/prd-input" and MAIN_PHASES[-1] == "/archive"
    assert REQUIRED_COMMANDS == PREPARE_COMMANDS + MAIN_PHASES
    assert len(REQUIRED_COMMANDS) == 9


def test_skills_load_nine_commands() -> None:
    stages = load_stages(SKILLS_DIR)
    assert list(stages) == list(REQUIRED_COMMANDS)
    assert len(stages) == 9