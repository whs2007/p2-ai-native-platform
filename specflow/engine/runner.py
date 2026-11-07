"""阶段执行器：9 个阶段 command → 门控 → 上下文组装 → LLM/确定性逻辑 → 产物落盘 → 状态推进。

契约见 docs/07 §5.10 / §6。所有 LLM 输出先按 specflow.output_schemas 校验；
失败不写产物、不推进状态（docs/07 §7）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from specflow.artifacts import schema as artifact_schema
from specflow.artifacts.frontmatter import parse as parse_frontmatter
from specflow.artifacts.indexer import Indexer
from specflow.artifacts.lock import FileLock
from specflow.artifacts.store import Artifact, ArtifactStore
from specflow.config import Settings
from specflow.engine.gate import GateResult, check, summarize_upstream
from specflow.engine.stages import Stage, load_stages
from specflow.engine.state import StateStore
from specflow.errors import (
    ArtifactError,
    GateBlocked,
    LLMError,
    NeedsHuman,
    SpecflowError,
    UsageError,
)
from specflow.llm.base import LLMClient
from specflow.output_schemas import OUTPUT_SCHEMAS
from specflow.platforms.base import PlatformAdapter
from specflow.util import (
    append_jsonl,
    atomic_write_text,
    estimate_tokens,
    now_iso,
    read_text,
    rel_posix,
    today_stamp,
    truncate_by_tokens,
)

_SKIP_DIRS = {
    ".git",
    ".spec",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".demo",
    "dist",
    "build",
}
_LANG_BY_SUFFIX = {
    ".py": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".go": "Go",
    ".java": "Java",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".md": "Markdown",
    ".vue": "Vue",
}
_SKIP_ARTIFACT_NAMES = {"AGENTS.md", "domain.md", "index.md", "README.md"}


@dataclass
class RunResult:
    stage_id: str
    command: str
    summary: str
    artifacts: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 0


class StageRunner:
    def __init__(
        self,
        settings: Settings,
        store: ArtifactStore,
        state_store: StateStore,
        indexer: Indexer,
        llm: LLMClient,
        platform: PlatformAdapter,
    ) -> None:
        self.settings = settings
        self.store = store
        self.state_store = state_store
        self.indexer = indexer
        self.llm = llm
        self.platform = platform
        self.state = self.state_store.load()
        self._trace_path: Path | None = None

    # ------------------------------------------------------------------ #
    # 入口
    # ------------------------------------------------------------------ #
    def run(self, stage: Stage, args: Mapping[str, Any]) -> RunResult:
        slug = stage.slug
        # 同一进程内可能先跑过 `gate approve`（例如 demo/e2e 复用 runner），
        # 每次执行前重新加载 state，避免用旧内存状态覆盖已批准的门控。
        self.state = self.state_store.load()
        self._trace_path = self.settings.traces_dir / f"stage-{slug}-{today_stamp()}.jsonl"
        self._trace(stage, "start", status="running", data={"args": dict(args)})
        try:
            self._precheck(stage)
            self.state.clear_gate_failures(slug)
            handler = getattr(self, f"_run_{slug.replace('-', '_')}", None)
            if handler is None:
                raise UsageError(f"阶段 {stage.command} 尚未实现执行器")
            result = handler(stage, dict(args))
            self.state.last_command = stage.command
            self.state.updated_at = now_iso()
            self.state_store.save(self.state)
            self._trace(stage, "done", status="ok", data={"artifacts": result.artifacts})
            return result
        except SpecflowError as exc:
            self.state.last_command = stage.command
            self.state.updated_at = now_iso()
            self.state_store.save(self.state)
            self._trace(stage, "error", status="error", data={"error": exc.message})
            raise

    # ------------------------------------------------------------------ #
    # 门控
    # ------------------------------------------------------------------ #
    def _precheck(self, stage: Stage) -> None:
        result = check(stage, self.store, self.state)
        if result.passed:
            return
        failures = self.state.record_gate_failure(stage.slug)
        self.state_store.save(self.state)
        if failures >= self.settings.gate_repeat_limit:
            raise NeedsHuman(
                f"{stage.command} 已连续 {failures} 次未通过门控，停止自动重试。\n"
                + "\n".join(result.missing),
                hint=result.howto,
            )
        raise GateBlocked(
            f"{stage.command} 被门控阻断（第 {failures} 次）。",
            missing=result.missing,
            reasons=result.reasons,
            howto=result.howto,
            details=result.details,
        )

    # ------------------------------------------------------------------ #
    # 通用：LLM / 产物 / 上下文
    # ------------------------------------------------------------------ #
    def _skill_prompt(self, stage: Stage) -> str:
        try:
            text = read_text(stage.skill_path)
        except OSError:
            return f"你是 specflow 的 {stage.title} 角色。"
        try:
            _meta, body = parse_frontmatter(text)
            return body.strip()
        except SpecflowError:
            return text.strip()

    def _llm_json(
        self,
        stage: Stage,
        scenario: str,
        context: Mapping[str, Any],
        *,
        round: int | None = None,
    ) -> dict[str, Any]:
        schema = OUTPUT_SCHEMAS[scenario]
        messages = [
            {
                "role": "system",
                "content": self._skill_prompt(stage)
                + "\n\n硬性要求：只输出一个 JSON 对象，不要 Markdown 代码围栏，不要解释。",
            },
            {"role": "user", "content": json.dumps(context, ensure_ascii=False, indent=2)},
        ]
        result = self.llm.complete(messages, schema=schema, scenario=scenario, round=round)
        self._trace(
            stage,
            "llm",
            status="ok",
            data={
                "scenario": scenario,
                "round": round,
                "mode": result.mode,
                "model": result.model,
                "usage": dict(result.usage or {}),
                "elapsed_ms": result.elapsed_ms,
                "cached": result.cached,
            },
        )
        if result.data is None:
            raise LLMError(f"阶段 {stage.id} 未返回可解析的 JSON（mode={result.mode}）")
        return dict(result.data)

    def _save(
        self,
        stage: Stage,
        name: str,
        body: str,
        *,
        meta: Mapping[str, Any] | None = None,
        status: str = "draft",
    ) -> Artifact:
        lock_path = self.settings.locks_dir / f"{Path(name).stem}.lock"
        lock = FileLock(
            lock_path,
            name,
            owner=self.settings.owner,
            session_id=os.environ.get("SPECFLOW_SESSION_ID", "cli"),
        )
        lock.acquire()
        try:
            artifact = self.store.save(
                name,
                body,
                meta=meta,
                status=status,
                stage=stage.id,
                owner=self.settings.owner,
            )
        finally:
            lock.release()
        problems = artifact_schema.validate(artifact)
        if problems:
            raise ArtifactError(
                f"{name} 未通过产物校验，未登记：\n" + "\n".join(f"  - {item}" for item in problems)
            )
        self.indexer.register_artifact(artifact)
        self.state.record_artifact(name, artifact.version, artifact.hash, artifact.status, stage.id)
        return artifact

    def _load(self, name: str) -> Artifact:
        try:
            return self.store.load(name)
        except SpecflowError as exc:
            raise ArtifactError(f"缺少上游产物 {name}：{exc}") from exc

    def _load_optional(self, name: str) -> Artifact | None:
        return self.store.load_or_none(name)

    def _stage_context(self, stage: Stage, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        agents = self._read_root("AGENTS.md")
        rules = [line.strip() for line in agents.splitlines() if line.strip().startswith("- R-")][:20]
        upstream: list[dict[str, Any]] = []
        for requirement in stage.requires:
            artifact = self.store.load_or_none(requirement.artifact)
            if artifact is None:
                continue
            upstream.append(
                {
                    "name": artifact.name,
                    "summary": artifact.summary(),
                    "sections": artifact.sections(),
                    "path": rel_posix(artifact.path, self.settings.project_dir),
                }
            )
        profile = self._load_optional("repo-profile.md")
        context: dict[str, Any] = {
            "stage": stage.id,
            "command": stage.command,
            "repo": self.settings.project_dir.name,
            "rules": rules,
            "upstream": upstream,
            "profile": profile.appendix() if profile else None,
            "domain": self._read_root("domain.md")[:2000],
            "anchor_hint": "需要正文细节时按 path#章节 读取产物，不要臆造。",
        }
        if extra:
            context.update(extra)
        return self._fit_context(stage, context)

    def _fit_context(self, stage: Stage, context: dict[str, Any]) -> dict[str, Any]:
        """按 SKILL 的 `max_context_tokens` 收缩上下文（阶段契约的硬上限）。

        顺序：上游摘要/章节 → 规则条数 → domain 截断 → 丢弃 upstream/profile。
        只收缩可重建的信息；不改变产物与状态。
        """
        budget = max(200, int(stage.max_context_tokens or 3000))

        def size(value: dict[str, Any]) -> int:
            return estimate_tokens(json.dumps(value, ensure_ascii=False, default=str))

        if size(context) <= budget:
            return context
        for item in context.get("upstream") or []:
            if isinstance(item, dict):
                item.pop("sections", None)
                item.pop("summary", None)
        context["anchor_hint"] = "上下文超出阶段预算：上游只保留 path，需要细节时按 path#章节 读取。"
        if size(context) <= budget:
            return context
        context["rules"] = list(context.get("rules") or [])[:10]
        if size(context) <= budget:
            return context
        context["domain"], _ = truncate_by_tokens(
            str(context.get("domain") or ""), max(1, budget // 4)
        )
        if size(context) <= budget:
            return context
        context["upstream"] = []
        context.pop("profile", None)
        if size(context) <= budget:
            return context
        context["domain"] = ""
        context["rules"] = []
        return context

    def _read_root(self, name: str) -> str:
        path = self.settings.project_dir / name
        try:
            return read_text(path)
        except OSError:
            return ""

    def _rebuild_index(self) -> None:
        try:
            self.indexer.rebuild_markdown(self.store, self.state)
        except SpecflowError:
            pass

    def _trace(self, stage: Stage, step: str, *, status: str = "ok",
               data: Mapping[str, Any] | None = None) -> None:
        if self._trace_path is None:
            return
        append_jsonl(
            self._trace_path,
            {
                "ts": now_iso(),
                "stage": stage.id,
                "step": step,
                "status": status,
                "data": dict(data or {}),
            },
        )

    # ------------------------------------------------------------------ #
    # S0A /project-init
    # ------------------------------------------------------------------ #
    def _run_project_init(self, stage: Stage, args: dict[str, Any]) -> RunResult:
        profile = self._scan_repo()
        profile_body = self._render_profile(profile)
        artifact = self._save(
            stage,
            "repo-profile.md",
            profile_body,
            meta={"summary": f"{profile['language']} / {profile['package_manager']} / {profile['test_command']}"},
        )
        agents_path = self.settings.project_dir / "AGENTS.md"
        self._write_agents_md(agents_path, profile)
        templates = self._extract_templates(profile)
        self._rebuild_index()
        self.state.advance(stage.id)
        notes = [f"AGENTS.md 已更新（命令基线 {len(profile['commands'])} 条）"]
        notes.append(f"templates/ 共 {len(templates)} 个：" + "、".join(templates))
        return RunResult(
            stage.id,
            stage.command,
            f"{stage.command} 完成：仓库画像已生成（{profile['language']}，{profile['test_command']}）。",
            artifacts=[artifact.name],
            details={"notes": notes, "profile": profile},
        )

    def _scan_repo(self, limit: int = 200) -> dict[str, Any]:
        root = self.settings.project_dir
        files: list[Path] = []
        for path in sorted(root.rglob("*")):
            if len(files) >= limit:
                break
            try:
                relative_parts = path.relative_to(root).parts
            except ValueError:  # pragma: no cover - 防御
                continue
            if any(part in _SKIP_DIRS for part in relative_parts):
                continue
            if path.is_file():
                files.append(path)
        counts: dict[str, int] = {}
        for path in files:
            language = _LANG_BY_SUFFIX.get(path.suffix.lower())
            if language:
                counts[language] = counts.get(language, 0) + 1
        language = max(counts, key=counts.get) if counts else "Unknown"

        package_manager = "pip (pyproject.toml)" if (root / "pyproject.toml").exists() else "pip"
        if (root / "package.json").exists():
            package_manager = "npm (package.json)"
        elif (root / "go.mod").exists():
            package_manager = "go mod"
        elif (root / "requirements.txt").exists():
            package_manager = "pip (requirements.txt)"

        test_command = "python -m pytest -q"
        if (root / "package.json").exists():
            test_command = "npm test"
        elif (root / "go.mod").exists():
            test_command = "go test ./..."

        entry_points: list[str] = []
        for pattern in ("src/*/__main__.py", "*/__main__.py", "main.py", "src/main.py"):
            for path in sorted(root.glob(pattern)):
                entry_points.append(rel_posix(path, root))
        if not entry_points:
            for path in sorted(root.glob("src/*/__init__.py")):
                entry_points.append(rel_posix(path, root) + "（包入口）")

        convention_candidates = [
            "README.md",
            "CONTRIBUTING.md",
            "AGENTS.md",
            "pyproject.toml",
            "ruff.toml",
            ".editorconfig",
            "Makefile",
            ".gitignore",
        ]
        conventions = [name for name in convention_candidates if (root / name).exists()]
        key_dirs = sorted(
            {
                path.parts[len(root.parts)]
                for path in files
                if len(path.parts) > len(root.parts) + 1
            }
        )[:6]

        commands = [
            {"purpose": "测试", "command": test_command},
            {"purpose": "安装", "command": "pip install -e ."},
        ]
        if (root / "ruff.toml").exists() or "ruff" in read_text(root / "pyproject.toml") if (root / "pyproject.toml").exists() else False:
            commands.append({"purpose": "Lint", "command": "ruff check src tests"})
        else:
            commands.append({"purpose": "语法编译检查", "command": "python -m compileall -q src"})
        commands.append({"purpose": "模块导入", "command": "python -c \"import visit_plan\""})

        return {
            "language": language,
            "languages": sorted(counts, key=lambda key: -counts[key]),
            "package_manager": package_manager,
            "test_command": test_command,
            "entry_points": entry_points,
            "convention_files": conventions,
            "key_dirs": key_dirs,
            "commands": commands,
            "files": [rel_posix(path, root) for path in files],
            "file_count": len(files),
        }

    def _render_profile(self, profile: Mapping[str, Any]) -> str:
        command_rows = [[item["purpose"], f"`{item['command']}`"] for item in profile["commands"]]
        portrait = "\n".join(
            [
                f"- 语言/运行时：{profile['language']}",
                f"- 包管理：{profile['package_manager']}",
                f"- 入口：{'、'.join(profile['entry_points']) or '（未识别）'}",
                f"- 关键目录：{'、'.join(f'`{item}/`' for item in profile['key_dirs']) or '（无）'}",
                f"- 规范文件：{'、'.join(profile['convention_files']) or '（无）'}",
                f"- 文件数（扫描上限 200）：{profile['file_count']}",
            ]
        )
        appendix = {
            "language": profile["language"],
            "package_manager": profile["package_manager"],
            "test_command": profile["test_command"],
            "entry_points": profile["entry_points"],
            "convention_files": profile["convention_files"],
            "commands": profile["commands"],
        }
        sections = [
            ("1 仓库画像", portrait),
            ("2 命令基线", self._md_table(["用途", "命令"], command_rows)),
        ]
        return self._join(sections, appendix)

    def _write_agents_md(self, path: Path, profile: Mapping[str, Any]) -> None:
        existing = read_text(path) if path.exists() else ""
        rules = self._extract_section_lines(existing, "## 2.", "## 3.")
        # init 生成的 v0.1 只有 R-01/R-02 两条占位规则；此时换成完整基线，
        # 避免 /project-init 之后规则反而比基线更少。
        if len(rules) <= 2:
            rules = [
                "- R-01 新增函数必须有类型标注；公共函数必须有 docstring",
                "- R-02 文件读写使用 `pathlib` 与 `encoding=\"utf-8\"`",
                "- R-03 禁止在业务代码里 `print`，日志走 `logging`",
                "- R-04 测试命名 `test_<行为>_<条件>_<期望>`",
                "- R-05 不引入新依赖，除非在 `design.md` 选型表中列出理由",
            ]
        history = self._extract_section_lines(existing, "## 5.", None)
        version = self._bump_patch(self._parse_agents_version(existing))
        command_rows = "\n".join(f"| {item['purpose']} | `{item['command']}` |" for item in profile["commands"])
        lines = [
            f"# AGENTS.md — {self.settings.project_dir.name} 基线规则",
            "",
            f"> 版本：{version} ｜ 更新：{now_iso()[:19]} ｜ 由 specflow 维护，人工可编辑",
            "",
            "## 1. 仓库画像",
            f"- 语言/运行时：{profile['language']}",
            f"- 包管理：{profile['package_manager']}",
            f"- 入口：{'、'.join(profile['entry_points']) or '（未识别）'}",
            f"- 关键目录：{'、'.join(f'`{item}/`' for item in profile['key_dirs']) or '（无）'}",
            "",
            "## 2. 工程规则（每条都必须可判定）",
            *rules,
            "",
            "## 3. 流程规则",
            "- 进入 `/plan-task` 前 `design.md` 必须 approved",
            "- 阶段产物写入 `.spec/artifacts/`，不得写到仓库根",
            "- 危险命令被 deny 时，按理由中的替代方案执行",
            "",
            "## 4. 命令基线",
            "| 用途 | 命令 |",
            "|---|---|",
            command_rows,
            "",
            "## 5. 变更记录",
            *history,
            f"- {version} ← `/project-init` 刷新仓库画像与命令基线",
            "",
        ]
        atomic_write_text(path, "\n".join(lines))

    @staticmethod
    def _parse_agents_version(text: str) -> str:
        import re

        match = re.search(r"版本：v(\d+)\.(\d+)", text or "")
        return f"v{match.group(1)}.{match.group(2)}" if match else "v0.1"

    @staticmethod
    def _bump_patch(version: str) -> str:
        try:
            major, minor = version.lstrip("v").split(".")
            return f"v{major}.{int(minor) + 1}"
        except (ValueError, AttributeError):
            return "v0.2"

    @staticmethod
    def _extract_section_lines(text: str, start: str, end: str | None) -> list[str]:
        lines = text.splitlines()
        try:
            begin = next(index for index, line in enumerate(lines) if line.startswith(start))
        except StopIteration:
            return []
        collected: list[str] = []
        for line in lines[begin + 1 :]:
            if end and line.startswith(end):
                break
            if start == "## 5." and line.startswith("## "):
                break
            if line.strip():
                collected.append(line)
        return collected

    def _extract_templates(self, profile: Mapping[str, Any]) -> list[str]:
        target = self.settings.project_dir / "templates"
        target.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        builtin = self.settings.templates_dir
        if builtin.exists():
            for source in sorted(builtin.iterdir()):
                if source.is_file() and not source.name.startswith("."):
                    atomic_write_text(target / source.name, read_text(source))
                    written.append(source.name)
        conventions = target / "repo-conventions.md"
        atomic_write_text(
            conventions,
            "# 仓库约定模板\n\n"
            f"- 语言：{profile['language']}\n"
            f"- 测试命令：`{profile['test_command']}`\n"
            "- 命名：`<模块>_<行为>.py`；测试 `test_<行为>_<条件>_<期望>`\n"
            "- 新增文件请先登记到 `tasks.md` 的 `<files>` 范围\n",
        )
        written.append(conventions.name)
        return sorted(set(written))

    # ------------------------------------------------------------------ #
    # S0B /domain-input
    # ------------------------------------------------------------------ #
    def _run_domain_input(self, stage: Stage, args: dict[str, Any]) -> RunResult:
        source = args.get("file") or self._first_existing("docs/domain/*.md")
        if source:
            path = self._resolve(source)
            if not path.exists():
                raise UsageError(f"领域知识文件不存在：{path}")
            source_text = read_text(path)
            source_ref = rel_posix(path, self.settings.project_dir)
        else:
            source_text = self._read_root("AGENTS.md")
            source_ref = "AGENTS.md"
        source_text, truncated = truncate_by_tokens(source_text, 6000)
        context = self._stage_context(
            stage,
            {"source_ref": source_ref, "source_text": source_text, "truncated": truncated},
        )
        data = self._llm_json(stage, "domain-input", context)
        sections = [
            ("1 业务域全景", str(data["domain"])),
            ("2 核心规则", self._md_table(
                ["#", "规则", "反例（违反即错）"],
                [[item["id"], item["rule"], item["counterexample"]] for item in data["rules"]],
            )),
            ("3 术语表", self._md_table(
                ["术语", "含义", "易混项"],
                [[item["term"], item["meaning"], item["confusable"]] for item in data["terms"]],
            )),
            ("4 边界与禁区", "\n".join(f"- {item}" for item in data.get("boundaries", [])) or "- （无）"),
        ]
        body = self._join(sections, {"source": source_ref, **data})
        header = (
            f"# domain.md — {self.settings.project_dir.name} 领域知识\n\n"
            f"> 版本：v1 ｜ 更新：{now_iso()[:19]} ｜ 来源：`{source_ref}`\n"
        )
        atomic_write_text(self.settings.project_dir / "domain.md", header + "\n" + body)
        self._rebuild_index()
        self.state.advance(stage.id)
        return RunResult(
            stage.id,
            stage.command,
            f"{stage.command} 完成：domain.md（规则 {len(data['rules'])} 条、术语 {len(data['terms'])} 条）。",
            artifacts=[],
            details={"notes": [f"domain.md ← {source_ref}", "index.md 已刷新"]},
        )

    # ------------------------------------------------------------------ #
    # S1 /prd-input
    # ------------------------------------------------------------------ #
    def _run_prd_input(self, stage: Stage, args: dict[str, Any]) -> RunResult:
        doc_id = args.get("doc")
        source_meta: dict[str, Any]
        if doc_id:
            claim = self.platform.docs_claim(doc_id, self.settings.owner)
            document = self.platform.docs_get(doc_id)
            ocr = self.platform.docs_ocr(doc_id)
            download = self.platform.docs_download(doc_id)
            source_text = str(document.get("text") or "")
            content = download.get("content") or b""
            downloaded_to = None
            if content:
                sources_dir = self.settings.spec_dir / "sources"
                sources_dir.mkdir(parents=True, exist_ok=True)
                filename = str(download.get("filename") or f"{doc_id}.md")
                suffix = Path(filename).suffix or ".md"
                target = sources_dir / f"{doc_id}{suffix}"
                target.write_bytes(content)
                source_text = content.decode("utf-8", "replace")
                downloaded_to = rel_posix(target, self.settings.project_dir)
            source_meta = {
                "type": "docs-shuttle",
                "ref": doc_id,
                "doc_id": doc_id,
                "claimed_by": claim.get("claimed_by"),
                "images": ocr.get("images", []),
                "downloaded_to": downloaded_to,
                "content_type": download.get("content_type"),
                "bytes": int(download.get("bytes") or 0),
            }
        else:
            source = args.get("file") or self._first_existing("docs/prd/*.md")
            if not source:
                raise UsageError("缺少 PRD 输入：用 `/prd-input <file>` 或 `/prd-input --doc DOC-2041`")
            path = self._resolve(str(source))
            if not path.exists():
                raise UsageError(f"PRD 文件不存在：{path}")
            source_text = read_text(path)
            source_meta = {"type": "file", "ref": rel_posix(path, self.settings.project_dir)}
        if not source_text.lstrip("\ufeff").strip():
            raise UsageError(f"PRD 输入为空：{source_meta.get('ref') or source_meta.get('doc_id') or 'unknown'}")
        source_text, truncated = truncate_by_tokens(source_text, 8000)
        context = self._stage_context(
            stage,
            {"source": source_meta, "prd_text": source_text, "truncated": truncated},
        )
        data = self._llm_json(stage, "prd-input", context)
        sections = [
            ("1 背景", str(data["background"])),
            ("2 需求条目", self._md_table(
                ["编号", "标题", "描述", "验收标准"],
                [
                    [item["id"], item["title"], item["desc"], "；".join(item["acceptance"])]
                    for item in data["requirements"]
                ],
            )),
            ("3 验收标准", "\n".join(
                f"- {item['id']}：{acceptance}"
                for item in data["requirements"]
                for acceptance in item["acceptance"]
            )),
            ("4 范围外", "\n".join(f"- {item}" for item in data.get("out_of_scope", [])) or "- （无）"),
        ]
        appendix = {"title": data["title"], "requirements": data["requirements"]}
        body = self._join(sections, appendix)
        artifact = self._save(
            stage,
            "prd.md",
            f"# PRD — {data['title']}\n\n" + body,
            meta={"source": source_meta, "summary": f"{data['title']}（{len(data['requirements'])} 条需求）"},
        )
        self.state.advance(stage.id)
        self._rebuild_index()
        return RunResult(
            stage.id,
            stage.command,
            f"{stage.command} 完成：prd.md（{len(data['requirements'])} 条需求，来源 {source_meta['type']}）。",
            artifacts=[artifact.name],
            details={"notes": [f"需求编号：{', '.join(item['id'] for item in data['requirements'])}"]},
        )

    # ------------------------------------------------------------------ #
    # S2 /explore
    # ------------------------------------------------------------------ #
    def _run_explore(self, stage: Stage, args: dict[str, Any]) -> RunResult:
        prd = self._load("prd.md")
        scope = args.get("scope")
        profile = self._scan_repo()
        files = profile["files"]
        if scope:
            files = [item for item in files if item.startswith(str(scope).strip("/"))]
        context = self._stage_context(
            stage,
            {
                "prd": prd.appendix(),
                "files": files[:120],
                "scope": scope,
            },
        )
        data = self._llm_json(stage, "explore", context)
        sections = [
            ("1 受影响模块", self._md_table(
                ["路径", "职责"],
                [[item["path"], item["purpose"]] for item in data["modules"]],
            )),
            ("2 现有约束", "\n".join(f"- {item}" for item in data.get("constraints", [])) or "- （无）"),
            ("3 未知项", self._md_table(
                ["未知项", "状态"],
                [[item["item"], item["status"]] for item in data["unknowns"]],
            )),
        ]
        appendix = {
            "modules": data["modules"],
            "constraints": data.get("constraints", []),
            "unknowns": data["unknowns"],
            "deps": data.get("deps", []),
        }
        artifact = self._save(
            stage,
            "exploration.md",
            f"# 综合探索 — {self.settings.project_dir.name}\n\n" + self._join(sections, appendix),
            meta={"summary": f"受影响模块 {len(data['modules'])} 个，未知项 {len(data['unknowns'])} 个"},
        )
        self.state.advance(stage.id)
        self._rebuild_index()
        return RunResult(
            stage.id,
            stage.command,
            f"{stage.command} 完成：exploration.md（模块 {len(data['modules'])}、未知项 {len(data['unknowns'])}）。",
            artifacts=[artifact.name],
        )

    # ------------------------------------------------------------------ #
    # S3 /tech-design
    # ------------------------------------------------------------------ #
    def _run_tech_design(self, stage: Stage, args: dict[str, Any]) -> RunResult:
        prd = self._load("prd.md")
        exploration = self._load("exploration.md")
        note = args.get("note")
        gate = self.state.gates.get("design")
        if not note and gate and gate.status == "rejected" and gate.note:
            note = gate.note
        context = self._stage_context(
            stage,
            {
                "prd": prd.appendix(),
                "exploration": exploration.appendix(),
                "rejection_note": note,
                "files": self._scan_repo()["files"][:80],
            },
        )
        data = self._llm_json(stage, "tech-design", context)
        sections = [
            ("1 选型对比", self._md_table(
                ["方案", "优点", "代价"],
                [[item["name"], item["pros"], item["cons"]] for item in data["options"]],
            )),
            ("2 模块改动", self._md_table(
                ["文件", "改动"],
                [[item["path"], item["change"]] for item in data["changes"]],
            )),
            ("3 接口变更", self._md_table(
                ["接口", "说明"],
                [[item["name"], item["detail"]] for item in (data.get("interfaces") or [])],
            )),
            ("4 风险与回滚", self._md_table(
                ["#", "风险", "缓解", "回滚"],
                [
                    [item["id"], item["desc"], item["mitigation"], item["rollback"]]
                    for item in data["risks"]
                ],
            )),
        ]
        appendix = {
            "gate": "design",
            "options": data["options"],
            "changes": data["changes"],
            "interfaces": data.get("interfaces", []),
            "risks": data["risks"],
        }
        artifact = self._save(
            stage,
            "design.md",
            f"# 技术方案 — {self.settings.project_dir.name}\n\n" + self._join(sections, appendix),
            meta={"gate": "design", "summary": data["summary"]},
        )
        previous_gate = self.state.gates.get("design")
        self.state.gates["design"] = self._gate_state(
            "pending", round_no=(previous_gate.round + 1) if previous_gate else 1
        )
        self.state.advance(stage.id)
        self._rebuild_index()
        return RunResult(
            stage.id,
            stage.command,
            f"{stage.command} 完成：design.md（draft，选型 {len(data['options'])} 个、风险 {len(data['risks'])} 条）。\n"
            f"下一步：specflow gate approve design --as <你> --note \"<结论>\"",
            artifacts=[artifact.name],
            details={"notes": [f"方案摘要：{data['summary']}"]},
        )

    # ------------------------------------------------------------------ #
    # S4 /plan-task
    # ------------------------------------------------------------------ #
    def _run_plan_task(self, stage: Stage, args: dict[str, Any]) -> RunResult:
        design = self._load("design.md")
        prd = self._load_optional("prd.md")
        max_hours = float(args.get("max_hours") or 4)
        context = self._stage_context(
            stage,
            {
                "design": design.appendix(),
                "prd": prd.appendix() if prd else None,
                "max_hours": max_hours,
                "feature": self.settings.project_dir.name,
            },
        )
        data = self._llm_json(stage, "plan-task", context)
        for task in data["tasks"]:
            if not task.get("test_first"):
                raise ArtifactError(f"任务 {task['id']} 未声明 test_first=true，拒绝拆解")
            if float(task["estimate_h"]) > max_hours:
                raise ArtifactError(
                    f"任务 {task['id']} 预估 {task['estimate_h']}h 超过上限 {max_hours}h，请拆分"
                )
        feature = self.platform.feature_create(
            f"{self.settings.project_dir.name}-{data['feature']['name']}",
            self.settings.owner,
        )
        feature_id = str(feature.get("feature_id") or "FEAT-0000")
        branch = str(feature.get("branch") or f"specflow/{feature_id}")
        for task in data["tasks"]:
            self.platform.feature_bind(feature_id, task["id"], branch)
            task["feature_id"] = feature_id
        branch_note: str | None = None
        if self.settings.git_branch_enabled:
            from specflow.git_ops import ensure_feature_branch

            info = ensure_feature_branch(self.settings.project_dir, branch)
            action = "新建" if info["created"] else ("切换" if info["switched"] else "已在该分支")
            branch_note = f"git 分支：{branch}（{action}）"
        sections = [
            ("1 任务表", self._md_table(
                ["任务", "标题", "文件范围", "测试先行", "预估", "验收点"],
                [
                    [
                        task["id"],
                        task["title"],
                        "、".join(f"`{item}`" for item in task["files"]),
                        "是" if task["test_first"] else "否",
                        f"{task['estimate_h']}h",
                        "；".join(task["acceptance"]),
                    ]
                    for task in data["tasks"]
                ],
            )),
            ("2 依赖顺序", "\n".join(f"- {item}" for item in data.get("order", [])) or "- 无"),
        ]
        appendix = {
            "feature_id": feature_id,
            "branch": branch,
            "tasks": data["tasks"],
            "order": data.get("order", []),
        }
        artifact = self._save(
            stage,
            "tasks.md",
            f"# 任务拆解 — {feature_id}\n\n" + self._join(sections, appendix),
            meta={
                "feature_id": feature_id,
                "summary": f"{len(data['tasks'])} 个原子任务（{feature_id}）",
            },
        )
        self.state.advance(stage.id)
        self._rebuild_index()
        return RunResult(
            stage.id,
            stage.command,
            f"{stage.command} 完成：tasks.md（{len(data['tasks'])} 个任务，feature {feature_id}）。",
            artifacts=[artifact.name],
            details={"notes": ([branch_note] if branch_note else []) + ['下一步：specflow run "/add --task T1"']},
        )

    # ------------------------------------------------------------------ #
    # S5 /add
    # ------------------------------------------------------------------ #
    def _run_add(self, stage: Stage, args: dict[str, Any]) -> RunResult:
        from specflow.agents.loop import TDDRunner

        tasks_artifact = self._load("tasks.md")
        appendix = tasks_artifact.appendix() or {}
        tasks: list[dict[str, Any]] = list(appendix.get("tasks") or [])
        if not tasks:
            raise ArtifactError("tasks.md 附录缺少 tasks[]，无法执行")
        selected_id = args.get("task")
        if args.get("all"):
            selected = tasks
        elif selected_id:
            selected = [task for task in tasks if task["id"] == selected_id]
            if not selected:
                raise UsageError(f"tasks.md 中不存在任务 {selected_id}")
        elif len(tasks) == 1:
            selected = tasks
        else:
            ids = "、".join(task["id"] for task in tasks)
            raise UsageError(f"存在多个任务（{ids}），请指定 --task T1 或 --all")
        design = self._load_optional("design.md")
        domain = self._load_optional("domain.md")  # domain 在仓库根，不在 artifacts；None 时 loop 自行降级
        runner = TDDRunner(
            self.settings, self.store, self.state_store, self.llm, self.indexer
        )
        outcomes = []
        for task in selected:
            outcome = runner.run_task(task, design=design, domain=domain)
            outcomes.append(outcome)
            self._trace(stage, "tdd", status=outcome.verdict, data={"task": task["id"], "rounds": outcome.rounds})

        rounds = max((outcome.rounds for outcome in outcomes), default=1)
        needs_human = any(outcome.verdict == "needs_human" for outcome in outcomes)
        verdict = "needs_human" if needs_human else (
            "pass" if all(outcome.verdict == "pass" for outcome in outcomes) else "fail"
        )
        passed = sum(outcome.passed for outcome in outcomes)
        total = sum(outcome.total for outcome in outcomes)
        failure_items = [item for outcome in outcomes for item in outcome.failure_items]

        failure_lines: list[str] = []
        for outcome in outcomes:
            for failure in outcome.failure_items:
                failure_lines.append(
                    f"- {outcome.task_id}：{failure.get('case')} — {failure.get('error')} → "
                    f"{failure.get('action')}"
                )
        sections = [
            ("1 执行摘要", self._md_table(
                ["任务", "轮次", "结论", "通过/总数"],
                [
                    [outcome.task_id, outcome.rounds, outcome.verdict, f"{outcome.passed}/{outcome.total}"]
                    for outcome in outcomes
                ],
            )),
            ("2 失败项", "\n".join(failure_lines) or "- 无"),
            ("3 最终测试结果", f"通过 {passed}/{total}；总轮次 {rounds}；结论 {verdict}"),
        ]
        appendix = {
            "tasks": [
                {
                    "task_id": outcome.task_id,
                    "rounds": outcome.rounds,
                    "verdict": outcome.verdict,
                    "passed": outcome.passed,
                    "total": outcome.total,
                    "failures": outcome.failure_items,
                }
                for outcome in outcomes
            ],
            "rounds": rounds,
            "verdict": verdict,
            "needs_human": needs_human,
        }
        artifact = self._save(
            stage,
            "tdd-report.md",
            f"# TDD 执行报告 — {', '.join(outcome.task_id for outcome in outcomes)}\n\n"
            + self._join(sections, appendix),
            meta={
                "task_id": outcomes[0].task_id if outcomes else "",
                "rounds": rounds,
                "verdict": verdict,
                "needs_human": needs_human,
                "summary": f"{len(outcomes)} 个任务，{rounds} 轮，{verdict}",
            },
            status="approved" if verdict == "pass" else "draft",
        )
        self.state.rounds["S5"] = rounds
        self.state.set_needs_human(needs_human)
        self.state.advance(stage.id)
        self._rebuild_index()
        if needs_human:
            raise NeedsHuman(
                f"{stage.command} 超过最大回流轮次，已标记 needs_human（测试文件与失败现场保留）。\n"
                f"失败项：{len(failure_items)} 条；修复后重跑 specflow run \"/add --task {outcomes[0].task_id}\"。",
                hint="人工修复后执行 specflow run \"/add --task <T>\" 续跑。",
            )
        return RunResult(
            stage.id,
            stage.command,
            f"{stage.command} 完成：{len(outcomes)} 个任务，{rounds} 轮收敛，测试 {passed}/{total} 通过。\n"
            f"下一步：specflow run \"/qa-validate\"",
            artifacts=[artifact.name, "tdd-plan.md", "tdd-report.jsonl"],
            details={"notes": [f"事件流：.spec/artifacts/tdd-report.jsonl"]},
        )

    # ------------------------------------------------------------------ #
    # S6 /qa-validate
    # ------------------------------------------------------------------ #
    def _run_qa_validate(self, stage: Stage, args: dict[str, Any]) -> RunResult:
        from specflow.platforms import get_platform

        tdd_report = self._load("tdd-report.md")
        tdd_appendix = tdd_report.appendix() or {}
        if tdd_appendix.get("verdict") != "pass":
            raise GateBlocked(
                "tdd-report.md 的 verdict 不是 pass，不能进入 QA 验证。",
                missing=[f"tdd-report.md:verdict={tdd_appendix.get('verdict')}"],
                howto='先修复失败用例并重跑 specflow run "/add --task <T>"。',
            )

        unit = self._run_unit_tests()
        platform = self.platform
        if args.get("fixture"):
            platform = get_platform(self.settings.with_overrides(platform_fixture=str(args["fixture"])))
        elif self.settings.platform_fixture:
            platform = get_platform(self.settings.with_overrides(platform_fixture=self.settings.platform_fixture))

        tasks_artifact = self._load_optional("tasks.md")
        tasks_appendix = tasks_artifact.appendix() or {} if tasks_artifact else {}
        feature_id = tasks_appendix.get("feature_id")
        # 分支信息来自 tasks.md 附录；不再查 platform.feature_contract（mock 只为预置 feature 提供契约，
        # 现场创建的 FEAT-xxxx 也能工作；live 接入时可用 contract 补 required_checks）。
        branch = str(tasks_appendix.get("branch") or f"specflow/{feature_id or 'feature'}")
        trigger = platform.pipeline_trigger(branch)
        run_id = str(trigger.get("run_id") or "RUN-0000")
        pipeline = platform.pipeline_status(run_id)
        pipeline_status = "passed" if pipeline.get("status") == "passed" else "failed"

        if args.get("skip_rpc"):
            rpc_status = "skipped"
            rpc_detail = "按 --skip-rpc 跳过"
        else:
            rpc = platform.rpc_call(
                "visit.import.validate",
                {"feature_id": feature_id, "suite": "visit-import", "cases": int(tdd_appendix.get("passed") or 0)},
            )
            rpc_status = "passed" if rpc.get("ok") else "failed"
            rpc_detail = json.dumps(rpc.get("result") or rpc.get("error"), ensure_ascii=False)

        evidence = [
            {"type": "unit", "status": unit["status"], "detail": unit["detail"]},
            {
                "type": "pipeline",
                "status": pipeline_status,
                "detail": f"{run_id} / {pipeline.get('duration_s', '?')}s / {pipeline.get('url', '')}",
            },
            {"type": "rpc", "status": rpc_status, "detail": rpc_detail},
        ]
        verdict = "pass" if all(item["status"] in {"passed", "skipped"} for item in evidence) else "fail"
        sections = [
            ("1 单测", f"{unit['status']}：{unit['detail']}"),
            ("2 pipeline", f"{pipeline_status}：{run_id}（{pipeline.get('duration_s', '?')}s）"),
            ("3 rpc", f"{rpc_status}：{rpc_detail}"),
            ("4 结论", f"verdict = {verdict}"),
        ]
        appendix = {"verdict": verdict, "evidence": evidence}
        artifact = self._save(
            stage,
            "qa-report.md",
            f"# QA 验证报告 — {self.settings.project_dir.name}\n\n" + self._join(sections, appendix),
            meta={
                "verdict": verdict,
                "evidence": [f"{item['type']}:{item['status']}" for item in evidence],
                "summary": f"单测 {unit['status']} / pipeline {pipeline_status} / rpc {rpc_status} → {verdict}",
            },
            status="approved" if verdict == "pass" else "draft",
        )
        self.state.advance(stage.id)
        self._rebuild_index()
        notes = [f"证据：{item['type']}={item['status']}" for item in evidence]
        if verdict == "pass":
            notes.append('下一步：specflow run "/archive"')
        else:
            notes.append("存在失败证据，/archive 会被门控阻断；修复后重跑 /qa-validate。")
        return RunResult(
            stage.id,
            stage.command,
            f"{stage.command} 完成：qa-report.md（verdict={verdict}）。",
            artifacts=[artifact.name],
            details={"notes": notes},
        )

    def _run_unit_tests(self) -> dict[str, str]:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q"],
                cwd=str(self.settings.project_dir),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            return {"status": "failed", "detail": "pytest 超时（>300s）"}
        output = (proc.stdout or "") + (proc.stderr or "")
        tail = output.strip().splitlines()[-1] if output.strip() else ""
        status = "passed" if proc.returncode == 0 else "failed"
        return {"status": status, "detail": f"{tail}（rc={proc.returncode}）"}

    # ------------------------------------------------------------------ #
    # S7 /archive
    # ------------------------------------------------------------------ #
    def _run_archive(self, stage: Stage, args: dict[str, Any]) -> RunResult:
        qa = self._load("qa-report.md")
        qa_appendix = qa.appendix() or {}
        if qa_appendix.get("verdict") != "pass":
            raise GateBlocked(
                "qa-report.md 的 verdict 不是 pass，不能归档。",
                missing=[f"qa-report.md:verdict={qa_appendix.get('verdict')}"],
                howto='先修复 QA 失败项并重跑 specflow run "/qa-validate"。',
            )
        tdd = self._load_optional("tdd-report.md")
        design = self._load_optional("design.md")
        tasks = self._load_optional("tasks.md")
        context = self._stage_context(
            stage,
            {
                "qa": qa_appendix,
                "tdd": tdd.appendix() if tdd else None,
                "design": design.appendix() if design else None,
                "tasks": tasks.appendix() if tasks else None,
            },
        )
        data = self._llm_json(stage, "archive", context)
        sections = [
            ("1 变更摘要", str(data["summary"])),
            ("2 候选回灌规则", self._md_table(
                ["#", "规则", "来源", "判据", "反例"],
                [
                    [item["id"], item["text"], item["source"], item["criteria"], item["counterexample"]]
                    for item in data["rules_proposed"]
                ],
            ) or "- （无候选规则）"),
            ("3 复用条目", "\n".join(f"- {item}" for item in data.get("reusable", [])) or "- （无）"),
            ("4 变更清单", "\n".join(f"- {item}" for item in data.get("changes", [])) or "- （无）"),
        ]
        appendix = {
            "rules_proposed": data["rules_proposed"],
            "changes": data.get("changes", []),
            "reusable": data.get("reusable", []),
        }
        if args.get("dry_run"):
            self.state.advance(stage.id)
            return RunResult(
                stage.id,
                stage.command,
                f"{stage.command}（dry-run）候选规则 {len(data['rules_proposed'])} 条，未写盘。",
                artifacts=[],
                details={"notes": [item["text"] for item in data["rules_proposed"]]},
            )
        artifact = self._save(
            stage,
            "archive-record.md",
            f"# 归档记录 — {self.settings.project_dir.name}\n\n" + self._join(sections, appendix),
            meta={
                "rules_proposed": [item["id"] for item in data["rules_proposed"]],
                "summary": data["summary"][:80],
            },
        )
        self.state.gates["agents-md"] = self._gate_state("pending", round_no=0)
        self.state.advance(stage.id)
        self._rebuild_index()
        return RunResult(
            stage.id,
            stage.command,
            f"{stage.command} 完成：archive-record.md（候选规则 {len(data['rules_proposed'])} 条）。\n"
            f"下一步：specflow gate approve agents-md --as <你> --note \"<结论>\"（回灌生效）",
            artifacts=[artifact.name],
            details={"notes": ["index.md 已刷新", "回灌须人工批准后才会写入 AGENTS.md"]},
        )

    def _gate_state(self, status: str, *, round_no: int = 0):
        from specflow.engine.state import GateState

        return GateState(status=status, at=now_iso(), round=round_no)

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def _resolve(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else (self.settings.project_dir / path)

    def _first_existing(self, pattern: str) -> str | None:
        matches = sorted(self.settings.project_dir.glob(pattern))
        return rel_posix(matches[0], self.settings.project_dir) if matches else None

    @staticmethod
    def _md_table(headers: Iterable[str], rows: Iterable[Iterable[Any]]) -> str:
        header = "| " + " | ".join(str(item) for item in headers) + " |"
        divider = "|" + "|".join("---" for _ in headers) + "|"
        body = ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
        if not body:
            return "（无）"
        return "\n".join([header, divider, *body])

    @staticmethod
    def _join(sections: Iterable[tuple[str, str]], appendix: Mapping[str, Any]) -> str:
        lines: list[str] = []
        for title, content in sections:
            lines.extend([f"## {title}", "", content.strip(), ""])
        lines.extend(
            [
                "## 附录：结构化数据",
                "",
                "```json",
                json.dumps(dict(appendix), ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
        return "\n".join(lines)