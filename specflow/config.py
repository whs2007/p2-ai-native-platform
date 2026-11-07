"""Settings：环境变量 + `.env` + 显式覆盖项（docs/07 §2.1）。

优先级：overrides > 进程环境变量 > <project_dir>/.env > <platform_root>/.env > 默认值。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from specflow.errors import UsageError

_PLATFORM_ROOT = Path(__file__).resolve().parent.parent

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _as_bool(raw: Any, default: bool = False) -> bool:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def _as_float(raw: Any, default: float) -> float:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _as_int(raw: Any, default: int) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def read_dotenv(path: Path) -> dict[str, str]:
    """解析 KEY=VALUE 形式的 .env；忽略注释/空行，去除值两端引号。"""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, PermissionError, UnicodeDecodeError):
        return values
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


@dataclass(frozen=True)
class Settings:
    project_dir: Path
    spec_dir: Path
    llm_mode: str = "auto"
    platform_mode: str = "mock"
    platform_base_url: str = ""
    platform_token: str = ""
    platform_timeout_s: float = 10.0
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.3
    llm_timeout_s: float = 60.0
    llm_json_mode: bool = True
    llm_cache: bool = False
    owner: str = "specflow-user"
    strict_hooks: bool = False
    max_rounds: int = 3
    gate_repeat_limit: int = 3
    session_budget: int = 900
    prompt_budget: int = 300
    platform_fixture: str | None = None
    git_branch_enabled: bool = False
    platform_root: Path = _PLATFORM_ROOT

    # ------------------------------------------------------------------ #
    # 派生路径
    # ------------------------------------------------------------------ #
    @property
    def artifacts_dir(self) -> Path:
        return self.spec_dir / "artifacts"

    @property
    def traces_dir(self) -> Path:
        return self.spec_dir / "traces"

    @property
    def backup_dir(self) -> Path:
        return self.spec_dir / "backup"

    @property
    def locks_dir(self) -> Path:
        return self.spec_dir / "locks"

    @property
    def index_dir(self) -> Path:
        return self.spec_dir / "index"

    @property
    def cache_dir(self) -> Path:
        return self.spec_dir / "cache" / "llm"

    @property
    def state_path(self) -> Path:
        return self.spec_dir / "state.json"

    @property
    def artifacts_index_path(self) -> Path:
        return self.index_dir / "artifacts.json"

    @property
    def package_dir(self) -> Path:
        """specflow 包目录（安装后 = site-packages/specflow）。"""
        return Path(__file__).resolve().parent

    @property
    def skills_dir(self) -> Path:
        # 随包分发，保证 pip install 后仍能找到 9 个阶段定义
        return self.package_dir / "skills"

    @property
    def templates_dir(self) -> Path:
        return self.package_dir / "templates"

    @property
    def fixture_repo(self) -> Path:
        return self.platform_root / "fixtures" / "demo-repo"

    @property
    def effective_llm_mode(self) -> str:
        if self.llm_mode == "auto":
            return "live" if self.llm_api_key else "mock"
        return self.llm_mode

    # ------------------------------------------------------------------ #
    # 构造
    # ------------------------------------------------------------------ #
    @classmethod
    def load(
        cls,
        project_dir: Path | str | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> "Settings":
        overrides = dict(overrides or {})
        project = Path(
            project_dir
            or os.environ.get("SPECFLOW_PROJECT_DIR")
            or os.getcwd()
        ).expanduser().resolve()

        dotenvs = [read_dotenv(project / ".env"), read_dotenv(_PLATFORM_ROOT / ".env")]

        def pick(name: str, default: Any = None) -> Any:
            if name in overrides and overrides[name] is not None:
                return overrides[name]
            if name in os.environ and os.environ[name] != "":
                return os.environ[name]
            for dotenv in dotenvs:
                if name in dotenv and dotenv[name] != "":
                    return dotenv[name]
            return default

        spec_name = str(pick("SPECFLOW_SPEC_DIR", ".spec")).strip() or ".spec"
        spec_dir = (project / spec_name).resolve()

        llm_mode = str(pick("LLM_MODE", pick("SPECFLOW_MODE", "auto"))).strip().lower()
        if llm_mode not in {"auto", "live", "mock"}:
            raise UsageError(f"LLM_MODE 非法：{llm_mode}（可选 auto/live/mock）")
        platform_mode = str(pick("PLATFORM_MODE", "mock")).strip().lower()
        if platform_mode not in {"mock", "live"}:
            raise UsageError(f"PLATFORM_MODE 非法：{platform_mode}（可选 mock/live）")
        platform_base_url = str(pick("PLATFORM_BASE_URL", "") or "").strip()
        platform_token = str(pick("PLATFORM_TOKEN", "") or "").strip()
        platform_timeout_s = _as_float(pick("PLATFORM_TIMEOUT_S", 10.0), 10.0)
        if platform_mode == "live":
            missing = [
                name
                for name, value in (
                    ("PLATFORM_BASE_URL", platform_base_url),
                    ("PLATFORM_TOKEN", platform_token),
                )
                if not value
            ]
            if missing:
                raise UsageError(f"PLATFORM_MODE=live 需要配置 {' 和 '.join(missing)}")

        fixture = str(pick("SPECFLOW_PLATFORM_FIXTURE", "") or "").strip() or None

        return cls(
            project_dir=project,
            spec_dir=spec_dir,
            llm_mode=llm_mode,
            platform_mode=platform_mode,
            platform_base_url=platform_base_url,
            platform_token=platform_token,
            platform_timeout_s=platform_timeout_s,
            llm_base_url=str(pick("LLM_BASE_URL", "https://api.deepseek.com/v1")).strip(),
            llm_api_key=str(pick("LLM_API_KEY", "") or "").strip(),
            llm_model=str(pick("LLM_MODEL", "deepseek-chat")).strip(),
            llm_temperature=_as_float(pick("LLM_TEMPERATURE", 0.3), 0.3),
            llm_timeout_s=_as_float(pick("LLM_TIMEOUT_S", 60.0), 60.0),
            llm_json_mode=_as_bool(pick("LLM_JSON_MODE", "1"), True),
            llm_cache=_as_bool(pick("LLM_CACHE", "0"), False),
            owner=str(pick("SPECFLOW_OWNER", "specflow-user")).strip() or "specflow-user",
            strict_hooks=_as_bool(pick("SPECFLOW_STRICT_HOOKS", "0"), False),
            max_rounds=_as_int(pick("SPECFLOW_MAX_ROUNDS", 3), 3),
            gate_repeat_limit=_as_int(pick("SPECFLOW_GATE_REPEAT_LIMIT", 3), 3),
            session_budget=_as_int(pick("SPECFLOW_SESSION_BUDGET", 900), 900),
            prompt_budget=_as_int(pick("SPECFLOW_PROMPT_BUDGET", 300), 300),
            platform_fixture=fixture,
            git_branch_enabled=_as_bool(pick("SPECFLOW_GIT_BRANCH", "0"), False),
        )

    def with_overrides(self, **kw: Any) -> "Settings":
        unknown = set(kw) - {f.name for f in self.__dataclass_fields__.values()}
        if unknown:
            raise UsageError(f"未知配置项：{', '.join(sorted(unknown))}")
        return replace(self, **kw)

    def ensure_spec_dirs(self) -> None:
        for path in (
            self.spec_dir,
            self.artifacts_dir,
            self.traces_dir,
            self.backup_dir,
            self.locks_dir,
            self.index_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)