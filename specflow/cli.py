"""specflow CLI：init / run / status / gate / hook / doctor / restore / unlock。

设计：子命令只做参数解析与展示，业务逻辑下沉到 engine/artifacts/hooks；
所有重模块在 handler 内**延迟 import**，保证 `python -m specflow hook ...` 在
其他模块尚未就绪时也能工作（并行开发期）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from specflow import __version__
from specflow.config import Settings
from specflow.errors import NeedsHuman, SpecflowError, UsageError
from specflow.util import dump_json, load_json, parse_kv_flags, read_text

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse")

_MINIMAL_AGENTS = """# AGENTS.md — {repo} 基线规则

> 版本：v0.1 ｜ 更新：{stamp} ｜ 由 specflow 维护，人工可编辑
> 提示：执行 `specflow run "/project-init"` 生成完整仓库画像与命令基线。

## 1. 仓库画像
- 待 `/project-init` 生成

## 2. 工程规则（每条都必须可判定）
- R-01 文件读写使用 `pathlib` 与 `encoding="utf-8"`
- R-02 不引入新依赖，除非在 `design.md` 选型表中列出理由

## 3. 流程规则
- 进入 `/plan-task` 前 `design.md` 必须 approved
- 阶段产物写入 `.spec/artifacts/`，不得写到仓库根

## 4. 命令基线
| 用途 | 命令 |
|---|---|
| 测试 | `pytest -q` |

## 5. 变更记录
- v0.1 ← 初始化（`specflow init`）
"""


# ---------------------------------------------------------------------- #
# 参数解析
# ---------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="specflow",
        description="Spec 驱动的 Agentic 研发流水线（9 阶段命令 + 4 Hooks + 多角色 TDD）",
    )
    parser.add_argument("--version", action="version", version=f"specflow {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="在目标仓库安装 .spec 骨架、hooks.json、AGENTS.md 初版")
    p_init.add_argument("--target", default=None, help="目标仓库路径（默认当前目录）")
    p_init.add_argument("--force", action="store_true", help="覆盖已有 AGENTS.md/hooks.json")
    p_init.set_defaults(func=cmd_init)

    p_run = sub.add_parser("run", aliases=["stage"], help='执行阶段命令，如 run "/prd-input docs/prd/visit-import.md"')
    p_run.add_argument("words", nargs=argparse.REMAINDER,
                       help="阶段命令与参数（--task/--all/--file 等原样透传）")
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="展示 9 阶段（2 初始化 + 7 主链路）/ 门控 / 产物状态")
    p_status.add_argument("--check", action="store_true", help="做产物字段与 hash 校验")
    p_status.add_argument("--json", action="store_true", help="输出 JSON")
    p_status.set_defaults(func=cmd_status)

    p_gate = sub.add_parser("gate", help="人工门控：approve/reject/supersede/revise")
    p_gate.add_argument("action", choices=("approve", "reject", "supersede", "revise"))
    p_gate.add_argument("name", help="门控名或产物名：design / agents-md / design.md")
    p_gate.add_argument("--as", dest="as_user", default=None, help="操作人（写入 state.json）")
    p_gate.add_argument("--note", default=None, help="门控说明/打回意见")
    p_gate.set_defaults(func=cmd_gate)

    p_hook = sub.add_parser("hook", help="Hook 协议入口（读 stdin JSON，写 stdout JSON）")
    p_hook.add_argument("event", choices=HOOK_EVENTS)
    p_hook.set_defaults(func=cmd_hook)

    p_doctor = sub.add_parser("doctor", help="环境与配置自检")
    p_doctor.add_argument("--json", action="store_true", help="输出 JSON")
    p_doctor.set_defaults(func=cmd_doctor)

    p_restore = sub.add_parser("restore", help="从 .spec/backup 回滚产物")
    p_restore.add_argument("name", help="产物名，如 design.md")
    p_restore.add_argument("--version", type=int, default=None, help="版本号，默认倒数第一个备份")
    p_restore.set_defaults(func=cmd_restore)

    p_unlock = sub.add_parser("unlock", help="清理残留写锁")
    p_unlock.add_argument("name", help="产物名，如 design.md")
    p_unlock.add_argument("--force", action="store_true", help="确认清理")
    p_unlock.set_defaults(func=cmd_unlock)

    p_web = sub.add_parser("web", help="启动本地 Web 控制台（阶段/门控/产物/Trace/运行）")
    p_web.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    p_web.add_argument("--port", type=int, default=8030, help="端口（默认 8030）")
    p_web.add_argument("--project", default=None, help="目标仓库路径（默认当前目录）")
    p_web.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    p_web.set_defaults(func=cmd_web)
    return parser


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    _harden_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        active = settings or _settings_for(args)
        result = args.func(args, active)
        return int(result or 0)
    except SpecflowError as exc:
        print(f"[specflow] {exc.render()}", file=sys.stderr)
        return exc.exit_code
    except BrokenPipeError:  # pragma: no cover - 终端管道
        return 0


def entrypoint() -> None:  # console_scripts 入口
    raise SystemExit(main(sys.argv[1:]))


def _harden_stdio() -> None:
    """Windows GBK 控制台下避免因个别符号（如 ✓）抛 UnicodeEncodeError。

    保留原有编码，只把不可编码字符替换成 '?'；重定向/管道场景同样安全。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # pragma: no cover - 非常规流
            pass


def _settings_for(args: argparse.Namespace) -> Settings:
    target = getattr(args, "target", None)
    return Settings.load(target)


# ---------------------------------------------------------------------- #
# init
# ---------------------------------------------------------------------- #
def cmd_init(args: argparse.Namespace, settings: Settings) -> int:
    from specflow.engine.state import State, StateStore
    from specflow.util import now_iso, today_stamp

    settings.ensure_spec_dirs()
    store = StateStore(settings.state_path)
    if not settings.state_path.exists() or args.force:
        store.save(State(owner=settings.owner, updated_at=now_iso()))
    if not settings.artifacts_index_path.exists() or args.force:
        dump_json(
            settings.artifacts_index_path,
            {"schema_version": 1, "generated_at": now_iso(), "artifacts": {}, "files": {}},
        )

    hooks_target = settings.project_dir / "hooks.json"
    hooks_source = settings.platform_root / "hooks.json"
    if not hooks_source.exists():
        hooks_source = settings.package_dir / "hooks.json"
    if (not hooks_target.exists() or args.force) and hooks_source.exists():
        hooks_target.write_text(read_text(hooks_source), encoding="utf-8", newline="\n")

    agents = settings.project_dir / "AGENTS.md"
    if not agents.exists() or args.force:
        agents.write_text(
            _MINIMAL_AGENTS.format(repo=settings.project_dir.name, stamp=now_iso()[:10]),
            encoding="utf-8",
            newline="\n",
        )

    _append_gitignore(settings.project_dir)

    print(f"[specflow] init 完成：{settings.project_dir}")
    print(f"  .spec/            {settings.spec_dir.name}/ (artifacts/traces/locks/backup/index)")
    print(f"  hooks.json        {(settings.project_dir / 'hooks.json').exists()}")
    if not (settings.project_dir / "hooks.json").exists():
        print("  ! hooks.json 未写入：模板缺失（安装包可能未包含 hooks.json；可用 pip install -e . 从源码安装）")
    print(f"  AGENTS.md         已就绪（v0.1 初版）")
    print("下一步：specflow run \"/project-init\"")
    return 0


def _append_gitignore(project_dir: Path) -> None:
    path = project_dir / ".gitignore"
    entries = [".spec/traces/", ".spec/locks/", ".spec/cache/", ".env", ".demo/", "__pycache__/"]
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    missing = [entry for entry in entries if entry not in existing.splitlines()]
    if not missing:
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    header = "" if existing else "# specflow\n"
    path.write_text(existing + prefix + header + "\n".join(missing) + "\n", encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------- #
# run
# ---------------------------------------------------------------------- #
def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    from specflow.artifacts.store import ArtifactStore
    from specflow.engine.runner import StageRunner
    from specflow.engine.stages import get_stage, load_stages
    from specflow.artifacts.indexer import Indexer
    from specflow.llm import get_llm
    from specflow.platforms import get_platform

    words = _split_words(args.words)
    if not words:
        raise UsageError('用法：specflow run "/<阶段命令> [参数]"，例如 run "/plan-task"')
    command = words[0]
    if not command.startswith("/"):
        command = "/" + command
    stage = get_stage(command)
    if stage is None:
        available = "、".join(load_stages(settings.skills_dir).keys())
        raise UsageError(f"未知阶段命令：{command}\n可用命令：{available}")

    positionals, flags = parse_kv_flags(words[1:])
    run_args = _run_args(stage.slug, positionals, flags)
    runner = StageRunner(settings, ArtifactStore(settings), _state_store(settings),
                         Indexer(settings), get_llm(settings), get_platform(settings))
    result = runner.run(stage, run_args)
    _print_run_result(result)
    return result.exit_code


def _run_args(slug: str, positionals: list[str], flags: dict[str, Any]) -> dict[str, Any]:
    file_arg = flags.get("file")
    if file_arg is None and positionals:
        file_arg = positionals[0]
    return {
        "file": file_arg,
        "doc": flags.get("doc"),
        "scope": flags.get("scope"),
        "note": flags.get("note"),
        "task": flags.get("task"),
        "all": bool(flags.get("all")),
        "max_hours": flags.get("max_hours", 4),
        "skip_rpc": bool(flags.get("skip_rpc")),
        "fixture": flags.get("fixture"),
        "dry_run": bool(flags.get("dry_run")),
        "force": bool(flags.get("force")),
        "slug": slug,
    }


def _print_run_result(result: Any) -> None:
    print(result.summary)
    for name in result.artifacts:
        print(f"  产物：.spec/artifacts/{name}")
    for line in (result.details or {}).get("notes", []):
        print(f"  {line}")


def _split_words(words: list[str]) -> list[str]:
    if len(words) == 1 and " " in words[0].strip():
        return words[0].split()
    return list(words)


def _state_store(settings: Settings):
    from specflow.engine.state import StateStore

    return StateStore(settings.state_path)


# ---------------------------------------------------------------------- #
# status
# ---------------------------------------------------------------------- #
def cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    from specflow.engine.status import collect_status, render_status

    report = collect_status(settings, check=args.check)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_status(report))
    return 0 if report.get("ok", True) else 2


# ---------------------------------------------------------------------- #
# gate
# ---------------------------------------------------------------------- #
def cmd_gate(args: argparse.Namespace, settings: Settings) -> int:
    from specflow.engine.actions import gate_action
    from specflow.identity import resolve_operator

    result = gate_action(
        settings,
        action=args.action,
        name=args.name,
        by=resolve_operator(settings, args.as_user),
        note=args.note,
    )
    print(result["summary"])
    return 0


# ---------------------------------------------------------------------- #
# hook
# ---------------------------------------------------------------------- #
def cmd_hook(args: argparse.Namespace, settings: Settings) -> int:
    from specflow.hooks.manager import run_hook

    return run_hook(args.event, settings)


# ---------------------------------------------------------------------- #
# doctor
# ---------------------------------------------------------------------- #
def cmd_doctor(args: argparse.Namespace, settings: Settings) -> int:
    from specflow.doctor import run_doctor

    report = run_doctor(settings)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(report["rendered"])
    return 0 if report["ok"] else 1


# ---------------------------------------------------------------------- #
# restore / unlock
# ---------------------------------------------------------------------- #
def cmd_web(args: argparse.Namespace, settings: Settings) -> int:
    from specflow.web import serve

    target = getattr(args, "project", None)
    if target:
        settings = Settings.load(target)
    serve(settings, host=args.host, port=args.port, open_browser=args.open)
    return 0


def cmd_restore(args: argparse.Namespace, settings: Settings) -> int:
    from specflow.artifacts.store import ArtifactStore
    from specflow.engine.state import StateStore

    store = ArtifactStore(settings)
    artifact = store.restore(args.name, args.version)
    # 回滚后同步 state 的 hash/版本，避免 status --check 把"人工回滚"误报为 tampered。
    state_store = StateStore(settings.state_path)
    state = state_store.load()
    state.record_artifact(
        artifact.name,
        artifact.version,
        artifact.hash,
        artifact.status,
        str(artifact.meta.get("stage") or ""),
    )
    state_store.save(state)
    print(f"[specflow] 已回滚 {artifact.name} 到 v{artifact.version}（hash {artifact.hash}）")
    return 0


def cmd_unlock(args: argparse.Namespace, settings: Settings) -> int:
    stem = Path(args.name).stem
    lock_path = settings.locks_dir / f"{stem}.lock"
    if not lock_path.exists():
        print(f"[specflow] 没有残留锁：{lock_path.name}")
        return 0
    info = load_json(lock_path, {}) or {}
    if not args.force:
        raise NeedsHuman(
            f"{args.name} 正被持有：owner={info.get('owner')} pid={info.get('pid')} "
            f"expire_at={info.get('expire_at')}；确认后加 --force 清理。"
        )
    stale = lock_path.with_suffix(f".lock.stale.{int(__import__('time').time())}")
    lock_path.replace(stale)
    print(f"[specflow] 已清理残留锁（owner={info.get('owner')} pid={info.get('pid')}），留证：{stale.name}")
    return 0