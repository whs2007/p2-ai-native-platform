"""索引：`.spec/index/artifacts.json` + 项目根 `index.md`（docs/07 §3.2 / §4.4）。

- `register_artifact`：产物落盘后登记（阶段/状态/版本/hash/mtime）；
- `register_file`：交付代码文件登记（task_id/stage/source/hash），供 PostToolUse 调用；
- `rebuild_markdown`：重建 `index.md` 三节表格（阶段产物 / 交付代码 / 最近需求）。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, TYPE_CHECKING

from specflow.artifacts.store import Artifact, ArtifactStore
from specflow.config import Settings
from specflow.util import (
    atomic_write_text,
    dump_json,
    load_json,
    now_iso,
    rel_posix,
    sha256_file,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注，避免 artifacts 反向 import engine
    from specflow.engine.state import State


def _iso_mtime(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        return now_iso()


def _cell(value: Any) -> str:
    """Markdown 表格单元格转义（竖线/换行会破坏表格）。"""
    return str(value if value is not None else "-").replace("|", "\\|").replace("\n", " ").strip() or "-"


class Indexer:
    """产物/代码文件索引的读写入口。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.index_path = Path(settings.artifacts_index_path)
        self.index_md_path = Path(settings.project_dir) / "index.md"

    # ------------------------------------------------------------------ #
    # artifacts.json
    # ------------------------------------------------------------------ #
    def load(self) -> dict[str, Any]:
        data = load_json(self.index_path, None)
        if not isinstance(data, dict):
            data = {}
        data.setdefault("schema_version", 1)
        data.setdefault("generated_at", now_iso())
        for key in ("artifacts", "files"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        return data

    def _save(self, data: dict[str, Any]) -> None:
        data["generated_at"] = now_iso()
        dump_json(self.index_path, data)

    def register_artifact(self, artifact: Artifact) -> None:
        data = self.load()
        data["artifacts"][artifact.name] = {
            "path": rel_posix(artifact.path, self.settings.project_dir),
            "stage": str(artifact.meta.get("stage") or ""),
            "status": artifact.status,
            "version": artifact.version,
            "hash": artifact.hash,
            "mtime": _iso_mtime(artifact.path),
        }
        self._save(data)

    def register_file(
        self,
        path: Path,
        *,
        task_id: str | None,
        stage: str | None,
        source: str = "executor",
    ) -> None:
        target = Path(path)
        if not target.is_absolute():
            target = Path(self.settings.project_dir) / target
        rel = rel_posix(target, self.settings.project_dir)
        data = self.load()
        data["files"][rel] = {
            "task_id": task_id,
            "stage": stage,
            "hash": sha256_file(target) if target.is_file() else "",
            "source": source,
            "mtime": _iso_mtime(target) if target.exists() else now_iso(),
        }
        self._save(data)

    def get_file(self, rel_path: str) -> dict | None:
        key = _normalize_rel(rel_path)
        entry = self.load()["files"].get(key)
        return dict(entry) if isinstance(entry, dict) else None

    # ------------------------------------------------------------------ #
    # index.md
    # ------------------------------------------------------------------ #
    def rebuild_markdown(self, store: ArtifactStore, state: "State") -> Path:
        data = self.load()
        lines: list[str] = [
            "# 项目知识索引",
            "",
            f"> 生成时间：{now_iso()} ｜ 阶段：{getattr(state, 'stage', '-')} ｜ "
            "由 specflow 维护，请勿手工编辑（重跑对应阶段或 `/archive` 会刷新）",
            "",
            "## 1 阶段产物",
            "",
            "| 产物 | 阶段 | 状态 | 版本 | 摘要 |",
            "|---|---|---|---|---|",
        ]
        artifacts = store.list()
        for artifact in artifacts:
            lines.append(
                "| {name} | {stage} | {status} | v{version} | {summary} |".format(
                    name=_cell(artifact.name),
                    stage=_cell(artifact.meta.get("stage") or "-"),
                    status=_cell(artifact.status),
                    version=artifact.version,
                    summary=_cell(artifact.summary()),
                )
            )
        if not artifacts:
            lines.append("| - | - | - | - | 尚无产物 |")

        lines.extend(["", "## 2 交付代码", "", "| 文件 | 任务 | 阶段 | 来源 |", "|---|---|---|---|"])
        files = data.get("files") or {}
        for rel in sorted(files):
            entry = files[rel] or {}
            lines.append(
                f"| {_cell(rel)} | {_cell(entry.get('task_id'))} | "
                f"{_cell(entry.get('stage'))} | {_cell(entry.get('source'))} |"
            )
        if not files:
            lines.append("| - | - | - | 尚未登记交付文件 |")

        lines.extend(["", "## 3 最近需求", ""])
        recent = _recent_rules(store)
        lines.extend(recent or ["- 尚无归档记录（S7 `/archive` 后出现）"])
        lines.append("")

        atomic_write_text(self.index_md_path, "\n".join(lines))
        return self.index_md_path


def _normalize_rel(rel_path: str) -> str:
    text = str(rel_path or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text


def _recent_rules(store: ArtifactStore) -> list[str]:
    """从 archive-record.md 的附录提取候选回灌规则（§3 最近需求）。"""
    archive = store.load_or_none("archive-record.md")
    if archive is None:
        return []
    appendix = archive.appendix() or {}
    rules = appendix.get("rules_proposed")
    lines: list[str] = []
    if isinstance(rules, list):
        for item in rules:
            if not isinstance(item, dict):
                continue
            rule_id = str(item.get("id") or "").strip()
            text = str(item.get("text") or "").strip()
            source = str(item.get("source") or "").strip()
            if not rule_id and not text:
                continue
            suffix = f"（来源：{source}）" if source else ""
            lines.append(f"- {rule_id} {text}{suffix}".strip())
    reusable = appendix.get("reusable")
    if isinstance(reusable, list):
        for item in reusable:
            lines.append(f"- 复用：{item}")
    return lines