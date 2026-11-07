"""阶段定义：解析 `skills/<slug>/SKILL.md` 的 front-matter（docs/07 §5.1 / §5.2、ADR-002）。

`skills/` 是阶段元数据 + 提示词的**唯一事实源**；本模块只解析 front-matter，
正文（提示词模板）留给 `engine.runner._skill_prompt` 读取。
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from specflow.artifacts import ARTIFACT_STATUSES
from specflow.artifacts.frontmatter import parse as parse_frontmatter
from specflow.engine.state import STAGE_ORDER
from specflow.errors import SpecflowError

#: 2 个上下文初始化命令（S0A/S0B）：不属于 7 个主链路阶段
PREPARE_COMMANDS: tuple[str, ...] = (
    "/project-init",
    "/domain-input",
)

#: 7 个主链路阶段（S1–S7）
MAIN_PHASES: tuple[str, ...] = (
    "/prd-input",
    "/explore",
    "/tech-design",
    "/plan-task",
    "/add",
    "/qa-validate",
    "/archive",
)

#: 9 个固定阶段命令 = 2 个初始化 + 7 个主链路阶段（顺序即展示顺序）
REQUIRED_COMMANDS: tuple[str, ...] = PREPARE_COMMANDS + MAIN_PHASES

_DEFAULT_SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"
_STAGE_IDS = set(STAGE_ORDER)
#: {skills_dir: OrderedDict[command, Stage]}
_CACHE: dict[Path, "OrderedDict[str, Stage]"] = {}


# ---------------------------------------------------------------------- #
# 数据模型
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class Requirement:
    """上游产物要求：`artifact.md` 或 `artifact.md:approved`。"""

    artifact: str
    status: str | None = None

    def render(self) -> str:
        return f"{self.artifact}:{self.status}" if self.status else self.artifact


@dataclass(frozen=True)
class Stage:
    """单个阶段的完整定义（由 SKILL.md front-matter 解析而来）。"""

    id: str
    command: str
    slug: str
    title: str
    requires: tuple[Requirement, ...]
    produces: tuple[str, ...]
    human_gate: str | None
    max_context_tokens: int
    skill_path: Path
    next: str | None = None


# ---------------------------------------------------------------------- #
# 解析
# ---------------------------------------------------------------------- #
def load_stages(skills_dir: Path) -> "OrderedDict[str, Stage]":
    """解析 `skills_dir/*/SKILL.md`，返回 `{command: Stage}`（按 9 个固定命令排序）。"""
    root = Path(skills_dir)
    if not root.is_dir():
        raise SpecflowError(
            f"阶段定义目录不存在：{root}（需要 {len(REQUIRED_COMMANDS)} 个 <slug>/SKILL.md）"
        )
    skill_files = sorted(path for path in root.glob("*/SKILL.md") if path.is_file())
    if not skill_files:
        raise SpecflowError(f"阶段定义缺失：{root} 下没有任何 <slug>/SKILL.md")

    parsed: dict[str, Stage] = {}
    seen_ids: dict[str, Path] = {}
    for path in skill_files:
        stage = _parse_skill(path)
        if stage.command in parsed:
            raise SpecflowError(
                f"阶段命令重复：{stage.command}（{parsed[stage.command].skill_path} 与 {path}）"
            )
        if stage.id in seen_ids:
            raise SpecflowError(
                f"阶段 id 重复：{stage.id}（{seen_ids[stage.id]} 与 {path}）"
            )
        parsed[stage.command] = stage
        seen_ids[stage.id] = path

    missing = [command for command in REQUIRED_COMMANDS if command not in parsed]
    if missing:
        raise SpecflowError(
            f"阶段定义不完整：缺少 {'、'.join(missing)}（共需 {len(REQUIRED_COMMANDS)} 个阶段）"
        )

    ordered: "OrderedDict[str, Stage]" = OrderedDict()
    for command in REQUIRED_COMMANDS:
        ordered[command] = parsed[command]
    for command in sorted(set(parsed) - set(REQUIRED_COMMANDS)):
        ordered[command] = parsed[command]

    _CACHE[root.resolve()] = ordered
    return ordered


def _parse_skill(path: Path) -> Stage:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecflowError(f"阶段定义读取失败：{path}（{exc}）") from exc
    try:
        meta, _body = parse_frontmatter(text)
    except SpecflowError as exc:
        raise SpecflowError(f"阶段定义解析失败：{path}（{exc}）") from exc

    command = _require_text(meta, "command", path)
    if not command.startswith("/"):
        raise SpecflowError(f"{path}: command 必须以 '/' 开头（当前 {command!r}）")
    slug = command.lstrip("/")
    if not slug:
        raise SpecflowError(f"{path}: command 只有 '/'，缺少命令名")

    stage_id = _require_text(meta, "stage", path)
    if stage_id not in _STAGE_IDS:
        raise SpecflowError(f"{path}: 未知阶段 id {stage_id!r}（可选：{'/'.join(STAGE_ORDER)}）")

    next_stage = meta.get("next")
    if next_stage in (None, ""):
        next_stage = None
    elif str(next_stage) not in _STAGE_IDS:
        raise SpecflowError(f"{path}: next 指向未知阶段 {next_stage!r}")

    human_gate = meta.get("human_gate")
    human_gate = str(human_gate).strip() if human_gate not in (None, "") else None
    human_gate = human_gate or None

    try:
        max_tokens = int(meta.get("max_context_tokens") or 3000)
    except (TypeError, ValueError) as exc:
        raise SpecflowError(f"{path}: max_context_tokens 必须是整数") from exc
    if max_tokens <= 0:
        raise SpecflowError(f"{path}: max_context_tokens 必须为正数（当前 {max_tokens}）")

    return Stage(
        id=stage_id,
        command=command,
        slug=slug,
        title=_require_text(meta, "title", path),
        requires=_parse_requirements(meta.get("requires"), path),
        produces=_parse_text_list(meta.get("produces"), "produces", path),
        human_gate=human_gate,
        max_context_tokens=max_tokens,
        skill_path=path,
        next=str(next_stage) if next_stage else None,
    )


def _require_text(meta: Mapping[str, Any], key: str, path: Path) -> str:
    value = meta.get(key)
    text = str(value).strip() if value is not None else ""
    if not text:
        raise SpecflowError(f"{path}: front-matter 缺少必填字段 {key}")
    return text


def _parse_text_list(value: Any, key: str, path: Path) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, (list, tuple)):
        raise SpecflowError(f"{path}: {key} 必须是内联列表，如 [{key}: [a.md, b.md]]")
    items = tuple(str(item).strip() for item in value if str(item).strip())
    return items


def _parse_requirements(value: Any, path: Path) -> tuple[Requirement, ...]:
    requirements: list[Requirement] = []
    for raw in _parse_text_list(value, "requires", path):
        artifact, status = raw, None
        if ":" in raw:
            artifact, status = raw.split(":", 1)
            artifact = artifact.strip()
            status = status.strip() or None
        if not artifact:
            raise SpecflowError(f"{path}: requires 元素缺少产物名：{raw!r}")
        if status is not None and status not in ARTIFACT_STATUSES:
            raise SpecflowError(
                f"{path}: requires 状态非法 {status!r}（可选：{'/'.join(ARTIFACT_STATUSES)}）"
            )
        requirements.append(Requirement(artifact=artifact, status=status))
    return tuple(requirements)


# ---------------------------------------------------------------------- #
# 查询
# ---------------------------------------------------------------------- #
def get_stage(command: str) -> Stage | None:
    """按命令查阶段：支持 `/tech-design` 与 `tech-design`。"""
    normalized = _normalize_command(command)
    if not normalized:
        return None
    stages = _load_default_stages()
    return stages.get(normalized) if stages else None


def _load_default_stages() -> "OrderedDict[str, Stage] | None":
    root = Path(_DEFAULT_SKILLS_DIR)
    try:
        cached = _CACHE.get(root.resolve())
        if cached is not None:
            return cached
        return load_stages(root)
    except SpecflowError:
        return None


def _normalize_command(command: str) -> str:
    text = (command or "").strip()
    if not text:
        return ""
    return text if text.startswith("/") else "/" + text


def stage_command(slug: str) -> str:
    """`plan-task` → `/plan-task`；已带 `/` 原样返回。"""
    return _normalize_command(slug)


def all_commands() -> list[str]:
    """9 个固定阶段命令（不含 skills 里的扩展阶段）。"""
    return list(REQUIRED_COMMANDS)


def stage_ids() -> list[str]:
    return list(STAGE_ORDER)