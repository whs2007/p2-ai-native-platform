"""Mock 平台适配器：从 `platforms/mock_data/*.json` 查表返回确定性数据（docs/07 §5.9）。

设计约束：
- 无网络、无 sleep、无随机；同一 adapter 实例内 `docs_claim` / `feature_create`
  的状态在内存中累积，跨实例不共享（每次演示都是干净起点）。
- 所有返回值都是深拷贝，调用方修改不会污染 fixture 数据。
- fixture 通过 `Settings.platform_fixture` 注入：`pipeline-fail` / `rpc-error`；
  未知 fixture、未知 doc_id、未登记的 rpc 方法一律抛 `UsageError`，把 fixture
  问题暴露在演示现场而不是静默降级。
"""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Mapping

from specflow.config import Settings
from specflow.errors import UsageError
from specflow.util import load_json

MOCK_DATA_DIR = Path(__file__).resolve().parent / "mock_data"

FIXTURE_PIPELINE_FAIL = "pipeline-fail"
FIXTURE_RPC_ERROR = "rpc-error"
_KNOWN_FIXTURES = frozenset({FIXTURE_PIPELINE_FAIL, FIXTURE_RPC_ERROR})
_DEFAULT_FIXTURE_ALIASES = frozenset({"", "default", "none", "ok", "passed"})


def _normalise_fixture(raw: str | None) -> str | None:
    """把配置值规范成已知 fixture 名；空值与 default/none 视为「无 fixture」。"""
    text = (raw or "").strip().lower()
    if text in _DEFAULT_FIXTURE_ALIASES:
        return None
    if text not in _KNOWN_FIXTURES:
        allowed = "、".join(sorted(_KNOWN_FIXTURES))
        raise UsageError(f"未知平台 fixture：{raw!r}（可选：{allowed}）")
    return text


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or "feature"


class MockPlatform:
    """`PlatformAdapter` 的 mock 实现：五份 JSON 各管一个子域。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.fixture = _normalise_fixture(settings.platform_fixture)

        self._docs_data = self._read("docs_shuttle")
        self._feature_data = self._read("feature")
        self._pipeline_data = self._read("pipeline")
        self._rpc_data = self._read("rpc")
        self._sso_data = self._read("sso")

        # 实例内可变状态：认领关系 / 新建的 feature / 绑定关系
        self._claimed: dict[str, str | None] = {
            str(doc.get("doc_id")): doc.get("claimed_by")
            for doc in self._documents()
        }
        self._features: list[dict[str, Any]] = copy.deepcopy(
            self._feature_data.get("features") or []
        )
        self._bindings: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # 数据装载
    # ------------------------------------------------------------------ #
    def _read(self, name: str) -> dict[str, Any]:
        path = MOCK_DATA_DIR / f"{name}.json"
        data = load_json(path, default=None)
        if not isinstance(data, dict) or not data:
            raise UsageError(f"mock 数据缺失或非法：{path.as_posix()}")
        return data

    def _documents(self) -> list[dict[str, Any]]:
        documents = self._docs_data.get("documents")
        if not isinstance(documents, list) or not documents:
            raise UsageError("mock 数据 docs_shuttle.json 缺少 documents 列表")
        return documents

    def _document(self, doc_id: str) -> dict[str, Any]:
        for doc in self._documents():
            if str(doc.get("doc_id")) == str(doc_id):
                return doc
        raise UsageError(f"mock 未知文档：{doc_id}（可先调 docs_list 查看）")

    def _feature(self, feature_id: str) -> dict[str, Any]:
        for feature in self._features:
            if str(feature.get("feature_id")) == str(feature_id):
                return feature
        raise UsageError(f"mock 未知 feature：{feature_id}")

    def _runs(self) -> dict[str, Any]:
        runs = self._pipeline_data.get("runs")
        if not isinstance(runs, dict) or not runs:
            raise UsageError("mock 数据 pipeline.json 缺少 runs")
        return runs

    # ------------------------------------------------------------------ #
    # sso
    # ------------------------------------------------------------------ #
    def whoami(self) -> dict[str, Any]:
        users = self._sso_data.get("users") or {}
        default_user = str(self._sso_data.get("default_user") or "")
        user = users.get(default_user) if isinstance(users, dict) else None
        if not isinstance(user, dict):
            raise UsageError(f"mock 数据 sso.json 缺少默认用户：{default_user!r}")
        return copy.deepcopy(user)

    # ------------------------------------------------------------------ #
    # docs-shuttle
    # ------------------------------------------------------------------ #
    def docs_list(self) -> dict[str, Any]:
        items = [
            {
                "doc_id": doc.get("doc_id"),
                "title": doc.get("title"),
                "status": doc.get("status"),
                "url": doc.get("url"),
            }
            for doc in self._documents()
        ]
        return {"items": copy.deepcopy(items)}

    def docs_get(self, doc_id: str) -> dict[str, Any]:
        doc = self._document(doc_id)
        return {
            "doc_id": doc.get("doc_id"),
            "title": doc.get("title"),
            "text": doc.get("text", ""),
            "images": copy.deepcopy(doc.get("images") or []),
            "claimed_by": self._claimed.get(str(doc.get("doc_id"))),
        }

    def docs_claim(self, doc_id: str, owner: str) -> dict[str, Any]:
        doc = self._document(doc_id)
        key = str(doc.get("doc_id"))
        existing = self._claimed.get(key)
        if existing and existing != owner:
            return {
                "doc_id": doc.get("doc_id"),
                "claimed_by": existing,
                "claimed": False,
                "reason": f"已被 {existing} 认领，不能重复认领",
            }
        self._claimed[key] = owner
        return {"doc_id": doc.get("doc_id"), "claimed_by": owner, "claimed": True}

    def docs_ocr(self, doc_id: str) -> dict[str, Any]:
        doc = self._document(doc_id)
        images = copy.deepcopy(doc.get("images") or [])
        parts = [str(image.get("text", "")) for image in images if image.get("text")]
        return {
            "doc_id": doc.get("doc_id"),
            "images": images,
            "text": "\n".join(parts),
            "count": len(images),
        }

    def docs_download(self, doc_id: str) -> dict[str, Any]:
        doc = self._document(doc_id)
        content = str(doc.get("text") or "").encode("utf-8")
        return {
            "doc_id": doc.get("doc_id"),
            "filename": f"{doc.get('doc_id')}.md",
            "content_type": "text/markdown",
            "content": content,
            "bytes": len(content),
        }

    # ------------------------------------------------------------------ #
    # feature / branch
    # ------------------------------------------------------------------ #
    def feature_create(self, name: str, owner: str) -> dict[str, Any]:
        wanted = str(name).strip()
        for feature in self._features:
            if wanted in {str(feature.get("name", "")), str(feature.get("title", ""))}:
                return {
                    "feature_id": feature.get("feature_id"),
                    "name": feature.get("name"),
                    "branch": feature.get("branch"),
                    "owner": owner or feature.get("owner"),
                    "created": False,
                }
        feature_id = self._next_feature_id()
        created = {
            "feature_id": feature_id,
            "name": wanted,
            "title": wanted,
            "owner": owner,
            "branch": f"feature/{_slugify(wanted)}",
            "base_branch": "main",
            "status": "active",
            "contract": {
                "feature_id": feature_id,
                "title": wanted,
                "branch": f"feature/{_slugify(wanted)}",
                "base_branch": "main",
                "owner": owner,
                "required_checks": ["python -m pytest -q"],
                "reviewers": [owner],
                "merge_policy": "squash",
                "notes": "由 mock 现场创建，仅用于演示",
            },
        }
        self._features.append(created)
        return {
            "feature_id": feature_id,
            "name": wanted,
            "branch": created["branch"],
            "owner": owner,
            "created": True,
        }

    def _next_feature_id(self) -> str:
        highest = 1024
        for feature in self._features:
            match = re.match(r"FEAT-(\d+)$", str(feature.get("feature_id", "")))
            if match:
                highest = max(highest, int(match.group(1)))
        return f"FEAT-{highest + 1}"

    def feature_bind(self, feature_id: str, task_id: str, branch: str) -> dict[str, Any]:
        feature = self._feature(feature_id)
        binding = {
            "feature_id": feature.get("feature_id"),
            "task_id": str(task_id),
            "branch": branch or feature.get("branch"),
        }
        self._bindings.append(binding)
        return {**binding, "bound": True}

    def feature_contract(self, feature_id: str) -> dict[str, Any]:
        feature = self._feature(feature_id)
        contract = feature.get("contract")
        if not isinstance(contract, dict):
            raise UsageError(f"mock feature 缺少 contract：{feature_id}")
        result = copy.deepcopy(contract)
        result.setdefault("feature_id", feature.get("feature_id"))
        return result

    # ------------------------------------------------------------------ #
    # pipeline
    # ------------------------------------------------------------------ #
    def pipeline_trigger(self, branch: str) -> dict[str, Any]:
        key = "failed_run_id" if self.fixture == FIXTURE_PIPELINE_FAIL else "default_run_id"
        run_id = self._pipeline_data.get(key)
        if not isinstance(run_id, str) or run_id not in self._runs():
            raise UsageError(f"mock 数据 pipeline.json 缺少 {key}")
        return {"run_id": run_id, "branch": branch or self._runs()[run_id].get("branch", "")}

    def pipeline_status(self, run_id: str) -> dict[str, Any]:
        runs = self._runs()
        run = runs.get(str(run_id))
        if not isinstance(run, dict):
            raise UsageError(f"mock 未知 pipeline run：{run_id}")
        status = str(run.get("status", "passed"))
        if self.fixture == FIXTURE_PIPELINE_FAIL:
            status = "failed"
        return {
            "run_id": run_id,
            "status": status,
            "duration_s": int(run.get("duration_s", 0)),
            "url": str(run.get("url", "")),
        }

    # ------------------------------------------------------------------ #
    # rpc
    # ------------------------------------------------------------------ #
    def rpc_call(self, method: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise UsageError("rpc payload 必须是 Mapping")
        if self.fixture == FIXTURE_RPC_ERROR:
            error = self._rpc_data.get("error") or {}
            return {
                "ok": False,
                "method": method,
                "error": {
                    "code": str(error.get("code", "RPC_PLATFORM_ERROR")),
                    "message": str(error.get("message", "平台 RPC 调用失败（mock fixture）")),
                },
            }
        methods = self._rpc_data.get("methods") or {}
        entry = methods.get(method) if isinstance(methods, dict) else None
        if not isinstance(entry, dict):
            raise UsageError(f"mock 未登记的 rpc 方法：{method}（见 mock_data/rpc.json）")
        return {
            "ok": True,
            "method": method,
            "result": copy.deepcopy(entry.get("result") or {}),
        }
