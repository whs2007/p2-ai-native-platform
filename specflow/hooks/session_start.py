"""SessionStart hook：注入基线规则 + 模板索引 + 当前阶段/门控（docs/07 §5.6、docs/04 §5.2）。

预算：`settings.session_budget`（默认 900 token 估算）。超限按 `util.truncate_by_tokens`
截断并追加「已截断」标注；AGENTS.md 缺失/无 §2 时降级为内置最小规则 + 告警，**不阻断**（exit 0）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from specflow.hooks import _support
from specflow.hooks.protocol import HookInput, HookOutcome, inject
from specflow.util import estimate_tokens, read_text, truncate_by_tokens

MAX_INJECTED_RULES = 20

_MINIMAL_RULES: tuple[str, ...] = (
    "R-00 只改当前任务声明（tasks.md files[]）的文件；越界先更新任务并重新门控",
    "R-01 危险命令（rm -rf /、git reset --hard、sudo、curl|sh 等）一律拒绝，按替代方案执行",
    "R-02 文件读写用 pathlib + encoding=\"utf-8\"，不依赖默认编码",
    "R-03 先写测试再写实现（test_first=true），测试至少含 1 条边界用例",
    "R-04 阶段产物写 .spec/artifacts/，不得写仓库根；已批准产物不可直改",
    "R-05 业务判断以 domain.md/AGENTS.md 为准；缺失时先问，不编造术语",
)

_TEMPLATE_HINTS: dict[str, str] = {
    "prd": "PRD 结构（背景/需求/验收/范围外）",
    "design": "选型对比 / 改动 / 风险与回滚",
    "tasks": "任务拆解表（files/test_first/estimate_h）",
    "test_unit": "测试骨架（命名/边界用例）",
    "qa_report": "QA 三段证据（单测/pipeline/rpc）",
    "tdd_plan": "TDD 步骤计划",
    "archive_record": "归档与候选回灌规则",
}

_HEADING_RE = re.compile(r"^(#{2,6})\s+(.*?)\s*$")
_VERSION_RE = re.compile(r"版本\s*[:：]\s*(v?\d+(?:\.\d+)*)")


# ---------------------------------------------------------------------- #
# AGENTS.md
# ---------------------------------------------------------------------- #
def _read_agents_rules(path: Path) -> tuple[list[str], str | None]:
    """读 AGENTS.md 的「§2 工程规则」条目，返回 (规则列表, 版本号)。"""
    try:
        text = read_text(path)
    except (FileNotFoundError, NotADirectoryError, PermissionError, UnicodeDecodeError):
        return [], None
    version_match = _VERSION_RE.search(text)
    version: str | None = None
    if version_match:
        raw = version_match.group(1)
        version = raw if raw.lower().startswith("v") else f"v{raw}"

    lines = text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        heading = _HEADING_RE.match(line.strip())
        if not heading:
            continue
        title = heading.group(2).strip()
        if re.match(r"^2(?:[.、．:：\s]|$)", title) or "工程规则" in title:
            start = index + 1
            break
    if start is None:
        return [], version

    rules: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        if _HEADING_RE.match(stripped):
            break
        if stripped.startswith(("- ", "* ")):
            item = stripped[2:].strip()
            if item:
                rules.append(item)
    return rules[:MAX_INJECTED_RULES], version


# ---------------------------------------------------------------------- #
# 模板索引
# ---------------------------------------------------------------------- #
def _template_index(settings: Any, project_dir: Path) -> tuple[list[str], str | None]:
    candidates = [project_dir / "templates", Path(settings.templates_dir)]
    files: list[Path] = []
    for base in candidates:
        try:
            if base.is_dir():
                files = sorted(
                    path
                    for path in list(base.glob("*.md")) + list(base.glob("*.py"))
                    if path.is_file()
                )
        except OSError:  # pragma: no cover - 权限异常
            files = []
        if files:
            break
    if not files:
        return [], "未找到 templates/ 模板目录：先执行 /project-init 提取模板（本次注入不含模板索引）。"
    lines = [
        f"- templates/{path.name}（{_TEMPLATE_HINTS.get(path.stem, '模板')}）"
        for path in files[:12]
    ]
    return lines, None


# ---------------------------------------------------------------------- #
# 当前阶段 / 门控
# ---------------------------------------------------------------------- #
def stage_status_line(state: dict[str, Any], settings: Any) -> tuple[str, dict[str, Any]]:
    """一行阶段/门控摘要，供 SessionStart 与 UserPromptSubmit 共用。"""
    defs_by_id = _support.stage_defs_by_id(settings)
    stage_id = str(state.get("stage") or "S0A")
    defn = defs_by_id.get(stage_id) or {}
    title = str(defn.get("title") or "").strip()
    bits: list[str] = [f"{stage_id} {title}".strip()]
    gate = defn.get("human_gate")
    if gate:
        bits.append(f"{gate} 门={_support.gate_status(state, str(gate))}")
    following = _support.next_command(defs_by_id, stage_id)
    if following:
        bits.append(f"下一步 {following}")
    line = "｜".join(bits)

    approved = sorted(_support.approved_artifacts(state))
    anchor_bits: list[str] = []
    if approved:
        anchor_bits.append("、".join(f".spec/artifacts/{name}" for name in approved[-3:]))
    anchor_bits.append("AGENTS.md#2")
    info: dict[str, Any] = {
        "stage": stage_id,
        "gate": _support.gate_status(state, str(gate)) if gate else None,
        "needs_human": bool(state.get("needs_human")),
    }
    return line, {"anchors": anchor_bits, **info}


def _stage_section(state: dict[str, Any], settings: Any) -> tuple[list[str], dict[str, Any]]:
    line, info = stage_status_line(state, settings)
    anchors = "、".join(f"`{item}`" for item in info.pop("anchors", []))
    return [line, f"需要细节时读锚点：{anchors}"], info


# ---------------------------------------------------------------------- #
# handler
# ---------------------------------------------------------------------- #
def handle(payload: HookInput, settings: Any) -> HookOutcome:
    """SessionStart：注入规则/模板/阶段，永不阻断（exit 0）。"""
    project_dir = Path(payload.cwd or settings.project_dir)
    budget = max(1, int(getattr(settings, "session_budget", 900) or 900))
    sources: list[str] = []
    warnings: list[str] = []
    degraded = False

    agents_path = project_dir / "AGENTS.md"
    rules, version = _read_agents_rules(agents_path)
    if rules:
        sources.append("AGENTS.md#2")
    else:
        degraded = True
        rules = list(_MINIMAL_RULES)
        sources.append("hooks.session_start#min-rules")
        warnings.append(
            "AGENTS.md 缺失或缺少「§2 工程规则」：已降级为内置最小规则；"
            "请先执行 `specflow init` 或 /project-init 生成本仓库规则。"
        )

    template_lines, template_warn = _template_index(settings, project_dir)
    if template_lines:
        sources.append("templates/index")
    if template_warn:
        warnings.append(template_warn)

    state = _support.load_state_dict(settings)
    stage_lines, stage_info = _stage_section(state, settings)
    sources.append(".spec/state.json")
    if stage_info.get("needs_human"):
        warnings.append("state.needs_human=true：TDD 已超限，需人工介入后再继续自动化流程。")

    if degraded:
        rule_heading = (
            f"## 基线规则（内置最小规则 {len(rules)} 条，AGENTS.md 缺失或缺 §2）"
        )
    elif len(rules) >= MAX_INJECTED_RULES:
        rule_heading = (
            f"## 基线规则（AGENTS.md {version or 'v?'}，节选前 {MAX_INJECTED_RULES} 条）"
        )
    else:
        rule_heading = f"## 基线规则（AGENTS.md {version or 'v?'}，节选）"

    sections: list[str] = [rule_heading]
    sections.extend(f"- {rule}" for rule in rules[:MAX_INJECTED_RULES])
    sections.append("")
    sections.append("## 模板索引")
    sections.extend(template_lines or ["- （无模板，先执行 /project-init 提取）"])
    sections.append("")
    sections.append("## 当前阶段")
    sections.extend(stage_lines)
    if warnings:
        sections.append("")
        sections.append("## 告警")
        sections.extend(f"- {item}" for item in warnings)

    context = "\n".join(sections).strip()
    context, truncated = truncate_by_tokens(context, budget)
    if truncated:
        warnings.append(f"注入内容超出 session_budget={budget}，已截断。")

    tokens = estimate_tokens(context)
    meta: dict[str, Any] = {
        "stage": stage_info.get("stage"),
        "context_tokens": tokens,
        "sources": sources,
        "degraded": degraded,
        "truncated": truncated,
    }
    if stage_info.get("gate"):
        meta["gate"] = stage_info["gate"]
    if warnings:
        meta["warnings"] = warnings
    return inject("SessionStart", context, meta=meta)