"""HttpPlatform 契约测试：用本地 stub 验证 REST 路径/字段/Bearer 鉴权（不访问外网）。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from specflow.config import Settings
from specflow.errors import PlatformError
from specflow.platforms import get_platform
from specflow.platforms.http import HttpPlatform
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
            "PLATFORM_TIMEOUT_S": "5",
        },
    )


def test_get_platform_returns_http_adapter(tmp_path: Path, stub) -> None:
    platform = get_platform(_settings(tmp_path, stub))
    assert isinstance(platform, HttpPlatform)


def test_whoami(tmp_path: Path, stub) -> None:
    user = HttpPlatform(_settings(tmp_path, stub)).whoami()
    assert user["user_id"] == "U-1001"
    assert user["name"] == "lin.xu"


def test_docs_flow(tmp_path: Path, stub) -> None:
    platform = HttpPlatform(_settings(tmp_path, stub))
    listed = platform.docs_list()
    assert any(item["doc_id"] == "DOC-2041" for item in listed["items"])

    doc = platform.docs_get("DOC-2041")
    assert doc["doc_id"] == "DOC-2041"
    assert "text" in doc and doc["images"]

    first = platform.docs_claim("DOC-2041", "alice")
    assert first["claimed"] is True and first["claimed_by"] == "alice"
    second = platform.docs_claim("DOC-2041", "bob")
    assert second["claimed"] is False and second["claimed_by"] == "alice"

    ocr = platform.docs_ocr("DOC-2041")
    assert ocr["count"] >= 1 and ocr["text"]


def test_docs_download(tmp_path: Path, stub) -> None:
    platform = HttpPlatform(_settings(tmp_path, stub))
    download = platform.docs_download("DOC-2041")
    assert download["bytes"] > 0
    assert download["content_type"] == "text/markdown"
    assert "拜访" in download["content"].decode("utf-8")
    assert download["filename"] == "DOC-2041.md"


def test_feature_bind_and_contract(tmp_path: Path, stub) -> None:
    platform = HttpPlatform(_settings(tmp_path, stub))
    created = platform.feature_create("audit-http-feature", "alice")
    assert created["created"] is True
    assert created["branch"].startswith("feature/")

    bound = platform.feature_bind(created["feature_id"], "T1", created["branch"])
    assert bound["bound"] is True and bound["task_id"] == "T1"

    contract = platform.feature_contract(created["feature_id"])
    assert contract["feature_id"] == created["feature_id"]
    assert contract["required_checks"]


def test_pipeline_and_rpc(tmp_path: Path, stub) -> None:
    platform = HttpPlatform(_settings(tmp_path, stub))
    trigger = platform.pipeline_trigger("feature/audit-http")
    assert trigger["run_id"] == "RUN-2031"

    status = platform.pipeline_status(trigger["run_id"])
    assert status["status"] == "passed"
    assert status["run_id"] == "RUN-2031"

    rpc = platform.rpc_call("visit.import.validate", {"feature_id": "FEAT-1024"})
    assert rpc["ok"] is True
    assert rpc["result"]["cases"] == 5


def test_bad_token_raises_platform_error(tmp_path: Path, stub) -> None:
    platform = HttpPlatform(_settings(tmp_path, stub, token="wrong-token"))
    with pytest.raises(PlatformError) as excinfo:
        platform.whoami()
    assert "401" in str(excinfo.value)


def test_stub_records_requests(tmp_path: Path, stub) -> None:
    platform = HttpPlatform(_settings(tmp_path, stub))
    platform.whoami()
    platform.docs_list()
    paths = [item["path"] for item in stub.requests]
    assert "/api/v1/sso/whoami" in paths
    assert "/api/v1/docs" in paths