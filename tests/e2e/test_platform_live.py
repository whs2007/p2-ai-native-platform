"""E2E：平台 live 适配层（本地 stub，不访问外网）。

验证 `PLATFORM_MODE=live` 时，`/prd-input --doc` 真的经 HTTP 平台取文档/OCR，
而不是走 mock 分支。
"""
from __future__ import annotations

import threading
from pathlib import Path

from specflow.artifacts.store import ArtifactStore
from specflow.cli import main
from specflow.platforms.stub_server import make_server


def test_live_platform_prd_input(settings, repo: Path) -> None:
    server = make_server(port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    live = settings.with_overrides(
        platform_mode="live",
        platform_base_url=f"http://{host}:{port}",
        platform_token="dev-token",
        platform_timeout_s=5.0,
    )
    try:
        assert main(["init"], settings=live) == 0
        assert main(["run", "/project-init"], settings=live) == 0
        assert main(["run", "/domain-input"], settings=live) == 0
        assert main(["run", "/prd-input --doc DOC-2041"], settings=live) == 0

        prd = ArtifactStore(live).load("prd.md")
        meta = prd.meta or {}
        assert meta["source"]["type"] == "docs-shuttle"
        downloaded_to = meta["source"]["downloaded_to"]
        assert downloaded_to and (repo / downloaded_to).is_file()
        assert (repo / downloaded_to).read_text(encoding="utf-8").strip()
        appendix = prd.appendix() or {}
        assert appendix["requirements"]
        paths = [item["path"] for item in server.requests]
        assert "/api/v1/docs/DOC-2041/claim" in paths
        assert "/api/v1/docs/DOC-2041" in paths
        assert "/api/v1/docs/DOC-2041/ocr" in paths
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)