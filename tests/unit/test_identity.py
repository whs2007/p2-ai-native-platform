"""身份解析单测：--as 覆盖 / live 走 SSO whoami / mock 用 owner。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from specflow.config import Settings
from specflow.errors import PlatformError
from specflow.identity import resolve_operator
from specflow.platforms.stub_server import make_server


@pytest.fixture()
def stub():
    server = make_server(port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _settings(tmp_path: Path, server, token: str = "dev-token") -> Settings:
    host, port = server.server_address[:2]
    return Settings.load(
        project_dir=tmp_path,
        overrides={
            "LLM_MODE": "mock",
            "PLATFORM_MODE": "live",
            "PLATFORM_BASE_URL": f"http://{host}:{port}",
            "PLATFORM_TOKEN": token,
            "SPECFLOW_OWNER": "local-owner",
        },
    )


def test_mock_uses_owner(tmp_path: Path) -> None:
    settings = Settings.load(
        project_dir=tmp_path,
        overrides={"LLM_MODE": "mock", "PLATFORM_MODE": "mock", "SPECFLOW_OWNER": "local-owner"},
    )
    assert resolve_operator(settings) == "local-owner"


def test_explicit_as_overrides(tmp_path: Path) -> None:
    settings = Settings.load(
        project_dir=tmp_path,
        overrides={"LLM_MODE": "mock", "PLATFORM_MODE": "mock", "SPECFLOW_OWNER": "local-owner"},
    )
    assert resolve_operator(settings, "auditor") == "auditor"


def test_live_uses_sso_whoami(tmp_path: Path, stub) -> None:
    assert resolve_operator(_settings(tmp_path, stub)) == "lin.xu"


def test_live_sso_failure_raises(tmp_path: Path, stub) -> None:
    with pytest.raises(PlatformError):
        resolve_operator(_settings(tmp_path, stub, token="wrong-token"))