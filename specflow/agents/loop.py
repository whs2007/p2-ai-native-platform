"""TDDRunner：四角色循环 ≤max_rounds 轮 + 失败回流 + 事件流（docs/07 §5.8）。

一条 `run_task` 的固定顺序（每轮都重复）：

    planner/plan_ready → test_writer/test_written → executor/impl_written
    → verifier/fail 或 verifier/pass

事件同时写入：
- `.spec/artifacts/tdd-report.jsonl`（每行一个事件，顺序可断言）；
- `TDDOutcome.events`（内存副本，供 runner 渲染摘要）。

`tdd-plan.md` 由本类在 store 可用时逐轮保存（meta: task_id/round，status=draft）；
`tdd-report.md` 是**多任务汇总产物**，由 `engine.runner` 的 `/add` handler 依据
`TDDOutcome` 渲染保存（本类只写 `tdd-report.jsonl` 事件流，避免双写与版本噪声）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from specflow.agents.base import (
    AgentContext,
    artifact_brief,
    artifact_section,
)
from specflow.agents.executor import Executor
from specflow.agents.planner import Planner
from specflow.agents.test_writer import TestWriter
from specflow.agents.verifier import Verifier
from specflow.errors import UsageError
from specflow.util import append_jsonl, now_iso, rel_posix

__all__ = ["TDDOutcome", "TDDRunner", "render_tdd_plan"]


@dataclass
class TDDOutcome:
    task_id: str
    rounds: int
    verdict: str  # pass | fail | needs_human
    passed: int
    total: int
    failure_items: list[dict[str, Any]]
    events: list[dict[str, Any]]
    plan: dict[str, Any] | None = None
    test_files: list[str] = field(default_factory=list)
    impl_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "rounds": self.rounds,
            "verdict": self.verdict,
            "passed": self.passed,
            "total": self.total,
            "failure_items": list(self.failure_items),
            "events": list(self.events),
            "plan": self.plan,
            "test_files": list(self.test_files),
            "impl_files": list(self.impl_files),
        }


# ---------------------------------------------------------------------- #
# Markdown 渲染（与 07 §4.3 各产物必备章节/附录对齐）
# ---------------------------------------------------------------------- #
def render_tdd_plan(task_id: str, round_no: int, plan: Mapping[str, Any]) -> str:
    steps = list(plan.get("steps") or [])
    lines: list[str] = [f"# TDD 计划 · {task_id}", "", "## 步骤", ""]
    for step in steps:
        lines.append(f"{step.get('n')}. {step.get('goal', '')}")
        files = "、".join(str(item) for item in (step.get("files") or []))
        lines.append(f"   - 目标文件：{files}")
        lines.append(f"   - 验证：{step.get('verify', '')}")
    lines += [
        "",
        "## 验证方式",
        "",
        f"- 轮次：round={round_no}",
        "- 每步以 `python -m pytest -q <测试文件>` 验证；失败项回流下一轮计划。",
        "",
        "## 附录：结构化数据",
        "",
        "```json",
        json.dumps({"steps": steps}, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------- #
# 编排器
# ---------------------------------------------------------------------- #
class TDDRunner:
    def __init__(
        self,
        settings: Any,
        store: Any = None,
        state_store: Any = None,
        llm: Any = None,
        indexer: Any = None,
        verifier: Verifier | None = None,
        *,
        save_artifacts: bool = True,
    ) -> None:
        self.settings = settings
        if llm is None:
            from specflow.llm import get_llm

            llm = get_llm(settings)
        self.llm = llm
        self.store = store
        self.state_store = state_store
        self.indexer = indexer
        self.verifier = verifier or Verifier()
        self.save_artifacts = save_artifacts
        self.planner = Planner(llm)
        self.test_writer = TestWriter(llm)
        self.executor = Executor(llm)

    # ------------------------------------------------------------------ #
    @property
    def project_dir(self) -> Path:
        return Path(self.settings.project_dir)

    @property
    def report_jsonl_path(self) -> Path:
        return Path(self.settings.artifacts_dir) / "tdd-report.jsonl"

    # ------------------------------------------------------------------ #
    def run_task(
        self,
        task: dict[str, Any],
        *,
        design: Any = None,
        domain: Any = None,
        max_rounds: int | None = None,
    ) -> TDDOutcome:
        task = dict(task or {})
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            raise UsageError("任务缺少 id，无法执行 TDD")
        limit = int(
            max_rounds
            if max_rounds is not None
            else getattr(self.settings, "max_rounds", 3) or 3
        )
        if limit < 1:
            raise UsageError(f"max_rounds 必须 ≥ 1，实际 {limit}")

        project_dir = self.project_dir
        design_brief = artifact_brief(design)
        domain_rules = artifact_section(domain, "核心规则", "规则")
        repo_brief = f"仓库 {project_dir.name}；任务 {task_id}"
        events: list[dict[str, Any]] = []
        failure_items: list[dict[str, Any]] = []
        plan: dict[str, Any] | None = None
        test_rel: list[str] = []
        impl_rel: list[str] = []
        impl_paths: list[Path] = []
        test_paths: list[Path] = []
        last_result: dict[str, Any] | None = None
        verdict = "needs_human"
        round_no = 0

        for round_no in range(1, limit + 1):
            ctx = AgentContext(
                project_dir=project_dir,
                task=task,
                tasks=[task],
                design_brief=design_brief,
                domain_rules=domain_rules,
                repo_brief=repo_brief,
                plan=plan,
                failure_items=list(failure_items),
                round=round_no,
            )

            started = time.monotonic()
            plan_result = self.planner.plan(ctx)
            plan = plan_result.data
            self._emit(
                events,
                round_no=round_no,
                role="planner",
                event="plan_ready",
                steps=len(plan.get("steps") or []),
                elapsed_ms=_elapsed_ms(started),
                tokens=_usage_total(plan_result.usage),
            )
            self._save_plan(task_id, round_no, plan)
            ctx.plan = plan

            started = time.monotonic()
            tests_result = self.test_writer.write(ctx)
            test_paths = self.test_writer.apply(ctx, tests_result)
            test_rel = [rel_posix(path, project_dir) for path in test_paths]
            self._emit(
                events,
                round_no=round_no,
                role="test_writer",
                event="test_written",
                files=list(test_rel),
                cases=tests_result.data.get("cases"),
                elapsed_ms=_elapsed_ms(started),
                tokens=_usage_total(tests_result.usage),
            )

            started = time.monotonic()
            impl_result = self.executor.apply(ctx)
            impl_paths = self.executor.write(ctx, impl_result)
            impl_rel = [rel_posix(path, project_dir) for path in impl_paths]
            self._emit(
                events,
                round_no=round_no,
                role="executor",
                event="impl_written",
                files=list(impl_rel),
                elapsed_ms=_elapsed_ms(started),
                tokens=_usage_total(impl_result.usage),
            )

            started = time.monotonic()
            last_result = self.verifier.run(ctx, test_paths)
            event_name = "pass" if last_result.get("passed") else "fail"
            self._emit(
                events,
                round_no=round_no,
                role="verifier",
                event=event_name,
                passed=last_result.get("passed_count", 0),
                total=last_result.get("total", 0),
                failure_items=[
                    str(item.get("case", ""))
                    for item in last_result.get("failure_items", [])
                ],
                elapsed_ms=_elapsed_ms(started),
                tokens=0,
            )
            if last_result.get("passed"):
                verdict = "pass"
                failure_items = []
                break
            failure_items = list(last_result.get("failure_items") or [])
        else:
            verdict = "needs_human"
            self._emit(
                events,
                round_no=round_no,
                role="loop",
                event="needs_human",
                rounds=limit,
                failure_items=[str(item.get("case", "")) for item in failure_items],
            )
            if self.state_store is not None:
                self.state_store.set_needs_human(True)

        outcome = TDDOutcome(
            task_id=task_id,
            rounds=round_no,
            verdict=verdict,
            passed=int(last_result.get("passed_count", 0)) if last_result else 0,
            total=int(last_result.get("total", 0)) if last_result else 0,
            failure_items=[] if verdict == "pass" else list(failure_items),
            events=events,
            plan=plan,
            test_files=list(test_rel),
            impl_files=list(impl_rel),
        )
        self._register_files(task_id, impl_paths, test_paths)
        return outcome

    # ------------------------------------------------------------------ #
    def run(
        self,
        tasks: list[dict[str, Any]],
        *,
        design: Any = None,
        domain: Any = None,
        max_rounds: int | None = None,
    ) -> list[TDDOutcome]:
        outcomes: list[TDDOutcome] = []
        for task in tasks:
            outcomes.append(
                self.run_task(task, design=design, domain=domain, max_rounds=max_rounds)
            )
        return outcomes

    # ------------------------------------------------------------------ #
    # 事件流
    # ------------------------------------------------------------------ #
    def _emit(
        self,
        events: list[dict[str, Any]],
        *,
        round_no: int,
        role: str,
        event: str,
        **extra: Any,
    ) -> None:
        record: dict[str, Any] = {
            "ts": now_iso(),
            "stage": "S5",
            "round": round_no,
            "role": role,
            "event": event,
        }
        for key, value in extra.items():
            if value is not None:
                record[key] = value
        events.append(record)
        try:
            append_jsonl(self.report_jsonl_path, record)
        except OSError:  # pragma: no cover - 磁盘异常时不影响内存事件流
            pass

    # ------------------------------------------------------------------ #
    # 产物落盘（store 可用时才执行）
    # ------------------------------------------------------------------ #
    def _artifacts_enabled(self) -> bool:
        return bool(self.store is not None and self.save_artifacts)

    def _save_plan(self, task_id: str, round_no: int, plan: Mapping[str, Any]) -> None:
        if not self._artifacts_enabled():
            return
        body = render_tdd_plan(task_id, round_no, plan)
        self.store.save(
            "tdd-plan.md",
            body,
            meta={"task_id": task_id, "round": round_no},
            status="draft",
            stage="S5",
        )

    def _register_files(
        self,
        task_id: str,
        impl_paths: Iterable[Path],
        test_paths: Iterable[Path] = (),
    ) -> None:
        if self.indexer is None:
            return
        try:
            for path in impl_paths:
                self.indexer.register_file(
                    Path(path), task_id=task_id, stage="S5", source="executor"
                )
            for path in test_paths:
                self.indexer.register_file(
                    Path(path), task_id=task_id, stage="S5", source="test_writer"
                )
        except Exception:  # 索引是旁路能力，失败不回滚 TDD 结果
            return


# ---------------------------------------------------------------------- #
def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _usage_total(usage: Mapping[str, Any] | None) -> int:
    if not usage:
        return 0
    try:
        return int(usage.get("total_tokens") or 0)
    except (TypeError, ValueError):
        return 0