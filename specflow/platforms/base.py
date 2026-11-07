"""平台适配层协议：docs / feature / pipeline / rpc / sso 五个子域（docs/07 §5.9）。

本期只有 `MockPlatform` 实现；未来接入真实平台时新增同接口实现，
调用方（`engine/runner.py` 的 S1/S4/S6）不需要改动。
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable


@runtime_checkable
class PlatformAdapter(Protocol):
    """平台适配器统一接口。

    返回结构以 docs/07 §5.9「Mock 返回结构（固定键）」为准：
    实现方必须保证这些键存在，额外键可追加但不得改名。
    """

    # ---- sso ---------------------------------------------------------- #
    def whoami(self) -> dict[str, Any]:
        """返回当前登录用户：user_id / name / display_name / email / roles / teams。"""
        ...

    # ---- docs-shuttle ------------------------------------------------- #
    def docs_list(self) -> dict[str, Any]:
        """返回文档列表：{"items": [{doc_id, title, status, url}]}。"""
        ...

    def docs_get(self, doc_id: str) -> dict[str, Any]:
        """返回文档正文与图片：doc_id / title / text / images / claimed_by。"""
        ...

    def docs_claim(self, doc_id: str, owner: str) -> dict[str, Any]:
        """认领文档；已被他人认领时不抢占。返回 doc_id / claimed_by / claimed。"""
        ...

    def docs_ocr(self, doc_id: str) -> dict[str, Any]:
        """返回文档内嵌图片的 OCR 文本：doc_id / images / text / count。"""
        ...

    def docs_download(self, doc_id: str) -> dict[str, Any]:
        """下载文档原文：doc_id / filename / content_type / content(bytes) / bytes。"""
        ...

    # ---- feature / branch --------------------------------------------- #
    def feature_create(self, name: str, owner: str) -> dict[str, Any]:
        """按名称创建（或复用）feature：feature_id / name / branch / owner / created。"""
        ...

    def feature_bind(self, feature_id: str, task_id: str, branch: str) -> dict[str, Any]:
        """把 tasks.md 的任务绑定到 feature 分支：feature_id / task_id / branch / bound。"""
        ...

    def feature_contract(self, feature_id: str) -> dict[str, Any]:
        """返回 feature 的交付契约（分支、必过检查、评审人）。"""
        ...

    # ---- pipeline ------------------------------------------------------ #
    def pipeline_trigger(self, branch: str) -> dict[str, Any]:
        """触发 CI，返回 {"run_id": "RUN-xxxx", "branch": branch}。"""
        ...

    def pipeline_status(self, run_id: str) -> dict[str, Any]:
        """轮询 CI：run_id / status(passed|failed) / duration_s / url。"""
        ...

    # ---- rpc ----------------------------------------------------------- #
    def rpc_call(self, method: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """调用 staging RPC：成功 {ok, method, result}，失败 {ok, method, error}。"""
        ...
