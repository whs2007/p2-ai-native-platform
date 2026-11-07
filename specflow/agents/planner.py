"""Planner：任务 → ≤8 步 TDD 计划（docs/07 §5.8）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from specflow.agents.base import (
    AgentContext,
    AgentResult,
    BaseAgent,
    normalize_rel_path,
    task_file_set,
)
from specflow.agents.roles import assert_tool
from specflow.errors import GateBlocked
from specflow.output_schemas import OUTPUT_SCHEMAS

__all__ = ["Planner", "PLAN_SCHEMA"]

PLAN_SCHEMA: Mapping[str, Any] = OUTPUT_SCHEMAS["tdd-plan"]
MAX_STEPS = 8

_SYSTEM_INSTRUCTION = (
    "你是 TDD 的 Planner。把单个任务拆成最多 8 步的「测试先行」计划："
    "先写/更新测试，再写实现，最后跑验证；每一步都要给出目标文件 files[] 与验证方式 verify。"
    "files[] 只能来自任务声明范围，不得出现范围外文件；"
    "不得输出具体测试代码或实现代码，也不得输出 tasks/design 等其它阶段章节。\n"
    "输出字段：{\"steps\": [{\"n\": 1, \"goal\": \"...\", \"files\": [\"...\"], \"verify\": \"...\"}]}"
)


class Planner(BaseAgent):
    role = "planner"
    scenario = "tdd-plan"
    schema = PLAN_SCHEMA

    def build_messages(self, ctx: AgentContext) -> list[dict[str, str]]:
        return self._messages(_SYSTEM_INSTRUCTION, self._context_payload(ctx))

    # ------------------------------------------------------------------ #
    def plan(self, ctx: AgentContext) -> AgentResult:
        """生成并校验计划；任务不满足 test_first / 计划越界 → GateBlocked。"""
        assert_tool("planner", "llm")
        task = ctx.task or {}
        task_id = str(task.get("id") or "?")
        if task.get("test_first") is not True:
            raise GateBlocked(
                f"任务 {task_id} 未声明 test_first=true，拒绝进入 TDD 计划",
                reasons=[f"{task_id}.test_first={task.get('test_first')!r}（要求 true）"],
                howto="在 tasks.md 中把该任务标记 test_first: true 后重试 /add。",
            )
        declared = task_file_set(task)
        if not declared:
            raise GateBlocked(
                f"任务 {task_id} 没有声明文件范围 files[]，拒绝生成计划",
                howto="在 tasks.md 中补齐该任务的 files[]（至少 1 个文件）。",
            )
        result = self.invoke(ctx)
        steps = self._validate_steps(
            result.data.get("steps"), declared, task_id, ctx.project_dir
        )
        return AgentResult(
            data={"steps": steps},
            usage=result.usage,
            model=result.model,
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_steps(
        steps: Any, declared: set[str], task_id: str, project_dir: Path
    ) -> list[dict[str, Any]]:
        if not isinstance(steps, list) or not steps:
            raise GateBlocked(
                f"TDD 计划为空：任务 {task_id} 至少需要 1 个步骤",
                howto="让 Planner 输出 steps[]，每步包含 n/goal/files/verify。",
            )
        if len(steps) > MAX_STEPS:
            raise GateBlocked(
                f"TDD 计划步骤 {len(steps)} 步，超过上限 {MAX_STEPS}",
                howto="把任务拆小或合并步骤后重试。",
            )
        validated: list[dict[str, Any]] = []
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, Mapping):
                raise GateBlocked(f"TDD 计划第 {index} 步不是 JSON 对象")
            plan_no = step.get("n", index)
            goal = str(step.get("goal") or "").strip()
            verify = str(step.get("verify") or "").strip()
            files = step.get("files")
            if not goal:
                raise GateBlocked(f"TDD 计划第 {index} 步缺少 goal")
            if not verify:
                raise GateBlocked(f"TDD 计划第 {index} 步缺少验证方式 verify")
            if not isinstance(files, list) or not files:
                raise GateBlocked(f"TDD 计划第 {index} 步没有声明目标文件 files[]")
            normalized_files: list[str] = []
            for raw in files:
                rel = normalize_rel_path(raw, project_dir)
                if rel not in declared:
                    raise GateBlocked(
                        f"TDD 计划越界：步骤 {plan_no} 包含任务 {task_id} 范围外文件 {rel}",
                        reasons=[f"{plan_no}.files 含 {rel}，任务声明为 {sorted(declared)}"],
                        howto="Planner 只能使用 tasks.md 中该任务 files[] 的文件；修正后重试。",
                    )
                normalized_files.append(rel)
            validated.append(
                {
                    "n": int(plan_no) if isinstance(plan_no, int) else index,
                    "goal": goal,
                    "files": normalized_files,
                    "verify": verify,
                }
            )
        return validated