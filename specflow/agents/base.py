"""Agent 基础设施：上下文、结果、消息构造与写入范围校验（docs/07 §5.8）。

依赖方向：`agents` 可以 import `llm` / `errors` / `util` / `output_schemas`，
不得 import `engine.runner` / `hooks.manager`；对 `hooks.rules` 只在写入路径时需要，
因此用延迟 import + 本地兜底，保证 T2 未就绪时本模块可单独测试。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from specflow.errors import GateBlocked, LLMError, UsageError
from specflow.llm.base import LLMClient

__all__ = [
    "AgentContext",
    "AgentResult",
    "BaseAgent",
    "ScopeDecision",
    "normalize_rel_path",
    "task_file_set",
    "check_scope",
    "ensure_scope",
    "artifact_brief",
    "artifact_section",
]


# ---------------------------------------------------------------------- #
# 数据结构
# ---------------------------------------------------------------------- #
@dataclass
class AgentContext:
    """单个 TDD 任务的完整上下文（角色之间只交换结构化 JSON）。"""

    project_dir: Path
    task: dict[str, Any]
    tasks: list[dict[str, Any]]
    design_brief: str
    domain_rules: str
    repo_brief: str
    plan: dict[str, Any] | None = None
    failure_items: list[dict[str, Any]] = field(default_factory=list)
    round: int = 1


@dataclass
class AgentResult:
    data: dict[str, Any]
    usage: dict[str, int]
    model: str


@dataclass(frozen=True)
class ScopeDecision:
    severity: str  # allow | deny
    rule_id: str
    reason: str
    rel: str


# ---------------------------------------------------------------------- #
# 路径与任务范围
# ---------------------------------------------------------------------- #
def normalize_rel_path(path: str | Path, project_dir: Path) -> str:
    """把模型返回的路径规范为仓库内相对 POSIX 路径；越界直接阻断。"""
    raw = str(path).strip().replace("\\", "/")
    if not raw:
        raise GateBlocked("写入路径为空，拒绝执行")
    candidate = Path(raw)
    project_resolved = Path(project_dir).resolve()
    if candidate.is_absolute():
        try:
            rel = candidate.resolve().relative_to(project_resolved).as_posix()
        except ValueError as exc:
            raise GateBlocked(
                f"写入路径越界：{raw} 不在仓库 {project_resolved} 内",
                reasons=[f"路径 {raw} 解析后超出 project_dir"],
                howto="只写当前任务 files[] 声明的仓库内相对路径。",
            ) from exc
    else:
        parts: list[str] = []
        for part in raw.split("/"):
            if part in {"", "."}:
                continue
            if part == "..":
                raise GateBlocked(
                    f"写入路径越界：{raw} 包含 '..'",
                    howto="只写当前任务 files[] 声明的仓库内相对路径。",
                )
            parts.append(part)
        rel = "/".join(parts)
    if not rel:
        raise GateBlocked(f"写入路径非法：{raw!r}")
    return rel


def task_file_set(task: Mapping[str, Any] | None) -> set[str]:
    """任务声明文件白名单（规范化为相对 POSIX 路径）。"""
    if not task:
        return set()
    raw = task.get("files") or []
    if isinstance(raw, str):
        raw = [raw]
    result: set[str] = set()
    for item in raw:
        if not item:
            continue
        normalized = str(item).replace("\\", "/").strip()
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized:
            result.add(normalized)
    return result


def _local_scope_decision(rel: str, task: Mapping[str, Any], role: str) -> ScopeDecision:
    """C-01 / C-02 / C-03 的本地兜底实现（hooks/rules.py 不可用或被跳过时）。"""
    allowed = task_file_set(task)
    task_id = str(task.get("id") or "?")
    name = rel.rsplit("/", 1)[-1]
    if rel not in allowed:
        return ScopeDecision(
            "deny",
            "C-01",
            f"写入越界：{rel} 不在任务 {task_id} 的 files[] 内",
            rel,
        )
    if role == "test_writer":
        if rel.startswith("src/") or (name.endswith(".py") and not name.startswith("test_")):
            return ScopeDecision(
                "deny", "C-02", f"test_writer 不得写实现文件：{rel}", rel
            )
        if not (name.endswith(".py") and name.startswith("test_")):
            return ScopeDecision(
                "deny", "C-02", f"test_writer 只能写测试文件（test_*.py）：{rel}", rel
            )
    if role == "executor" and name.endswith(".py") and name.startswith("test_"):
        return ScopeDecision("deny", "C-03", f"executor 不得改写既有测试：{rel}", rel)
    return ScopeDecision("allow", "C-00", "", rel)


def _hook_scope_decision(
    rel: str, task: Mapping[str, Any], role: str, project_dir: Path
) -> ScopeDecision | None:
    """优先复用 T2 的 hooks.rules.check_path_scope；不可用时返回 None 走本地兜底。"""
    try:  # 延迟 import：并行开发期 hooks 可能尚不存在
        from specflow.hooks import rules as hook_rules
    except Exception:
        return None
    checker = getattr(hook_rules, "check_path_scope", None)
    if not callable(checker):
        return None
    try:
        hit = checker(
            Path(rel),
            task,
            role=role,
            project_dir=Path(project_dir),
            tasks_artifact=None,
        )
    except Exception:  # 规则实现未就绪/签名不符时退回本地判断，不阻断主流程
        return None
    severity = str(getattr(hit, "severity", "") or "")
    rule_id = str(getattr(hit, "rule_id", "") or "C-01")
    reason = str(getattr(hit, "reason", "") or "").strip()
    if severity == "deny":
        return ScopeDecision("deny", rule_id, reason or f"规则 {rule_id} 拒绝写入 {rel}", rel)
    if severity == "confirm":
        return ScopeDecision(
            "deny", rule_id, f"规则 {rule_id} 要求人工确认写入 {rel}（TDD 自动流程中按拒绝处理）", rel
        )
    return None


def check_scope(
    path: str | Path,
    *,
    task: Mapping[str, Any],
    role: str,
    project_dir: Path,
) -> ScopeDecision:
    """组合本地规则（保证契约语义）+ hooks.rules（T2 唯一事实源）。"""
    rel = normalize_rel_path(path, project_dir)
    local = _local_scope_decision(rel, task, role)
    if local.severity == "deny":
        return local
    external = _hook_scope_decision(rel, task, role, project_dir)
    if external is not None and external.severity == "deny":
        return external
    return local


def ensure_scope(path: str | Path, *, ctx: AgentContext, role: str) -> str:
    """校验写入范围；越界抛 GateBlocked，返回规范化相对路径。"""
    decision = check_scope(path, task=ctx.task, role=role, project_dir=ctx.project_dir)
    if decision.severity == "deny":
        raise GateBlocked(
            f"{role} 写入被规则 {decision.rule_id} 阻断：{decision.reason}",
            reasons=[f"{decision.rule_id}: {decision.reason}"],
            howto="把写入范围限制在 tasks.md 当前任务的 files[] 内后重试。",
        )
    return decision.rel


# ---------------------------------------------------------------------- #
# 上游产物摘要
# ---------------------------------------------------------------------- #
def artifact_brief(artifact: Any) -> str:
    """Artifact.summary() 的安全读取（兼容 T1 未就绪/仅传字符串的场景）。"""
    if artifact is None:
        return ""
    if isinstance(artifact, str):
        return artifact.strip()
    summary = getattr(artifact, "summary", None)
    try:
        text = summary() if callable(summary) else str(summary or "")
    except Exception:
        text = ""
    return str(text or "").strip()


def artifact_section(artifact: Any, *titles: str) -> str:
    """读取产物某个章节正文（忽略编号差异），失败回退摘要。"""
    if artifact is None:
        return ""
    body_reader = getattr(artifact, "section_body", None)
    if callable(body_reader):
        for title in titles:
            try:
                body = body_reader(title)
            except Exception:
                body = None
            if body and str(body).strip():
                return str(body).strip()
    return artifact_brief(artifact)


# ---------------------------------------------------------------------- #
# Agent 基类
# ---------------------------------------------------------------------- #
_JSON_RULE = (
    "只输出一个 JSON 对象：不要 Markdown 代码围栏、不要解释、不要在 JSON 前后加任何文字。"
    "只输出本阶段字段；不得输出其它阶段的章节（例如技术方案阶段不得输出任务拆解或实现代码）。"
)


class BaseAgent:
    """所有角色的基类：负责组装 messages、调用 LLM、解包 AgentResult。"""

    scenario: str = ""
    role: str = "agent"
    schema: Mapping[str, Any] | None = None

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    # ------------------------------------------------------------------ #
    def build_messages(self, ctx: AgentContext) -> list[dict[str, str]]:
        raise NotImplementedError

    def invoke(
        self,
        ctx: AgentContext,
        *,
        scenario: str | None = None,
        schema: Mapping[str, Any] | None = None,
        round: int | None = None,
    ) -> AgentResult:
        active_scenario = scenario or self.scenario
        if not active_scenario:
            raise UsageError(f"{type(self).__name__} 未配置 scenario")
        active_schema = schema if schema is not None else self.schema
        messages = self.build_messages(ctx)
        llm_round = ctx.round if round is None else round
        result = self.llm.complete(
            messages,
            schema=active_schema,
            scenario=active_scenario,
            round=llm_round,
        )
        data = result.data
        if data is None:
            raise LLMError(f"{active_scenario} 未返回结构化 JSON data")
        if not isinstance(data, dict):
            raise LLMError(f"{active_scenario} 返回的 data 必须是对象，实际是 {type(data).__name__}")
        return AgentResult(data=data, usage=dict(result.usage or {}), model=result.model)

    # ------------------------------------------------------------------ #
    def _messages(self, instruction: str, payload: Mapping[str, Any]) -> list[dict[str, str]]:
        system = f"你是 {self.role}。{instruction}\n\n{_JSON_RULE}"
        user = (
            "本轮上下文（JSON）：\n"
            + json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    @staticmethod
    def _context_payload(ctx: AgentContext) -> dict[str, Any]:
        return {
            "round": ctx.round,
            "task": ctx.task,
            "tasks": ctx.tasks,
            "plan": ctx.plan,
            "failure_items": ctx.failure_items,
            "design_brief": ctx.design_brief,
            "domain_rules": ctx.domain_rules,
            "repo_brief": ctx.repo_brief,
        }