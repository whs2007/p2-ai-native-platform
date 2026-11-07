"""PostToolUse hook：Write/Edit 后登记产物/文件并刷新索引（docs/07 §5.6、docs/04 §5.5）。

原则：**永不阻断**（exit 0）。任何登记失败都只写告警 + trace，不影响工具结果。
- `.spec/artifacts/*.md` → `Indexer.register_artifact`；
- 其它文件 → `Indexer.register_file`（task_id/stage 来自上下文，source 取角色）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping

from specflow.hooks import _support
from specflow.hooks.protocol import HookInput, HookOutcome, inject
from specflow.util import rel_posix

_WRITE_TOOLS = {
    "write", "edit", "multiedit", "multi_edit", "notebookedit", "apply_patch",
}
_PATH_KEYS = ("file_path", "path", "filepath", "target_file", "notebook_path")
_TASK_FLAG_RE = re.compile(r"--task[=\s]+(\S+)")
_INDEX_REL = ".spec/index/artifacts.json"


def _extract_path(tool_input: Mapping[str, Any]) -> str | None:
    for key in _PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _resolve(path_text: str, project_dir: Path) -> Path:
    target = Path(path_text).expanduser()
    if not target.is_absolute():
        target = project_dir / target
    try:
        return target.resolve()
    except OSError:  # pragma: no cover - 极端路径
        return target


def _is_artifact_path(path: Path, artifacts_dir: Path) -> bool:
    try:
        relative = path.relative_to(artifacts_dir.resolve())
    except (ValueError, OSError):
        return False
    return relative.suffix.lower() == ".md"


def _task_id_from_state(state: Mapping[str, Any]) -> str:
    match = _TASK_FLAG_RE.search(str(state.get("last_command") or ""))
    return match.group(1) if match else ""


def handle(payload: HookInput, settings: Any) -> HookOutcome:
    """登记写入结果并返回 additionalContext；任何异常都降级为告警（exit 0）。"""
    tool_name = (payload.tool_name or _first_text(payload.raw.get("tool_name"))).strip().lower()
    if tool_name not in _WRITE_TOOLS:
        return inject("PostToolUse", "", meta={"tracked": None, "index_updated": False})

    path_text = _extract_path(payload.tool_input or {})
    if not path_text:
        return inject(
            "PostToolUse", "",
            meta={"tracked": None, "index_updated": False, "warnings": ["写类工具未提供 file_path"]},
        )

    project_dir = Path(payload.cwd or settings.project_dir)
    path = _resolve(path_text, project_dir)
    rel = rel_posix(path, project_dir)
    artifacts_dir = Path(settings.artifacts_dir)

    state = _support.load_state_dict(settings)
    stage = str(state.get("stage") or "") or None
    role = _first_text(
        payload.raw.get("agent_role"),
        payload.raw.get("role"),
        os.environ.get("SPECFLOW_ROLE"),
    ).lower()
    source = role if role in {"test_writer", "executor"} else "executor"
    task_id = _first_text(
        payload.raw.get("task_id"),
        os.environ.get("SPECFLOW_TASK_ID"),
        _task_id_from_state(state),
    ) or None

    try:
        from specflow.artifacts.indexer import Indexer  # 延迟导入：T1 模块

        indexer = Indexer(settings)
        if _is_artifact_path(path, artifacts_dir):
            from specflow.artifacts.store import ArtifactStore

            artifact_name = rel_posix(path, artifacts_dir)
            store = ArtifactStore(settings)
            artifact = store.load_or_none(artifact_name)
            if artifact is None:
                raise FileNotFoundError(f"产物尚不可读：{artifact_name}")
            indexer.register_artifact(artifact)
            version = getattr(artifact, "version", None)
            status = getattr(artifact, "status", None)
            detail = f"v{version}，{status}" if version is not None else str(status or "")
            context = (
                f"产物已登记：{artifact_name}（{detail}），索引已刷新 {_INDEX_REL}。"
                if detail
                else f"产物已登记：{artifact_name}，索引已刷新 {_INDEX_REL}。"
            )
            tracked = {
                "path": rel,
                "artifact": artifact_name,
                "version": version,
                "status": status,
            }
        else:
            indexer.register_file(path, task_id=task_id, stage=stage, source=source)
            info: dict[str, Any] | None = None
            try:
                info = indexer.get_file(rel)
            except Exception:  # get_file 不可用时不影响登记
                info = None
            digest = str((info or {}).get("hash") or "")
            tracked = {"path": rel, "task_id": task_id, "hash": digest}
            context = f"产物已登记：{rel} → {task_id or '未绑定任务'}"
            if digest:
                context += f"（hash {digest}）"
            context += f"。索引已刷新 {_INDEX_REL}。"
        return inject("PostToolUse", context, meta={"tracked": tracked, "index_updated": True})
    except Exception as exc:  # 永不阻断：登记失败只告警
        return inject(
            "PostToolUse",
            f"索引刷新失败（不影响工具结果）：{exc}",
            meta={
                "tracked": None,
                "index_updated": False,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )