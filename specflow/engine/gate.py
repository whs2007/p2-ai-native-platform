"""门控：机器门 + 人门（docs/07 §5.4）。

**纯函数**：只读 `ArtifactStore` 与 `State`，不写任何文件（写操作在 CLI/runner 侧）。
同一实现被 `UserPromptSubmit`、`runner._precheck`、`status --check` 复用，避免规则两套。

人门规则（ADR-009）：
- `design`：阻断式——`state.gates.design.status == "approved"` 且 `design.md` 存在/approved/hash 一致/schema 通过；
- `agents-md`：不阻断命令（回灌由 `specflow gate approve agents-md` 触发）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from specflow.artifacts import schema as artifact_schema
from specflow.artifacts.store import Artifact, ArtifactStore
from specflow.engine.stages import Stage
from specflow.engine.state import GateState, State
from specflow.util import truncate_by_tokens

#: 不在 `.spec/artifacts/` 的阶段依赖（仓库根文件，只查存在性）
ROOT_DOCS: frozenset[str] = frozenset({"AGENTS.md", "domain.md", "index.md", "README.md"})

_BLOCKING_GATE = "design"
_NON_BLOCKING_GATES = frozenset({"agents-md"})
_GATE_KEYS = {"design.md": "design", "tdd-report.md": "tdd", "qa-report.md": "qa"}


@dataclass
class GateResult:
    """门控结论：可直接把 missing/howto 展示给用户。"""

    passed: bool
    missing: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    howto: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.passed


# ---------------------------------------------------------------------- #
# 人门文案
# ---------------------------------------------------------------------- #
def _gate_key(artifact_name: str) -> str:
    return _GATE_KEYS.get(artifact_name, Path(artifact_name).stem)


def _approve_hint(gate_key: str) -> str:
    return f'specflow gate approve {gate_key} --as <你> --note "<结论>"'


def _restore_hint(artifact_name: str) -> str:
    return (
        f"specflow restore {artifact_name} 回滚，或 "
        f"specflow gate revise {Path(artifact_name).stem} 出新版本后重新审批"
    )


def _producer_hint(artifact_name: str) -> str:
    mapping = {
        "repo-profile.md": "/project-init",
        "prd.md": "/prd-input",
        "exploration.md": "/explore",
        "design.md": "/tech-design",
        "tasks.md": "/plan-task",
        "tdd-plan.md": "/add",
        "tdd-report.md": "/add",
        "qa-report.md": "/qa-validate",
        "archive-record.md": "/archive",
    }
    return mapping.get(artifact_name, "<对应阶段命令>")


# ---------------------------------------------------------------------- #
# 主入口
# ---------------------------------------------------------------------- #
def check(stage: Stage, store: ArtifactStore, state: State) -> GateResult:
    """按 `stage.requires` + 人门检查是否放行；返回人类可读原因与补齐命令。"""
    missing: list[str] = []
    reasons: list[str] = []
    details: dict[str, Any] = {}
    howtos: list[str] = []

    for requirement in stage.requires:
        name = requirement.artifact
        if name in ROOT_DOCS:
            if not _root_doc_exists(store, name):
                missing.append(f"缺少 {name}（仓库根）：先运行 /project-init 生成基线规则")
                reasons.append(f"{name}:missing")
                details[name] = {"status": None, "required": "exists"}
            continue

        artifact = store.load_or_none(name)
        if artifact is None:
            missing.append(
                f"缺少上游产物 {name}：{stage.command} 需要它，先运行 {_producer_hint(name)}"
            )
            reasons.append(f"{name}:missing")
            details[name] = {"status": None, "required": requirement.status or "exists"}
            howtos.append(f"specflow run \"{_producer_hint(name)}\"")
            continue

        required_status = requirement.status or "exists"
        details[name] = {"status": artifact.status, "required": required_status}

        # 1) 已批准产物的 hash 篡改检测（AC-10）
        recorded = (state.artifacts or {}).get(name) or {}
        recorded_status = recorded.get("status")
        recorded_hash = recorded.get("hash")
        if recorded_status == "approved" and recorded_hash and recorded_hash != artifact.hash:
            missing.append(
                f"{name} 正文 hash 与 state.json 记录不一致（{recorded_hash} ≠ {artifact.hash}），判定 tampered"
            )
            reasons.append(f"{name}:tampered")
            details[name]["tampered"] = True
            howtos.append(_restore_hint(name))

        # 2) front-matter 状态与 state 记录必须一致（07 §5.1 / §5.4）
        if requirement.status:
            if artifact.status != requirement.status:
                missing.append(
                    f"{name} 状态为 {artifact.status}（v{artifact.version}），需要 {requirement.status}"
                )
                reasons.append(f"{name}:status={artifact.status}(需 {requirement.status})")
                if requirement.status == "approved":
                    howtos.append(_approve_hint(_gate_key(name)))
            elif recorded_status and recorded_status != requirement.status:
                missing.append(
                    f"{name} front-matter 状态 {artifact.status} 与 state.json 记录 {recorded_status} 不一致"
                )
                reasons.append(f"{name}:state-status={recorded_status}(文件 {artifact.status})")

        # 3) 结构契约（章节 / 附录 / 结构断言）
        for issue in artifact_schema.validate(artifact):
            missing.append(f"{name} 未通过产物校验：{issue}")
            reasons.append(f"{name}:schema")
        if artifact_schema.validate(artifact):
            howtos.append(f"修正 {name} 的缺失章节/字段后重新运行 {_producer_hint(name)}")

    # 人门
    if stage.human_gate == _BLOCKING_GATE:
        gate = state.gates.get(_BLOCKING_GATE)
        status = gate.status if isinstance(gate, GateState) else "pending"
        if status != "approved":
            missing.append(f"design 尚未 approve（当前 {status}）")
            reasons.append(f"gate:design={status}(需 approved)")
            details.setdefault("gates", {})[_BLOCKING_GATE] = {
                "status": status,
                "required": "approved",
            }
            howtos.append(_approve_hint(_BLOCKING_GATE))
        _check_design_artifact(store, state, details, missing, reasons, howtos)
    elif stage.human_gate and stage.human_gate not in _NON_BLOCKING_GATES:
        gate = state.gates.get(stage.human_gate)
        status = gate.status if isinstance(gate, GateState) else "pending"
        if status != "approved":
            missing.append(f"{stage.human_gate} 尚未 approve（当前 {status}）")
            reasons.append(f"gate:{stage.human_gate}={status}(需 approved)")
            details.setdefault("gates", {})[stage.human_gate] = {
                "status": status,
                "required": "approved",
            }
            howtos.append(_approve_hint(stage.human_gate))
    elif stage.human_gate == "agents-md":
        gate = state.gates.get("agents-md")
        details.setdefault("gates", {})["agents-md"] = {
            "status": gate.status if isinstance(gate, GateState) else "pending",
            "required": "approved",
            "blocking": False,
        }

    return GateResult(
        passed=not missing,
        missing=_dedup(missing),
        reasons=_dedup(reasons),
        howto="；".join(_dedup(howtos)),
        details=details,
    )


def _check_design_artifact(
    store: ArtifactStore,
    state: State,
    details: dict[str, Any],
    missing: list[str],
    reasons: list[str],
    howtos: list[str],
) -> None:
    """`design` 门要求 design.md 存在/approved/hash 一致/schema 通过（07 §5.4）。"""
    name = "design.md"
    already = name in details
    artifact = store.load_or_none(name)
    if artifact is None:
        if not already:
            missing.append("design 门要求 design.md 存在（尚未生成）")
            reasons.append(f"{name}:missing")
            howtos.append('specflow run "/tech-design"')
        return
    if not already:
        details[name] = {"status": artifact.status, "required": "approved"}
        if artifact.status != "approved":
            missing.append(f"design.md 状态为 {artifact.status}，design 门要求 approved")
            reasons.append(f"{name}:status={artifact.status}(需 approved)")
            howtos.append(_approve_hint("design"))
        recorded = (state.artifacts or {}).get(name) or {}
        if recorded.get("hash") and recorded["hash"] != artifact.hash:
            missing.append(f"{name} 正文 hash 与 state.json 记录不一致，判定 tampered")
            reasons.append(f"{name}:tampered")
            howtos.append(_restore_hint(name))
        for issue in artifact_schema.validate(artifact):
            missing.append(f"{name} 未通过产物校验：{issue}")
            reasons.append(f"{name}:schema")


def check_artifact(name: str, store: ArtifactStore, state: State) -> list[str]:
    """单产物校验：存在 → 已批准则 hash 一致（否则 tampered）→ schema。"""
    artifact = store.load_or_none(name)
    if artifact is None:
        return [f"{name}:missing"]
    issues: list[str] = []
    recorded = (state.artifacts or {}).get(name) or {}
    if recorded.get("status") == "approved" and recorded.get("hash") and recorded["hash"] != artifact.hash:
        issues.append(f"{name}:tampered")
    for issue in artifact_schema.validate(artifact):
        issues.append(f"{name}: {issue}")
    return issues


# ---------------------------------------------------------------------- #
# 上下文摘要
# ---------------------------------------------------------------------- #
def summarize_upstream(stage: Stage, store: ArtifactStore, budget: int) -> str:
    """只读 front-matter summary + 章节锚点，≤budget tokens（用于注入）。"""
    lines: list[str] = []
    for requirement in stage.requires:
        name = requirement.artifact
        if name in ROOT_DOCS:
            if _root_doc_exists(store, name):
                lines.append(f"- {name}（仓库根）：根文件，需要细节时直接读取。")
            continue
        artifact = store.load_or_none(name)
        if artifact is None:
            continue
        summary = artifact.summary() or "（未写 summary）"
        lines.append(f"- {name}（{artifact.status} v{artifact.version}）：{summary}")
        toc = [str(item) for item in (artifact.meta.get("toc") or []) if str(item).strip()]
        if toc:
            lines.append(f"  章节：{'、'.join(toc[:6])}")
            anchors = "、".join(f"{_doc_ref(artifact)}#{_anchor(title)}" for title in toc[:2])
            lines.append(f"  锚点：{anchors}")
    if not lines:
        return "（无上游产物摘要）"
    text = "上游产物摘要（细节按锚点读取，勿臆造）：\n" + "\n".join(lines)
    text, _truncated = truncate_by_tokens(text, max(1, int(budget)))
    return text


def _doc_ref(artifact: Artifact) -> str:
    # ArtifactStore 的路径是 <spec_dir>/artifacts/<name>，据此还原注入里的相对锚点
    spec_name = artifact.path.parent.parent.name or ".spec"
    return f"{spec_name}/artifacts/{artifact.name}"


def _anchor(title: str) -> str:
    return re.sub(r"\s+", "-", title.strip())


# ---------------------------------------------------------------------- #
# 内部
# ---------------------------------------------------------------------- #
def _root_doc_exists(store: ArtifactStore, name: str) -> bool:
    try:
        project_dir = Path(store.settings.project_dir)
    except AttributeError:  # pragma: no cover - 防御：非标准 store
        return False
    return (project_dir / name).is_file()


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result