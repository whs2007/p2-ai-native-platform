"""人工门控动作：approve / reject / supersede / revise + 规则回灌（docs/07 §5.4 / §5.10）。

- `design`：阻断式人门，批准后 hash 锁定，正文变化判 tampered；
- `agents-md`：把 archive-record 的候选规则回灌进 AGENTS.md（人工批准才生效）；
- 其它产物名（tdd-report.md / qa-report.md 等）走通用批准/打回，便于评审留痕。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from specflow.artifacts import schema as artifact_schema
from specflow.artifacts.feedback import apply_rules
from specflow.artifacts.store import ArtifactStore
from specflow.config import Settings
from specflow.engine.state import StateStore
from specflow.errors import ArtifactNotFound, GateBlocked, UsageError
from specflow.util import atomic_write_text, now_iso, read_text

_BLOCKING_GATES = {"design"}
_GATE_ARTIFACTS = {
    "design": "design.md",
    "tdd": "tdd-report.md",
    "qa": "qa-report.md",
}


def gate_action(
    settings: Settings,
    *,
    action: str,
    name: str,
    by: str,
    note: str | None = None,
) -> dict[str, Any]:
    if action not in {"approve", "reject", "supersede", "revise"}:
        raise UsageError(f"未知门控动作：{action}")

    key = name[:-3] if name.endswith(".md") else name
    if key == "agents-md" or name == "AGENTS.md":
        return _agents_md_action(settings, action=action, by=by, note=note)

    artifact_name = _GATE_ARTIFACTS.get(key, name if name.endswith(".md") else f"{name}.md")
    store = ArtifactStore(settings)
    state_store = StateStore(settings.state_path)
    state = state_store.load()
    try:
        artifact = store.load(artifact_name)
    except ArtifactNotFound as exc:
        raise GateBlocked(
            f"产物不存在：{artifact_name}",
            missing=[f"{artifact_name} 未生成"],
            howto="先运行对应阶段命令生成产物。",
        ) from exc

    gate_key = key if key in _GATE_ARTIFACTS else Path(artifact_name).stem

    if action == "approve":
        _assert_not_tampered(artifact, state)
        problems = artifact_schema.validate(artifact)
        if problems:
            raise GateBlocked(
                f"{artifact_name} 未通过产物校验，不能批准。",
                missing=problems,
                howto="修正缺失章节/字段后重新生成或 gate revise。",
            )
        artifact = store.set_status(artifact_name, "approved", owner=by)
        state.record_artifact(
            artifact.name, artifact.version, artifact.hash, artifact.status, artifact.meta.get("stage")
        )
        state.record_gate(gate_key, "approved", by=by, note=note)
        state_store.save(state)
        return {
            "summary": f"[specflow] {artifact_name} v{artifact.version} 已批准（by {by}，hash {artifact.hash}）"
            + (f"\n  说明：{note}" if note else "")
        }

    if action == "reject":
        artifact = store.set_status(artifact_name, "draft", owner=by)
        state.record_artifact(
            artifact.name, artifact.version, artifact.hash, artifact.status, artifact.meta.get("stage")
        )
        state.record_gate(gate_key, "rejected", by=by, note=note)
        state_store.save(state)
        return {
            "summary": f"[specflow] {artifact_name} 已打回 draft（by {by}）"
            + (f"\n  打回意见：{note}（下次 /tech-design 会注入）" if note else "")
        }

    if action == "revise":
        revised = store.save(
            artifact_name,
            artifact.body,
            meta=dict(artifact.meta),
            status="draft",
            stage=artifact.meta.get("stage"),
            owner=by,
        )
        state.record_artifact(
            revised.name, revised.version, revised.hash, revised.status, revised.meta.get("stage")
        )
        state.record_gate(gate_key, "pending", by=by, note=note)
        state_store.save(state)
        return {
            "summary": f"[specflow] {artifact_name} 已生成新版本 v{revised.version}（draft），"
            f"需要重新 gate approve {gate_key}"
        }

    artifact = store.set_status(artifact_name, "superseded", owner=by)
    state.record_artifact(
        artifact.name, artifact.version, artifact.hash, artifact.status, artifact.meta.get("stage")
    )
    state.record_gate(gate_key, "superseded", by=by, note=note)
    state_store.save(state)
    return {"summary": f"[specflow] {artifact_name} 已标记 superseded（by {by}）"}


def _assert_not_tampered(artifact: Any, state: Any) -> None:
    recorded = (state.artifacts or {}).get(artifact.name)
    if not recorded:
        return
    recorded_hash = recorded.get("hash")
    if recorded_hash and recorded_hash != artifact.hash:
        raise GateBlocked(
            f"{artifact.name} 正文 hash 与 state.json 不一致（{recorded_hash} ≠ {artifact.hash}），判定为 tampered。",
            missing=[f"{artifact.name} 已被手工改写"],
            howto=f"specflow restore {artifact.name} 回滚，或 specflow gate revise {Path(artifact.name).stem} 出新版本。",
        )


def _agents_md_action(settings: Settings, *, action: str, by: str, note: str | None) -> dict[str, Any]:
    state_store = StateStore(settings.state_path)
    state = state_store.load()
    if action == "reject":
        state.record_gate("agents-md", "rejected", by=by, note=note)
        state_store.save(state)
        return {"summary": f"[specflow] agents-md 回灌已拒绝（候选规则保留在 archive-record.md）"}

    if action != "approve":
        raise UsageError("agents-md 只支持 approve / reject")

    store = ArtifactStore(settings)
    try:
        archive = store.load("archive-record.md")
    except ArtifactNotFound as exc:
        raise GateBlocked(
            "尚未生成 archive-record.md，无候选规则可回灌。",
            missing=["archive-record.md 不存在"],
            howto='先执行 specflow run "/archive"。',
        ) from exc
    appendix = archive.appendix() or {}
    proposals = list(appendix.get("rules_proposed") or [])
    agents_path = settings.project_dir / "AGENTS.md"
    if not agents_path.exists():
        raise GateBlocked(
            "AGENTS.md 不存在，无法回灌。",
            missing=["AGENTS.md 缺失"],
            howto='先执行 specflow run "/project-init" 生成基线规则。',
        )
    old_text = read_text(agents_path)
    old_version = _parse_version(old_text)
    backup_path = settings.backup_dir / f"AGENTS.{old_version}.md"
    atomic_write_text(backup_path, old_text)
    if proposals:
        result = apply_rules(
            agents_path,
            proposals,
            owner=by,
            source_req=settings.project_dir.name,
            backup_path=backup_path,
        )
        added = "、".join(result.get("added") or []) or "（无新增）"
        skipped = len(result.get("skipped_duplicate") or [])
        summary = (
            f"[specflow] 回灌完成：AGENTS.md → {result.get('version')}，新增 {added}"
            + (f"，跳过重复 {skipped} 条" if skipped else "")
        )
    else:
        summary = "[specflow] 没有候选规则，AGENTS.md 未变更"
    state.record_gate("agents-md", "approved", by=by, note=note)
    state_store.save(state)
    return {"summary": summary}


def _parse_version(text: str) -> str:
    match = re.search(r"版本：v(\d+)\.(\d+)", text or "")
    return f"v{match.group(1)}.{match.group(2)}" if match else "v0.1"