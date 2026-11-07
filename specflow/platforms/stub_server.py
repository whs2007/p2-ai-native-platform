"""可独立运行的平台 stub（本地 HTTP；用于 live 适配层演示与契约测试）。

不是企业系统实现：只按 `specflow/platforms/http.py` 的 REST 契约返回
`mock_data/*.json` 中的预置数据，用来证明“换实现不改调用方”。

单独启动：

    python scripts/platform_stub.py --port 8765 --token dev-token

然后：

    PLATFORM_MODE=live PLATFORM_BASE_URL=http://127.0.0.1:8765 \
    PLATFORM_TOKEN=dev-token python -m specflow run "/prd-input --doc DOC-2041"
"""
from __future__ import annotations

import copy
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from specflow.util import load_json

MOCK_DATA_DIR = Path(__file__).resolve().parent / "mock_data"


def _read(name: str) -> dict[str, Any]:
    data = load_json(MOCK_DATA_DIR / f"{name}.json", default=None)
    if not isinstance(data, dict) or not data:
        raise RuntimeError(f"stub 数据缺失：{MOCK_DATA_DIR / f'{name}.json'}")
    return data


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).strip().lower()).strip("-")
    return slug or "feature"


class StubPlatformServer(ThreadingHTTPServer):
    """内存态 stub；每个实例独立，可并行跑测试。"""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], token: str = "dev-token") -> None:
        super().__init__(address, _StubHandler)
        self.token = token
        self.docs = _read("docs_shuttle")
        self.sso = _read("sso")
        self.feature_data = _read("feature")
        self.pipeline = _read("pipeline")
        self.rpc = _read("rpc")
        self.features: list[dict[str, Any]] = copy.deepcopy(self.feature_data.get("features") or [])
        self.claimed: dict[str, str | None] = {
            str(doc.get("doc_id")): doc.get("claimed_by") for doc in self._documents()
        }
        self.bindings: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []

    def _documents(self) -> list[dict[str, Any]]:
        return list(self.docs.get("documents") or [])

    def document(self, doc_id: str) -> dict[str, Any]:
        for doc in self._documents():
            if str(doc.get("doc_id")) == str(doc_id):
                return doc
        raise KeyError(doc_id)

    def feature(self, feature_id: str) -> dict[str, Any]:
        for item in self.features:
            if str(item.get("feature_id")) == str(feature_id):
                return item
        raise KeyError(feature_id)

    def next_feature_id(self) -> str:
        highest = 1024
        for feature in self.features:
            match = re.match(r"FEAT-(\d+)$", str(feature.get("feature_id", "")))
            if match:
                highest = max(highest, int(match.group(1)))
        return f"FEAT-{highest + 1}"


class _StubHandler(BaseHTTPRequestHandler):
    server_version = "SpecflowStub/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # pragma: no cover - 由 --quiet 控制
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------------ #
    # 基础
    # ------------------------------------------------------------------ #
    @property
    def stub(self) -> StubPlatformServer:
        return self.server  # type: ignore[return-value]

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status: int, body: bytes, content_type: str, filename: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {self.stub.token}"

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return value

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        self.stub.requests.append({"method": method, "path": path, "query": parsed.query})
        if not self._authorized():
            self._send(401, {"error": "unauthorized", "message": "缺少或错误的 Bearer Token"})
            return
        try:
            if method == "GET":
                self._handle_get(path)
            else:
                self._handle_post(path)
        except KeyError as exc:
            self._send(404, {"error": "not_found", "message": str(exc)})
        except ValueError as exc:
            self._send(400, {"error": "bad_request", "message": str(exc)})
        except Exception as exc:  # pragma: no cover - stub 兜底
            self._send(500, {"error": "internal", "message": str(exc)})

    # ------------------------------------------------------------------ #
    # GET
    # ------------------------------------------------------------------ #
    def _handle_get(self, path: str) -> None:
        stub = self.stub
        if path == "/api/v1/sso/whoami":
            users = stub.sso.get("users") or {}
            default_user = str(stub.sso.get("default_user") or "")
            self._send(200, copy.deepcopy(users.get(default_user) or {}))
            return
        if path == "/api/v1/docs":
            items = [
                {"doc_id": d.get("doc_id"), "title": d.get("title"), "status": d.get("status"), "url": d.get("url")}
                for d in stub._documents()
            ]
            self._send(200, {"items": items})
            return
        if path.startswith("/api/v1/docs/") and path.endswith("/download"):
            doc_id = unquote(path[len("/api/v1/docs/"): -len("/download")])
            doc = stub.document(doc_id)
            body = str(doc.get("text") or "").encode("utf-8")
            self._send_bytes(200, body, "text/markdown; charset=utf-8", f"{doc_id}.md")
            return
        if path.startswith("/api/v1/docs/"):
            doc_id = unquote(path[len("/api/v1/docs/"):])
            doc = stub.document(doc_id)
            self._send(
                200,
                {
                    "doc_id": doc.get("doc_id"),
                    "title": doc.get("title"),
                    "text": doc.get("text", ""),
                    "images": copy.deepcopy(doc.get("images") or []),
                    "claimed_by": stub.claimed.get(str(doc.get("doc_id"))),
                },
            )
            return
        if path.startswith("/api/v1/features/") and path.endswith("/contract"):
            feature_id = unquote(path[len("/api/v1/features/"): -len("/contract")])
            contract = stub.feature(feature_id).get("contract")
            if not isinstance(contract, dict):
                raise KeyError(f"feature {feature_id} 无 contract")
            self._send(200, copy.deepcopy(contract))
            return
        if path.startswith("/api/v1/pipelines/"):
            run_id = unquote(path[len("/api/v1/pipelines/"):])
            runs = stub.pipeline.get("runs") or {}
            run = runs.get(run_id)
            if not isinstance(run, dict):
                raise KeyError(run_id)
            self._send(
                200,
                {
                    "run_id": run_id,
                    "status": run.get("status", "passed"),
                    "duration_s": int(run.get("duration_s", 0)),
                    "url": str(run.get("url", "")),
                },
            )
            return
        self._send(404, {"error": "not_found", "message": path})

    # ------------------------------------------------------------------ #
    # POST
    # ------------------------------------------------------------------ #
    def _handle_post(self, path: str) -> None:
        stub = self.stub
        body = self._body()
        if path.startswith("/api/v1/docs/") and path.endswith("/claim"):
            doc_id = unquote(path[len("/api/v1/docs/"): -len("/claim")])
            stub.document(doc_id)
            owner = str(body.get("owner") or "")
            existing = stub.claimed.get(doc_id)
            if existing and existing != owner:
                self._send(200, {"doc_id": doc_id, "claimed_by": existing, "claimed": False, "reason": f"已被 {existing} 认领"})
                return
            stub.claimed[doc_id] = owner
            self._send(200, {"doc_id": doc_id, "claimed_by": owner, "claimed": True})
            return
        if path.startswith("/api/v1/docs/") and path.endswith("/ocr"):
            doc_id = unquote(path[len("/api/v1/docs/"): -len("/ocr")])
            doc = stub.document(doc_id)
            images = copy.deepcopy(doc.get("images") or [])
            text = "\n".join(str(img.get("text", "")) for img in images if img.get("text"))
            self._send(200, {"doc_id": doc_id, "images": images, "text": text, "count": len(images)})
            return
        if path == "/api/v1/features":
            name = str(body.get("name") or "").strip()
            owner = str(body.get("owner") or "")
            for feature in stub.features:
                if name in {str(feature.get("name", "")), str(feature.get("title", ""))}:
                    self._send(200, {"feature_id": feature.get("feature_id"), "name": feature.get("name"), "branch": feature.get("branch"), "owner": owner or feature.get("owner"), "created": False})
                    return
            feature_id = stub.next_feature_id()
            branch = f"feature/{_slugify(name)}"
            created = {
                "feature_id": feature_id,
                "name": name,
                "title": name,
                "owner": owner,
                "branch": branch,
                "base_branch": "main",
                "status": "active",
                "contract": {
                    "feature_id": feature_id,
                    "title": name,
                    "branch": branch,
                    "base_branch": "main",
                    "owner": owner,
                    "required_checks": ["python -m pytest -q"],
                    "reviewers": [owner],
                    "merge_policy": "squash",
                    "notes": "stub 创建，仅用于本地契约演示",
                },
            }
            stub.features.append(created)
            self._send(200, {"feature_id": feature_id, "name": name, "branch": branch, "owner": owner, "created": True})
            return
        if path.startswith("/api/v1/features/") and path.endswith("/bind"):
            feature_id = unquote(path[len("/api/v1/features/"): -len("/bind")])
            feature = stub.feature(feature_id)
            binding = {"feature_id": feature.get("feature_id"), "task_id": str(body.get("task_id") or ""), "branch": body.get("branch") or feature.get("branch")}
            stub.bindings.append(binding)
            self._send(200, {**binding, "bound": True})
            return
        if path == "/api/v1/pipelines":
            run_id = stub.pipeline.get("default_run_id")
            runs = stub.pipeline.get("runs") or {}
            if not isinstance(run_id, str) or run_id not in runs:
                raise KeyError("default_run_id")
            self._send(200, {"run_id": run_id, "branch": body.get("branch") or runs[run_id].get("branch", "")})
            return
        if path.startswith("/api/v1/rpc/"):
            method = unquote(path[len("/api/v1/rpc/"):])
            methods = stub.rpc.get("methods") or {}
            entry = methods.get(method)
            if not isinstance(entry, dict):
                raise KeyError(f"未知 rpc 方法：{method}")
            self._send(200, {"ok": True, "method": method, "result": copy.deepcopy(entry.get("result") or {})})
            return
        self._send(404, {"error": "not_found", "message": path})


def make_server(host: str = "127.0.0.1", port: int = 0, token: str = "dev-token", verbose: bool = False) -> StubPlatformServer:
    server = StubPlatformServer((host, port), token=token)
    server.verbose = verbose  # type: ignore[attr-defined]
    return server