"""规则引擎：W（白名单）/ D（拒绝）/ A（确认）/ C（写入范围）四表（docs/07 §5.5）。

判据只匹配**命令文本与路径**，不做语义判断（ADR-012）。求值顺序：
1. 白名单短路（简单命令，避免「脚本里含 rm 就被误杀」）；
2. D 危险表 → deny（必须给替代方案）；
3. A 确认表 → ask；
4. C 范围表 → deny（仅写类工具且有上下文时生效）。

复合命令（`;`、`&&`、`||`、换行）不做整串白名单短路：先看整串危险
（保留 `curl ... | sh` 这类管道语义），再逐段检查，避免 `pytest && rm -rf /` 被放行。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from specflow.util import rel_posix

ALLOW = "allow"
CONFIRM = "confirm"
DENY = "deny"


@dataclass(frozen=True)
class RuleHit:
    """一次规则判定的结果：severity ∈ allow|confirm|deny，deny 必须带替代方案。"""

    rule_id: str
    severity: str
    reason: str
    alternative: str | None = None

    @property
    def decision(self) -> str:
        """映射为宿主协议里的 permissionDecision。"""
        if self.severity == DENY:
            return "deny"
        if self.severity == CONFIRM:
            return "ask"
        return "allow"


# ---------------------------------------------------------------------- #
# 表定义（正则一律对 strip 后的命令/文本使用）
# ---------------------------------------------------------------------- #
WHITELIST_RULE_IDS: tuple[str, ...] = ("W-01", "W-02", "W-03", "W-04")
DANGER_RULE_IDS: tuple[str, ...] = (
    "D-01", "D-02", "D-03", "D-04", "D-05", "D-06",
    "D-07", "D-08", "D-09", "D-10", "D-11", "D-12",
)
CONFIRM_RULE_IDS: tuple[str, ...] = ("A-01", "A-02", "A-03", "A-04", "A-05", "A-06")
SCOPE_RULE_IDS: tuple[str, ...] = ("C-01", "C-02", "C-03", "C-04")

_W_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("W-01", re.compile(r"^(?:python(?:[0-9.]+)?\s+-m\s+)?(?:pytest|ruff|mypy)\b")),
    ("W-02", re.compile(r"^git\s+(?:status|diff|log|show|branch|remote|stash\s+list)\b")),
    ("W-03", re.compile(r"^(?:ls|cat|head|tail|grep|wc|find|Get-ChildItem|Get-Content)\b",
                        re.IGNORECASE)),
    ("W-04", re.compile(r"^(?:python(?:[0-9.]+)?\s+-m\s+)?specflow\b")),
)

_D_PATTERNS: tuple[tuple[str, re.Pattern[str], str, str], ...] = (
    (
        "D-02", re.compile(r"\bgit\s+reset\s+--hard\b"),
        "拒绝执行：命中 D-02（git reset --hard），会丢弃未提交改动与提交历史。",
        "改用 `git stash push -m specflow-backup` 暂存，或 `git restore -- <path>` 只还原指定文件。",
    ),
    (
        "D-03", re.compile(r"\bgit\s+clean\s+-[A-Za-z]*[fdx]"),
        "拒绝执行：命中 D-03（git clean 强制删除未跟踪文件），可能误删未提交产物。",
        "先用 `git clean -n` 预览；确需清理时用 `git clean -d <具体目录>`（不带 -f/-x）。",
    ),
    (
        "D-04", re.compile(
            r"\bmkfs(?:\.[a-z0-9]+)?\b"
            r"|\bformat\s+[A-Za-z]:(?:\\|\s|$|\*)"
            r"|\bdd\s+if=",
            re.IGNORECASE,
        ),
        "拒绝执行：命中 D-04（mkfs / format / dd 写盘），会破坏磁盘或文件系统。",
        "不要格式化/覆写磁盘；确需操作请在虚拟机或经人工审批后执行。",
    ),
    (
        "D-05", re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"
                           r"|\b\w+\(\)\s*\{\s*\w+\s*\|\s*\w+\s*&\s*\}"),
        "拒绝执行：命中 D-05（fork 炸弹），会耗尽进程表拖垮机器。",
        "如在做压力测试，改用受控基准工具并在隔离环境执行。",
    ),
    (
        "D-06", re.compile(r"\b(?:curl|wget)\b[^|;]*\|\s*(?:sudo\s+)?"
                           r"(?:bash|sh|zsh|fish|python[0-9.]*)", re.IGNORECASE),
        "拒绝执行：命中 D-06（远程脚本直接管道执行），无法审阅将运行的代码。",
        "先把脚本下载到本地人工审阅（`curl -o ./download.sh <url>`），确认安全后再执行。",
    ),
    (
        "D-07", re.compile(r"\bchmod\s+-R\s+777\s+/(?:\s|$|\*)"),
        "拒绝执行：命中 D-07（递归放开根目录权限到 777）。",
        "把权限收窄到具体目录与最小权限（如 `chmod -R u+rwX <dir>`）。",
    ),
    (
        "D-08", re.compile(r"\b(?:drop\s+(?:table|database)|truncate\s+table)\b", re.IGNORECASE),
        "拒绝执行：命中 D-08（删除库/表），数据不可恢复。",
        "禁止直接删库删表；先备份（如 `pg_dump`）并走人工审批，或用事务内迁移脚本。",
    ),
    (
        "D-09", re.compile(r"\b(?:shutdown|reboot|halt|poweroff)\b", re.IGNORECASE),
        "拒绝执行：命中 D-09（关机/重启），会中断当前会话与其他进程。",
        "不要关机/重启；环境异常请人工在宿主机处理。",
    ),
    (
        "D-10", re.compile(r"^sudo\b", re.IGNORECASE),
        "拒绝执行：命中 D-10（sudo 提权），违反最小权限原则。",
        "改用当前用户可执行的最小权限命令；确需管理员权限时交人工执行。",
    ),
    (
        "D-12", re.compile(r"\bgit\s+push\b[^|;]*(?:--force(?:\s|$)|(?:^|\s)-f(?=\s|$))"),
        "拒绝执行：命中 D-12（git push --force），会重写远端历史。",
        "改用 `git push --force-with-lease`，或新建分支 / `git revert` 保留历史。",
    ),
)

_A_PATTERNS: tuple[tuple[str, re.Pattern[str], str, str], ...] = (
    (
        "A-01", re.compile(r"\brm\s+(?:-[A-Za-z]+\s+)*-[A-Za-z]*[rf]"),
        "命中 A-01：递归/强制删除目录，操作不可恢复，需要人工确认。",
        "先 `ls` 核对目标；优先 `git clean -n` 预览，或把删除范围缩小到具体文件。",
    ),
    (
        "A-02", re.compile(r"\bgit\s+(?:checkout\s+--\s|restore\s)"),
        "命中 A-02：将丢弃工作区未提交的改动。",
        "先 `git stash push -m specflow-backup` 备份，再执行；或只还原指定路径。",
    ),
    (
        "A-03", re.compile(r"\bgit\s+reset\b(?![^;|&\n]*--hard)"),
        "命中 A-03：重置提交历史（非 --hard），可能移动分支指针。",
        "确认目标提交已备份或在临时分支；优先 `git revert <sha>` 保留历史。",
    ),
    (
        "A-04", re.compile(r"\b(?:curl\b[^|;]*\s-o\s|wget\b[^|;]*\s-O\s?)"),
        "命中 A-04：远程下载并写入本地文件。",
        "先下载到临时目录并人工审阅内容，再决定是否使用。",
    ),
    (
        "A-05", re.compile(r"\b(?:pip3?\s+install|npm\s+install|npm\s+i)\b"),
        "命中 A-05：安装/变更依赖，可能引入未在 design.md 选型表记录的依赖。",
        "先确认 design.md 选型表已记录该依赖；优先使用锁定版本安装。",
    ),
    (
        "A-06", re.compile(r"\bchmod\s+-R\s+777\b"),
        "命中 A-06：递归把目录权限放开到 777。",
        "改用最小权限：`chmod -R u+rwX,go+rX <dir>`。",
    ),
)

#: 复合命令分隔符：出现这些字符时不做整串白名单短路
_COMPOUND_RE = re.compile(r"&&|\|\||;|\n")
_DRIVE_ROOT_RE = re.compile(r"^(?:[A-Za-z]:\\?|\*)$")
_ROOT_TARGETS = ("/", "/*", "~", "~/", "$HOME", "/root", "/.")
_WRITE_TOOLS = {
    "write", "edit", "multiedit", "multi_edit", "notebookedit",
    "apply_patch", "str_replace_editor",
}
_COMMAND_TOOLS = {
    "bash", "shell", "sh", "powershell", "pwsh", "cmd", "terminal", "run_command",
}
_TEST_FILE_RE = re.compile(r"^test_.*\.py$")
_COMMAND_KEYS = ("command", "cmd", "script")
_PATH_KEYS = ("file_path", "path", "filepath", "target_file", "notebook_path")


# ---------------------------------------------------------------------- #
# 内部工具
# ---------------------------------------------------------------------- #
def _no_match(reason: str = "未命中规则，默认放行。") -> RuleHit:
    return RuleHit("", ALLOW, reason, None)


def _is_compound(text: str) -> bool:
    return bool(_COMPOUND_RE.search(text))


def _segments(text: str) -> list[str]:
    return [part.strip() for part in _COMPOUND_RE.split(text) if part.strip()]


def _has_rm_rf_flag(text: str) -> bool:
    return bool(re.search(r"(?:^|\s)-[A-Za-z]*(?:r|f)[A-Za-z]*(?:\s|$)", text))


def _root_target(command: str) -> str | None:
    """D-01：rm 的删除目标是根/家目录/盘符根。返回命中的目标串。"""
    if not re.search(r"\brm\b", command):
        return None
    if not _has_rm_rf_flag(command):
        return None
    for target in _ROOT_TARGETS:
        if re.search(rf"(?:^|\s){re.escape(target)}(?=\s|$)", command):
            return target
    if re.search(r"(?:^|\s)[A-Za-z]:\\?(?=\s|$|\*)", command):
        return "盘符根目录"
    return None


def _windows_force_del(command: str) -> str | None:
    """D-11：`del /f /s /q C:\\*` 这类强制删除盘符根。"""
    for match in re.finditer(r"(?:^|\s)del\s+((?:/[A-Za-z]+\s+)*)(\S+)", command, re.IGNORECASE):
        flags = match.group(1).lower()
        target = match.group(2)
        if not re.search(r"/[a-z]*[fsq]", flags):
            continue
        if _DRIVE_ROOT_RE.match(target.rstrip("*")) or re.match(r"^[A-Za-z]:\\?\*?$", target):
            return target
    return None


def _match_whitelist(text: str) -> RuleHit | None:
    for rule_id, pattern in _W_PATTERNS:
        if pattern.search(text):
            return RuleHit(rule_id, ALLOW, f"白名单放行（{rule_id}）：只读/测试类命令。", None)
    return None


def _match_danger(text: str) -> RuleHit | None:
    target = _root_target(text)
    if target is not None:
        return RuleHit(
            "D-01", DENY,
            f"拒绝执行：命中 D-01（删除根目录/家目录：{target}），不可恢复。",
            "如确需清理，请明确列出具体子目录并先 `ls` 核对；"
            "用 `git clean -n` 预览后再清理具体目录。",
        )
    target = _windows_force_del(text)
    if target is not None:
        return RuleHit(
            "D-11", DENY,
            f"拒绝执行：命中 D-11（Windows 强制删除盘符根目录：{target}）。",
            "不要强制删除盘符根目录；改用 `Remove-Item -LiteralPath <具体路径> -Recurse`"
            " 并先 `Get-ChildItem` 核对。",
        )
    for rule_id, pattern, reason, alternative in _D_PATTERNS:
        if pattern.search(text):
            return RuleHit(rule_id, DENY, reason, alternative)
    return None


def _match_confirm(text: str) -> RuleHit | None:
    for rule_id, pattern, reason, alternative in _A_PATTERNS:
        if pattern.search(text):
            return RuleHit(rule_id, CONFIRM, reason, alternative)
    return None


# ---------------------------------------------------------------------- #
# 命令类工具
# ---------------------------------------------------------------------- #
def check_command(command: str) -> RuleHit:
    """对一条 shell/PowerShell 命令给出 allow/confirm/deny 判定。"""
    text = (command or "").strip()
    if not text:
        return _no_match("空命令，直接放行。")

    if not _is_compound(text):
        hit = _match_whitelist(text)
        if hit is not None:
            return hit
        hit = _match_danger(text)
        if hit is not None:
            return hit
        hit = _match_confirm(text)
        return hit if hit is not None else _no_match()

    # 复合命令：整串先看危险（保留 `curl ... | sh` 语义），再逐段检查
    hit = _match_danger(text)
    if hit is not None:
        return hit
    for segment in _segments(text):
        hit = _match_danger(segment)
        if hit is not None:
            return hit
    for segment in _segments(text):
        hit = _match_confirm(segment)
        if hit is not None:
            return hit
    return _no_match("复合命令各子命令均未命中危险规则，放行。")


# ---------------------------------------------------------------------- #
# 写入类工具 + C 类范围检查
# ---------------------------------------------------------------------- #
def _resolve(path: Path | str, project_dir: Path | None) -> Path:
    target = Path(path).expanduser()
    if not target.is_absolute():
        base = project_dir or Path.cwd()
        target = base / target
    try:
        return target.resolve()
    except OSError:  # pragma: no cover - 极端路径
        return target


def _approved_artifacts(context: Mapping[str, Any]) -> set[str]:
    approved: set[str] = set(str(item) for item in (context.get("approved_artifacts") or ()))
    state = context.get("state")
    if isinstance(state, Mapping):
        artifacts = state.get("artifacts")
        if isinstance(artifacts, Mapping):
            for name, info in artifacts.items():
                if isinstance(info, Mapping) and info.get("status") == "approved":
                    approved.add(str(name))
    return approved


def _declared_files(task: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(task, Mapping):
        return []
    raw = task.get("files")
    if not isinstance(raw, (list, tuple)):
        return []
    declared: list[str] = []
    for item in raw:
        text = str(item).replace("\\", "/").strip()
        text = re.sub(r"^(?:\./)+", "", text)
        if text:
            declared.append(text)
    return declared


def _project_dir_of(context: Mapping[str, Any]) -> Path | None:
    raw = context.get("project_dir")
    if raw:
        return Path(raw)
    settings = context.get("settings")
    if settings is not None and getattr(settings, "project_dir", None):
        return Path(settings.project_dir)
    return None


def check_write(path: Path | str, context: Mapping[str, Any] | None = None) -> RuleHit:
    """对 Write/Edit 的目标路径做 C 类检查；无上下文时 C 类跳过。"""
    ctx: Mapping[str, Any] = context or {}
    project_dir = _project_dir_of(ctx)
    resolved = _resolve(path, project_dir)
    if project_dir is not None:
        rel = rel_posix(resolved, project_dir)
    else:
        rel = resolved.as_posix()
    name = resolved.name
    lower_name = name.lower()
    role = str(ctx.get("role") or "").strip().lower()

    # C-04 直改已批准产物
    artifacts_dir_raw = ctx.get("artifacts_dir")
    if not artifacts_dir_raw and project_dir is not None:
        artifacts_dir_raw = project_dir / ".spec" / "artifacts"
    if artifacts_dir_raw:
        artifacts_dir = Path(artifacts_dir_raw)
        if not artifacts_dir.is_absolute() and project_dir is not None:
            artifacts_dir = project_dir / artifacts_dir
        try:
            artifact_name = resolved.relative_to(artifacts_dir.resolve()).as_posix()
        except ValueError:
            artifact_name = ""
        if artifact_name and artifact_name.endswith(".md"):
            approved = _approved_artifacts(ctx)
            if artifact_name in approved or rel in approved:
                return RuleHit(
                    "C-04", DENY,
                    f"拒绝直接改写已批准产物 {artifact_name}：会破坏 hash 与门控一致性。",
                    f"走改版流程：`specflow gate revise {artifact_name}` 后再改，"
                    "或按 gate 流程重新批准。",
                )

    # C-02 测试角色写实现
    if role == "test_writer":
        looks_like_impl = rel.startswith("src/") or (
            lower_name.endswith(".py") and not lower_name.startswith("test_")
        )
        if looks_like_impl:
            return RuleHit(
                "C-02", DENY,
                f"拒绝写入 {rel}：test_writer 角色只能写测试文件，不得写实现代码。",
                "实现代码由 executor 在实现阶段写入；test_writer 请只写 tests/ 下的 test_*.py。",
            )

    # C-03 Executor 改既有测试
    if role in {"executor", "impl", "implementer"} and _TEST_FILE_RE.match(lower_name):
        return RuleHit(
            "C-03", DENY,
            f"拒绝修改 {rel}：executor 不得删除/改写既有测试文件。",
            "实现阶段只改 src/ 实现文件；需要调整测试时交 test_writer 或人工确认。",
        )

    # C-01 写入越界
    task = ctx.get("task")
    if isinstance(task, Mapping) and (task.get("id") or task.get("files")):
        declared = _declared_files(task)
        task_id = str(task.get("id") or "当前任务")
        if rel not in declared:
            declared_text = "、".join(declared) if declared else "（未声明任何文件）"
            return RuleHit(
                "C-01", DENY,
                f"拒绝写入 {rel}：不在 {task_id} 声明的写入范围内"
                f"（声明：{declared_text}）。",
                "如需扩大范围，先更新 tasks.md 的 files[] 并重新门控，再继续实现。",
            )

    return _no_match("写入路径未命中 C 类规则。")


def check_path_scope(
    path: Path,
    task: Mapping[str, Any],
    *,
    role: str,
    project_dir: Path,
    tasks_artifact: Any | None = None,
) -> RuleHit:
    """C 类纯函数（供 PreToolUse 与 TDD Executor 复用）。

    `task` 为 tasks.md 附录中的单任务；缺 task/files 时视为「未声明范围」→ deny。
    `tasks_artifact` 仅用于诊断（可选），不参与判定。
    """
    del tasks_artifact  # 判定只依赖 task 自身的 files[]，保留参数以符合 07 契约
    ctx: dict[str, Any] = {"role": role, "project_dir": project_dir}
    if not isinstance(task, Mapping) or not (
        task.get("id") or task.get("files")
    ):
        return RuleHit(
            "C-01", DENY,
            f"拒绝写入 {path}：缺少任务范围上下文（tasks.md 中找不到当前任务声明）。",
            "先确认 tasks.md 当前任务并带上 task（含 files[]）再写入；"
            "如需扩大范围请先更新 tasks.md 并重新门控。",
        )
    ctx["task"] = task
    return check_write(path, ctx)


# ---------------------------------------------------------------------- #
# 工具调用入口
# ---------------------------------------------------------------------- #
def _extract_command(tool_input: Mapping[str, Any]) -> str | None:
    for key in _COMMAND_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, (list, tuple)) and value:
            parts = [str(item) for item in value]
            return " ".join(parts)
    return None


def _extract_path(tool_input: Mapping[str, Any]) -> str | None:
    for key in _PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def check_tool_use(
    tool_name: str,
    tool_input: Mapping[str, Any],
    context: Mapping[str, Any] | None = None,
) -> RuleHit:
    """PreToolUse 的统一入口：命令类走 W/D/A，写类走 C。"""
    name = (tool_name or "").strip().lower()
    payload: Mapping[str, Any] = tool_input or {}
    if name in _COMMAND_TOOLS:
        command = _extract_command(payload)
        return check_command(command or "")
    if name in _WRITE_TOOLS:
        path = _extract_path(payload)
        if not path:
            return _no_match("写类工具未提供 file_path，跳过 C 类检查。")
        return check_write(path, context)
    return _no_match(f"工具 {tool_name or '未知'} 不在规则表覆盖范围，放行。")