"""Settings 单元测试：优先级、模式解析、派生路径（docs/07 §2.1）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from specflow.config import Settings
from specflow.errors import UsageError


def test_defaults_and_derived_paths(tmp_path: Path) -> None:
    settings = Settings.load(project_dir=tmp_path, overrides={"LLM_MODE": "mock"})
    assert settings.project_dir == tmp_path.resolve()
    assert settings.spec_dir == (tmp_path / ".spec").resolve()
    assert settings.artifacts_dir == settings.spec_dir / "artifacts"
    assert settings.state_path == settings.spec_dir / "state.json"
    assert settings.artifacts_index_path == settings.spec_dir / "index" / "artifacts.json"
    assert settings.effective_llm_mode == "mock"


def test_auto_mode_uses_key(tmp_path: Path) -> None:
    # 平台根目录可能存在开发者本地 .env（含真实 Key），显式清空 key 以保证用例与机器无关
    no_key = Settings.load(
        project_dir=tmp_path, overrides={"LLM_MODE": "auto", "LLM_API_KEY": ""}
    )
    assert no_key.effective_llm_mode == "mock"
    live = Settings.load(
        project_dir=tmp_path, overrides={"LLM_MODE": "auto", "LLM_API_KEY": "sk-test"}
    )
    assert live.effective_llm_mode == "live"


def test_env_priority_over_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("LLM_MODEL=from-dotenv\nLLM_TEMPERATURE=0.9\n", encoding="utf-8")
    monkeypatch.setenv("LLM_MODEL", "from-env")
    settings = Settings.load(project_dir=tmp_path, overrides={"LLM_MODE": "mock"})
    assert settings.llm_model == "from-env"
    assert settings.llm_temperature == 0.9
    overridden = Settings.load(
        project_dir=tmp_path, overrides={"LLM_MODE": "mock", "LLM_MODEL": "from-override"}
    )
    assert overridden.llm_model == "from-override"


def test_invalid_mode_rejected(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        Settings.load(project_dir=tmp_path, overrides={"LLM_MODE": "remote"})
    with pytest.raises(UsageError):
        Settings.load(project_dir=tmp_path, overrides={"PLATFORM_MODE": "remote"})


def test_platform_live_requires_endpoint_and_token(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        Settings.load(project_dir=tmp_path, overrides={"PLATFORM_MODE": "live"})
    with pytest.raises(UsageError):
        Settings.load(
            project_dir=tmp_path,
            overrides={"PLATFORM_MODE": "live", "PLATFORM_BASE_URL": "http://127.0.0.1:1"},
        )
    live = Settings.load(
        project_dir=tmp_path,
        overrides={
            "PLATFORM_MODE": "live",
            "PLATFORM_BASE_URL": "http://127.0.0.1:1",
            "PLATFORM_TOKEN": "test-token",
        },
    )
    assert live.platform_mode == "live"
    assert live.platform_timeout_s > 0


def test_spec_dir_override(tmp_path: Path) -> None:
    settings = Settings.load(
        project_dir=tmp_path, overrides={"LLM_MODE": "mock", "SPECFLOW_SPEC_DIR": ".spec2"}
    )
    assert settings.spec_dir.name == ".spec2"