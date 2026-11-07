"""`specflow status`：7 阶段 / 门控 / 产物表格与 --check 校验（docs/07 §5.10）。"""
from __future__ import annotations

from typing import Any

from specflow.artifacts import schema as artifact_schema
from specflow.artifacts.store import ArtifactStore
from specflow.config import Settings
from specflow.engine.state import StateStore
from specflow.errors import SpecflowError

_STAGE_INDEX = {f"S{index}": index for index in range(8)}
_STAGE_INDEX["S0A"] = 0
_STAGE_INDEX["S0B"] = 1
_STAGE_INDEX["S1"] = 2
_STAGE_INDEX["S2"] = 3
_STAGE_INDEX["S3"] = 4
_STAGE_INDEX["S4"] = 5
_STAGE_INDEX["S5"] = 6
_STAGE_INDEX["S6"] = 7
_STAGE_INDEX["S7"] = 8

_GATE_LABELS = {"design": "方案批准（design）", "agents-md": "规则回灌（agents-md）"}


def collect_status(settings: Settings, *, check: bool = False) -> dict[str, Any]:
    from specflow.engine.stages import load_stages

    problems: list[str] = []
    try:
        stages = load_stages(settings.skills_dir)
    except SpecflowError as exc:
        return {
            "ok": False,
            "stage": "?",
            "stages": [],
            "gates": {},
            "artifacts": [],
            "problems": [f"阶段定义加载失败：{exc}"],
        }
    store = ArtifactStore(settings)
    state = StateStore(settings.state_path).load()
    current_index = _STAGE_INDEX.get(state.stage, 0)

    rows: list[dict[str, Any]] = []
    for command, stage in stages.items():
        stage_index = _STAGE_INDEX.get(stage.id, 0)
        produced: list[dict[str, Any]] = []
        for name in stage.produces:
            if name in {"AGENTS.md", "domain.md", "index.md"}:
                exists = (settings.project_dir / name).exists()
                produced.append({"name": name, "exists": exists, "status": "-", "version": "-"})
                if stage_index <= current_index and not exists:
                    problems.append(f"{name} 缺失（{stage.id} 应产出）")
                continue
            if name.endswith("/") or name.startswith("templates"):
                exists = (settings.project_dir / "templates").exists()
                produced.append({"name": "templates/", "exists": exists, "status": "-", "version": "-"})
                continue
            if name not in artifact_schema.ARTIFACT_SCHEMAS:
                continue
            artifact = store.load_or_none(name)
            if artifact is None:
                produced.append({"name": name, "exists": False, "status": "-", "version": "-"})
                if stage_index <= current_index:
                    problems.append(f"{name} 缺失（{stage.id} 应产出）")
                continue
            item = {
                "name": name,
                "exists": True,
                "status": artifact.status,
                "version": artifact.version,
            }
            if stage_index <= current_index and artifact.status == "superseded":
                problems.append(f"{name} 已 superseded，需重新生成")
            if check:
                for issue in artifact_schema.validate(artifact):
                    problems.append(f"{name}: {issue}")
                recorded = (state.artifacts or {}).get(name) or {}
                if artifact.status == "approved" and recorded.get("hash") and recorded["hash"] != artifact.hash:
                    problems.append(f"{name}: tampered（state hash {recorded['hash']} ≠ 文件 {artifact.hash}）")
            produced.append(item)
        gate = None
        if stage.human_gate:
            gate_state = state.gates.get(stage.human_gate)
            gate = {
                "name": stage.human_gate,
                "status": gate_state.status if gate_state else "pending",
                "by": gate_state.by if gate_state else None,
                "note": gate_state.note if gate_state else None,
            }
            if check and stage.human_gate in {"design"} and stage_index <= current_index:
                if gate["status"] != "approved":
                    problems.append(f"门控 {stage.human_gate} 尚未 approved（当前 {gate['status']}）")
        rows.append(
            {
                "stage": stage.id,
                "command": command,
                "title": stage.title,
                "current": stage.id == state.stage,
                "reached": stage_index <= current_index,
                "produced": produced,
                "gate": gate,
            }
        )

    artifact_rows: list[dict[str, Any]] = []
    for artifact in store.list():
        entry = {
            "name": artifact.name,
            "stage": artifact.meta.get("stage", "-"),
            "status": artifact.status,
            "version": artifact.version,
            "hash": artifact.hash,
            "summary": artifact.summary(),
        }
        if check:
            entry["issues"] = artifact_schema.validate(artifact)
            if entry["issues"]:
                problems.extend(f"{artifact.name}: {issue}" for issue in entry["issues"])
            recorded = (state.artifacts or {}).get(artifact.name) or {}
            if recorded.get("hash") and recorded["hash"] != artifact.hash and artifact.status == "approved":
                problems.append(f"{artifact.name}: tampered")
        artifact_rows.append(entry)

    return {
        "ok": not problems,
        "stage": state.stage,
        "owner": state.owner,
        "needs_human": state.needs_human,
        "stages": rows,
        "gates": {
            name: {"status": gate.status, "by": gate.by, "note": gate.note}
            for name, gate in (state.gates or {}).items()
        },
        "artifacts": artifact_rows,
        "problems": problems,
        "checked": check,
    }


def render_status(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"specflow 阶段状态（当前 {report.get('stage', '?')}，owner {report.get('owner', '?')}）")
    lines.append("")
    lines.append("| 阶段 | 命令 | 状态 | 产物 | 门控 |")
    lines.append("|---|---|---|---|---|")
    for row in report.get("stages", []):
        mark = "->" if row["current"] else ("OK" if row["reached"] else ".")
        products = "、".join(
            f"{item['name']}({item.get('status', '-')} v{item.get('version', '-')})"
            if item.get("exists")
            else f"{item['name']}(缺失)"
            for item in row["produced"]
        ) or "-"
        gate = "-"
        if row.get("gate"):
            gate = f"{row['gate']['name']}={row['gate']['status']}"
            if row["gate"].get("by"):
                gate += f"({row['gate']['by']})"
        lines.append(f"| {mark} {row['stage']} | `{row['command']}` | {row['title']} | {products} | {gate} |")
    lines.append("")
    lines.append("产物明细：")
    if not report.get("artifacts"):
        lines.append("  （尚无产物）")
    for item in report.get("artifacts", []):
        lines.append(
            f"  - {item['name']:<22} {item['stage']:<4} {item['status']:<10} v{item['version']}  {item['summary'][:40]}"
        )
    if report.get("problems"):
        lines.append("")
        lines.append(f"校验问题（{len(report['problems'])} 条）：")
        for problem in report["problems"]:
            lines.append(f"  x {problem}")
    elif report.get("checked"):
        lines.append("")
        lines.append("校验：全部产物字段与 hash 一致 [OK]")
    if report.get("needs_human"):
        lines.append("")
        lines.append("注意：当前标记 needs_human，需人工修复后续跑。")
    return "\n".join(lines)