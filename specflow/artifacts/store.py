"""产物读写：front-matter + 正文、版本 / 备份 / 回滚（docs/07 §4.2、ADR-006/008）。

约定：
- `Artifact.body` 是**规范化正文**（`\n` 换行、去掉尾部空行）；落盘时由
  `frontmatter.render` 补单个换行。`hash` 只对这份正文算 sha256 前 12 位，
  因此读取前后 hash 可比。
- 每次 `save` 默认版本 +1，旧内容先备份到 `.spec/backup/<stem>.v<old>.md`；
  `id`/`created_at` 首次生成后保留，`updated_at` 每次刷新，`toc` 未给出时自动生成。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from specflow.artifacts import ARTIFACT_STATUSES
from specflow.artifacts.frontmatter import generate_toc, parse, render
from specflow.config import Settings
from specflow.errors import ArtifactError, ArtifactNotFound, UsageError
from specflow.util import (
    atomic_write_text,
    new_artifact_id,
    now_iso,
    read_text,
    sha256_12,
)

_NUMBERED_PREFIX = re.compile(r"^[\s\d.、,，:：)）\-]+")
_APPENDIX_TITLE = "附录"


def _normalize_body(body: str) -> str:
    return (body or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def strip_numbering(title: str) -> str:
    """`"1 选型对比"` → `"选型对比"`（忽略编号/空格差异用）。"""
    return _NUMBERED_PREFIX.sub("", title.strip()).strip()


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", "", strip_numbering(title))


def _derive_summary(body: str, limit: int = 80) -> str:
    """没有 meta.summary 时，从正文第一段非标题文本派生（只用于展示）。"""
    in_fence = False
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped:
            continue
        if stripped.startswith(("#", ">", "|", "-", "*", "`")):
            continue
        text = re.sub(r"\s+", " ", stripped)
        return text[:limit]
    return ""


@dataclass
class Artifact:
    """单个产物的内存表示（fields 见 docs/07 §4.2）。"""

    name: str
    path: Path
    meta: dict[str, Any]
    body: str

    @property
    def version(self) -> int:
        try:
            return int(self.meta.get("version") or 1)
        except (TypeError, ValueError):
            return 1

    @property
    def status(self) -> str:
        return str(self.meta.get("status") or "draft")

    @property
    def hash(self) -> str:
        return sha256_12(_normalize_body(self.body))

    def summary(self) -> str:
        return str(self.meta.get("summary") or "")

    def sections(self) -> list[str]:
        """所有 `## ` 标题（去掉前缀序号；含结构化附录标题）。"""
        return [strip_numbering(line[3:]) for line in self.body.split("\n") if line.startswith("## ")]

    def has_section(self, title: str) -> bool:
        target = _normalize_title(title)
        return any(_normalize_title(item) == target for item in self.sections())

    def section_body(self, title: str) -> str | None:
        """返回章节正文（不含标题行，去掉首尾空行）；章节不存在返回 None。"""
        target = _normalize_title(title)
        lines = self.body.split("\n")
        start: int | None = None
        for index, line in enumerate(lines):
            if line.startswith("## ") and _normalize_title(line[3:]) == target:
                start = index + 1
                break
        if start is None:
            return None
        collected: list[str] = []
        for line in lines[start:]:
            if line.startswith("## "):
                break
            collected.append(line)
        return "\n".join(collected).strip("\n")

    def appendix(self) -> dict[str, Any] | None:
        """解析正文末尾 `## 附录：结构化数据` 的 ```json 块；失败返回 None。"""
        lines = self.body.split("\n")
        heading: int | None = None
        for index, line in enumerate(lines):
            if line.startswith("## ") and _APPENDIX_TITLE in line:
                heading = index
                break
        if heading is None:
            return None
        fence_start: int | None = None
        for index in range(heading + 1, len(lines)):
            if lines[index].strip().startswith("```"):
                fence_start = index
                break
        if fence_start is None:
            return None
        payload: list[str] = []
        for line in lines[fence_start + 1 :]:
            if line.strip().startswith("```"):
                break
            payload.append(line)
        else:
            return None
        try:
            data = json.loads("\n".join(payload))
        except (json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None


class ArtifactStore:
    """`.spec/artifacts/` 的读写入口；所有落盘走原子写。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.artifacts_dir = Path(settings.artifacts_dir)
        self.backup_dir = Path(settings.backup_dir)

    # ------------------------------------------------------------------ #
    # 路径与读取
    # ------------------------------------------------------------------ #
    def path_of(self, name: str) -> Path:
        base = self.artifacts_dir.resolve()
        candidate = (self.artifacts_dir / name).resolve()
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise UsageError(f"非法产物名：{name}（不允许越出 .spec/artifacts）") from exc
        return self.artifacts_dir / name

    def exists(self, name: str) -> bool:
        return self.path_of(name).is_file()

    def load(self, name: str) -> Artifact:
        path = self.path_of(name)
        if not path.is_file():
            raise ArtifactNotFound(f"产物不存在：{name}（{path}）")
        try:
            meta, body = parse(read_text(path))
        except ArtifactError:
            raise
        except OSError as exc:  # pragma: no cover - 权限等异常
            raise ArtifactError(f"产物读取失败：{name}（{exc}）") from exc
        return Artifact(name=name, path=path, meta=meta, body=_normalize_body(body))

    def load_or_none(self, name: str) -> Artifact | None:
        try:
            return self.load(name)
        except ArtifactNotFound:
            return None

    def list(self) -> list[Artifact]:
        if not self.artifacts_dir.is_dir():
            return []
        artifacts: list[Artifact] = []
        for path in sorted(self.artifacts_dir.glob("*.md")):
            try:
                artifacts.append(self.load(path.name))
            except ArtifactError:
                continue  # 非产物文件（front-matter 非法）不纳入列表
        artifacts.sort(key=lambda item: (str(item.meta.get("stage") or ""), item.name))
        return artifacts

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def save(
        self,
        name: str,
        body: str,
        *,
        meta: Mapping[str, Any] | None = None,
        status: str | None = None,
        stage: str | None = None,
        owner: str | None = None,
        bump_version: bool = True,
    ) -> Artifact:
        normalized = _normalize_body(body)
        old = self.load_or_none(name)
        old_meta: dict[str, Any] = dict(old.meta) if old else {}
        provided: dict[str, Any] = dict(meta or {})
        merged: dict[str, Any] = {**old_meta, **provided}

        new_status = status or (old.status if old else None) or str(merged.get("status") or "") or "draft"
        if new_status not in ARTIFACT_STATUSES:
            raise ArtifactError(
                f"非法的产物状态：{new_status!r}（可选：{'/'.join(ARTIFACT_STATUSES)}）"
            )
        new_stage = stage or str(old_meta.get("stage") or "") or str(merged.get("stage") or "")

        if old is not None and bump_version:
            self._backup(old)
            new_version = old.version + 1
        else:
            new_version = old.version if old is not None else 1

        merged["id"] = str(old_meta.get("id") or merged.get("id") or self._new_id(name))
        merged["name"] = name
        merged["stage"] = new_stage
        merged["version"] = new_version
        merged["status"] = new_status
        merged["owner"] = owner or str(old_meta.get("owner") or merged.get("owner") or self.settings.owner)
        merged["created_at"] = str(old_meta.get("created_at") or merged.get("created_at") or now_iso())
        merged["updated_at"] = now_iso()

        if "toc" in provided:
            toc = provided["toc"] or generate_toc(normalized)
        elif old is not None and normalized == _normalize_body(old.body) and old_meta.get("toc"):
            toc = old_meta["toc"]
        else:
            toc = generate_toc(normalized)
        merged["toc"] = list(toc)

        if not str(merged.get("summary") or "").strip():
            merged["summary"] = _derive_summary(normalized)

        merged["hash"] = sha256_12(normalized)
        atomic_write_text(self.path_of(name), render(merged, normalized))
        return Artifact(name=name, path=self.path_of(name), meta=merged, body=normalized)

    def set_status(self, name: str, status: str, *, owner: str | None = None) -> Artifact:
        """只改状态，不改正文、不产生新版本（正文变更必须走 save/revise）。"""
        if status not in ARTIFACT_STATUSES:
            raise ArtifactError(
                f"非法的产物状态：{status!r}（可选：{'/'.join(ARTIFACT_STATUSES)}）"
            )
        artifact = self.load(name)
        return self.save(
            name,
            artifact.body,
            meta=dict(artifact.meta),
            status=status,
            stage=str(artifact.meta.get("stage") or ""),
            owner=owner or str(artifact.meta.get("owner") or ""),
            bump_version=False,
        )

    def remove(self, name: str) -> None:
        """仅测试 / 重置用：删除当前版本文件（保留备份）。"""
        try:
            self.path_of(name).unlink()
        except FileNotFoundError:
            return
        except OSError as exc:  # pragma: no cover
            raise ArtifactError(f"删除产物失败：{name}（{exc}）") from exc

    # ------------------------------------------------------------------ #
    # 版本 / 回滚
    # ------------------------------------------------------------------ #
    def versions(self, name: str) -> list[Path]:
        """`.spec/backup/<stem>.v<n>.md`，按版本号升序。"""
        stem = Path(name).stem
        pattern = re.compile(rf"^{re.escape(stem)}\.v(\d{{1,4}})\.md$")
        versions: list[tuple[int, Path]] = []
        if not self.backup_dir.is_dir():
            return []
        for path in self.backup_dir.glob(f"{stem}.v*.md"):
            match = pattern.match(path.name)
            if match:
                versions.append((int(match.group(1)), path))
        versions.sort(key=lambda item: item[0])
        return [path for _, path in versions]

    def restore(self, name: str, version: int | None = None) -> Artifact:
        """从备份回滚；不指定 version 时回滚到最新备份。"""
        backups = self.versions(name)
        if not backups:
            raise ArtifactNotFound(f"没有可回滚的备份：{name}（.spec/backup/ 为空）")
        if version is None:
            target = backups[-1]
        else:
            matched = [path for path in backups if path.name == f"{Path(name).stem}.v{int(version)}.md"]
            if not matched:
                raise ArtifactNotFound(f"备份版本不存在：{name} v{version}")
            target = matched[0]

        backup_meta, backup_body = parse(read_text(target))
        normalized = _normalize_body(backup_body)
        current = self.load_or_none(name)
        if current is not None:
            self._backup(current)  # 回滚前也留一份，避免误操作丢现场

        meta: dict[str, Any] = dict(backup_meta)
        meta["name"] = name
        if current is not None:
            meta["id"] = current.meta.get("id") or meta.get("id")
            meta["created_at"] = current.meta.get("created_at") or meta.get("created_at")
        meta["updated_at"] = now_iso()
        meta["hash"] = sha256_12(normalized)
        meta.setdefault("toc", generate_toc(normalized))
        if not str(meta.get("summary") or "").strip():
            meta["summary"] = _derive_summary(normalized)
        atomic_write_text(self.path_of(name), render(meta, normalized))
        return Artifact(name=name, path=self.path_of(name), meta=meta, body=normalized)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _backup(self, artifact: Artifact) -> Path:
        target = self.backup_dir / f"{Path(artifact.name).stem}.v{artifact.version}.md"
        atomic_write_text(target, render(artifact.meta, artifact.body))
        return target

    def _new_id(self, name: str) -> str:
        prefix = Path(name).stem.upper()
        existing = [str(item.meta.get("id") or "") for item in self.list()]
        return new_artifact_id(prefix, existing)