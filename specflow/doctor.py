"""`specflow doctor`：环境与配置自检（docs/07 §5.10）。"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from specflow import __version__
from specflow.config import Settings
from specflow.util import read_text


@dataclass
class CheckItem:
    name: str
    status: str  # PASS | WARN | FAIL
    detail: str

    def render(self) -> str:
        return f"[{self.status}] {self.name}: {self.detail}"


def run_doctor(settings: Settings) -> dict[str, Any]:
    checks: list[CheckItem] = []

    checks.append(
        CheckItem(
            "python",
            "PASS" if sys.version_info >= (3, 11) else "FAIL",
            f"{sys.version.split()[0]}（要求 >=3.11）",
        )
    )
    checks.append(CheckItem("specflow", "PASS", f"v{__version__} @ {settings.platform_root}"))

    mode = settings.effective_llm_mode
    if mode == "live":
        checks.append(
            CheckItem(
                "llm_mode",
                "PASS" if settings.llm_api_key else "FAIL",
                f"live（model={settings.llm_model}，key={'set' if settings.llm_api_key else 'missing'}，"
                f"base={settings.llm_base_url}）",
            )
        )
        if not settings.llm_api_key:
            checks.append(CheckItem("llm_key", "FAIL", "LLM_MODE=live 但没有 LLM_API_KEY；可切 LLM_MODE=mock"))
    elif mode == "mock":
        checks.append(CheckItem("llm_mode", "PASS", "mock（离线确定，零网络）"))
    else:  # pragma: no cover - Settings 已校验
        checks.append(CheckItem("llm_mode", "FAIL", f"未知模式 {mode}"))

    platform_mode = settings.platform_mode
    if platform_mode == "live":
        missing = []
        if not settings.platform_base_url:
            missing.append("PLATFORM_BASE_URL")
        if not settings.platform_token:
            missing.append("PLATFORM_TOKEN")
        checks.append(
            CheckItem(
                "platform_mode",
                "PASS" if not missing else "FAIL",
                f"live @ {settings.platform_base_url or '未配置'}；token="
                f"{'set' if settings.platform_token else 'missing'}"
                + (f"；缺少 {'/'.join(missing)}" if missing else ""),
            )
        )
    else:
        checks.append(
            CheckItem(
                "platform_mode",
                "PASS" if platform_mode == "mock" else "FAIL",
                f"{platform_mode}（mock=本地 JSON，live=HTTP 适配层）",
            )
        )

    project = settings.project_dir
    checks.append(
        CheckItem("project_dir", "PASS" if project.is_dir() else "FAIL", str(project))
    )
    spec_path = settings.state_path
    if not spec_path.exists():
        checks.append(
            CheckItem("spec_init", "WARN", f"未初始化：cd {project} 后执行 specflow init")
        )
    else:
        try:
            from specflow.engine.state import StateStore

            StateStore(spec_path).load()
            checks.append(CheckItem("spec_init", "PASS", str(settings.spec_dir)))
        except Exception as exc:  # noqa: BLE001 - doctor 的职责就是把损坏变成可读结论
            checks.append(
                CheckItem(
                    "spec_init",
                    "FAIL",
                    f"{spec_path} 无法解析：{exc}；备份后可执行 specflow init --force 重建",
                )
            )
    hooks_target = project / "hooks.json"
    checks.append(
        CheckItem(
            "hooks_json",
            "PASS" if hooks_target.exists() else "WARN",
            str(hooks_target) if hooks_target.exists() else "缺失：specflow init 会写入",
        )
    )

    agents = project / "AGENTS.md"
    checks.append(
        CheckItem(
            "agents_md",
            "PASS" if agents.exists() else "WARN",
            f"{agents.name} {_parse_version(read_text(agents)) if agents.exists() else '（未生成）'}",
        )
    )

    skills = sorted(settings.skills_dir.glob("*/SKILL.md")) if settings.skills_dir.exists() else []
    checks.append(
        CheckItem(
            "skills",
            "PASS" if len(skills) >= 9 else "FAIL",
            f"{len(skills)} 份 SKILL.md（要求 9）@ {settings.skills_dir}",
        )
    )

    fixture = settings.fixture_repo
    checks.append(
        CheckItem(
            "fixtures",
            "PASS" if fixture.is_dir() else "WARN",
            f"demo-repo {'存在' if fixture.is_dir() else '未随包分发（仅开发仓库提供，跳过）'} @ {fixture}",
        )
    )

    mock_data = settings.package_dir / "platforms" / "mock_data"
    mock_files = sorted(mock_data.glob("*.json")) if mock_data.exists() else []
    checks.append(
        CheckItem(
            "platform_mock_data",
            "PASS" if len(mock_files) >= 5 else "FAIL",
            f"{len(mock_files)} 份 JSON（要求 5）@ {mock_data}",
        )
    )

    templates = [item for item in settings.templates_dir.glob("*") if item.is_file()] if settings.templates_dir.exists() else []
    checks.append(
        CheckItem(
            "templates",
            "PASS" if len(templates) >= 3 else "WARN",
            f"{len(templates)} 份 @ {settings.templates_dir}",
        )
    )

    writable = _check_writable(project)
    checks.append(CheckItem("writable", "PASS" if writable else "FAIL", "项目目录可写" if writable else "项目目录不可写"))

    pytest_available = importlib.util.find_spec("pytest") is not None
    checks.append(
        CheckItem("pytest", "PASS" if pytest_available else "WARN", "已安装（用于 Verifier/QA）" if pytest_available else "未安装：pip install pytest")
    )

    ok = all(item.status != "FAIL" for item in checks)
    return {
        "ok": ok,
        "checks": [{"name": item.name, "status": item.status, "detail": item.detail} for item in checks],
        "rendered": "\n".join(item.render() for item in checks)
        + "\n\n"
        + ("环境自检通过。" if ok else "存在 FAIL 项，请按提示修复。"),
    }


def _check_writable(project: Path) -> bool:
    probe = project / ".spec" / ".doctor_probe"
    try:
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _parse_version(text: str) -> str:
    import re

    match = re.search(r"版本：v(\d+)\.(\d+)", text or "")
    return f"v{match.group(1)}.{match.group(2)}" if match else "（版本未知）"