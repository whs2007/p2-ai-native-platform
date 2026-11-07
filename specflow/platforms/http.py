"""平台适配层：HTTP REST + Bearer 的 live 实现（docs/07 §5.9）。

约定（可被真实企业网关替换，只需保持路径/字段）：

- `GET    {base}/api/v1/sso/whoami`
- `GET    {base}/api/v1/docs`
- `GET    {base}/api/v1/docs/{doc_id}`
- `POST   {base}/api/v1/docs/{doc_id}/claim`      body: {"owner": "..."}
- `POST   {base}/api/v1/docs/{doc_id}/ocr`        body: {}
- `POST   {base}/api/v1/features`                 body: {"name": "...", "owner": "..."}
- `POST   {base}/api/v1/features/{id}/bind`       body: {"task_id": "...", "branch": "..."}
- `GET    {base}/api/v1/features/{id}/contract`
- `POST   {base}/api/v1/pipelines`                body: {"branch": "..."}
- `GET    {base}/api/v1/pipelines/{run_id}`
- `POST   {base}/api/v1/rpc/{method}`             body: 业务 payload

鉴权：`Authorization: Bearer <PLATFORM_TOKEN>`。
网络：仅标准库 `urllib`，超时 `PLATFORM_TIMEOUT_S`；5xx/连接错误重试 1 次，4xx 立即失败。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Mapping
from urllib.parse import quote

from specflow.config import Settings
from specflow.errors import PlatformError


class HttpPlatform:
    """对照真实企业平台网关的 HTTP 实现；字段形状与 `MockPlatform` 一致。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.platform_base_url.rstrip("/")
        self.token = settings.platform_token
        self.timeout_s = max(1.0, float(settings.platform_timeout_s))

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #
    def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not self.base_url:
            raise PlatformError("PLATFORM_MODE=live 但未配置 PLATFORM_BASE_URL")
        if not self.token:
            raise PlatformError("PLATFORM_MODE=live 但未配置 PLATFORM_TOKEN")
        url = f"{self.base_url}{path}"
        data = json.dumps(dict(payload or {}), ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "specflow/1.0",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        last_error: Exception | None = None
        for attempt in range(2):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                    body = response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                if 500 <= exc.code < 600 and attempt == 0:
                    last_error = exc
                    continue
                raise PlatformError(f"平台 HTTP {exc.code}：{method} {path}；响应：{detail}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt == 0:
                    last_error = exc
                    continue
                raise PlatformError(f"平台请求失败（已重试 1 次）：{method} {path}；{exc}") from exc
            if not body.strip():
                return {}
            try:
                result = json.loads(body)
            except json.JSONDecodeError as exc:
                raise PlatformError(f"平台返回非 JSON：{method} {path}；{body[:200]!r}") from exc
            if not isinstance(result, dict):
                raise PlatformError(f"平台返回不是对象：{method} {path}；{type(result).__name__}")
            return result
        raise PlatformError(f"平台请求失败：{method} {path}；{last_error}")

    def _request_bytes(self, method: str, path: str) -> dict[str, Any]:
        if not self.base_url:
            raise PlatformError("PLATFORM_MODE=live 但未配置 PLATFORM_BASE_URL")
        if not self.token:
            raise PlatformError("PLATFORM_MODE=live 但未配置 PLATFORM_TOKEN")
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "*/*",
            "User-Agent": "specflow/1.0",
        }
        last_error: Exception | None = None
        for attempt in range(2):
            request = urllib.request.Request(url, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                    content = response.read()
                    content_type = (response.headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0].strip()
                    disposition = response.headers.get("Content-Disposition") or ""
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                if 500 <= exc.code < 600 and attempt == 0:
                    last_error = exc
                    continue
                raise PlatformError(f"平台 HTTP {exc.code}：GET {path}；响应：{detail}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt == 0:
                    last_error = exc
                    continue
                raise PlatformError(f"平台下载失败（已重试 1 次）：GET {path}；{exc}") from exc
            filename = ""
            match = re.search(r'filename="?([^";]+)"?', disposition)
            if match:
                filename = match.group(1)
            return {
                "content": content,
                "content_type": content_type,
                "filename": filename,
                "bytes": len(content),
            }
        raise PlatformError(f"平台下载失败：GET {path}；{last_error}")

    # ------------------------------------------------------------------ #
    # sso
    # ------------------------------------------------------------------ #
    def whoami(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/sso/whoami")

    # ------------------------------------------------------------------ #
    # docs-shuttle
    # ------------------------------------------------------------------ #
    def docs_list(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/docs")

    def docs_get(self, doc_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/docs/{quote(str(doc_id), safe='')}")

    def docs_claim(self, doc_id: str, owner: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/docs/{quote(str(doc_id), safe='')}/claim",
            {"owner": owner},
        )

    def docs_ocr(self, doc_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/docs/{quote(str(doc_id), safe='')}/ocr",
            {},
        )

    def docs_download(self, doc_id: str) -> dict[str, Any]:
        return self._request_bytes(
            "GET",
            f"/api/v1/docs/{quote(str(doc_id), safe='')}/download",
        )

    # ------------------------------------------------------------------ #
    # feature / branch
    # ------------------------------------------------------------------ #
    def feature_create(self, name: str, owner: str) -> dict[str, Any]:
        return self._request("POST", "/api/v1/features", {"name": name, "owner": owner})

    def feature_bind(self, feature_id: str, task_id: str, branch: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/features/{quote(str(feature_id), safe='')}/bind",
            {"task_id": task_id, "branch": branch},
        )

    def feature_contract(self, feature_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v1/features/{quote(str(feature_id), safe='')}/contract",
        )

    # ------------------------------------------------------------------ #
    # pipeline / rpc
    # ------------------------------------------------------------------ #
    def pipeline_trigger(self, branch: str) -> dict[str, Any]:
        return self._request("POST", "/api/v1/pipelines", {"branch": branch})

    def pipeline_status(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/pipelines/{quote(str(run_id), safe='')}")

    def rpc_call(self, method: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise PlatformError("rpc payload 必须是 Mapping")
        return self._request("POST", f"/api/v1/rpc/{quote(str(method), safe='')}", payload)