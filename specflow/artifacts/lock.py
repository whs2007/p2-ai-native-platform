"""跨进程写锁：`.spec/locks/<artifact>.lock`（docs/07 §3.5 / §4.5、docs/04 §8）。

实现：`O_CREAT | O_EXCL` 原子创建，**失败不重试不等待**，直接 `LockBusy`（退出码 3）。
TTL 120s；过期允许抢占，旧锁改名为 `<artifact>.lock.stale.<ts>` 留证。
只写者（CLI / runner）加锁，Hooks 只读不加锁。
"""
from __future__ import annotations

import json
import os
import socket
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from specflow.errors import LockBusy
from specflow.util import atomic_write_text, now_iso, parse_iso

_O_BINARY = getattr(os, "O_BINARY", 0)


class FileLock:
    """单产物的跨进程写锁（上下文管理器）。"""

    def __init__(
        self,
        path: Path,
        artifact: str,
        owner: str,
        session_id: str,
        ttl_s: int = 120,
    ) -> None:
        self.path = Path(path)
        self.artifact = artifact
        self.owner = owner
        self.session_id = session_id
        self.ttl_s = max(1, int(ttl_s))
        self._acquired = False
        self.info: dict[str, Any] | None = None

    # ------------------------------------------------------------------ #
    # 获取 / 续期 / 释放
    # ------------------------------------------------------------------ #
    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._new_payload()
        text = json.dumps(payload, ensure_ascii=False)
        for _attempt in range(2):
            try:
                handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_BINARY, 0o644)
            except FileExistsError:
                existing = self._read()
                remaining = self._remaining_seconds(existing)
                if remaining > 0:
                    raise LockBusy(self._busy_message(existing, remaining))
                self._steal()
                continue
            try:
                os.write(handle, text.encode("utf-8"))
            finally:
                os.close(handle)
            self._acquired = True
            self.info = payload
            return
        raise LockBusy(
            f"{self.artifact} 写锁竞争失败（已被其它会话抢占），请稍后重试或 "
            f"specflow unlock {Path(self.artifact).stem} --force"
        )

    def renew(self) -> None:
        """续期：重写 `expire_at`（长阶段每 30s 调一次）。"""
        existing = self._read()
        if existing is None:
            raise LockBusy(f"{self.artifact} 写锁已丢失（无法续期），请重新获取或 specflow unlock 后重试")
        payload = dict(existing)
        payload["expire_at"] = self._expire_at()
        atomic_write_text(self.path, json.dumps(payload, ensure_ascii=False))
        self.info = payload

    def release(self) -> None:
        """释放；锁不存在不报错（`finally` 里安全调用）。"""
        self._acquired = False
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        except OSError:  # pragma: no cover - 权限等异常不阻塞主流程
            return

    @property
    def acquired(self) -> bool:
        return self._acquired

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001 - 上下文协议
        self.release()

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _new_payload(self) -> dict[str, Any]:
        return {
            "artifact": self.artifact,
            "pid": os.getpid(),
            "owner": self.owner,
            "session_id": self.session_id,
            "host": socket.gethostname(),
            "acquired_at": now_iso(),
            "expire_at": self._expire_at(),
        }

    def _expire_at(self) -> str:
        return (datetime.now().astimezone() + timedelta(seconds=self.ttl_s)).isoformat(timespec="seconds")

    def _read(self) -> dict[str, Any] | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _remaining_seconds(self, info: dict[str, Any] | None) -> int:
        if not info:
            return 0
        expire_at = info.get("expire_at")
        if not expire_at:
            return 0
        try:
            expire = parse_iso(str(expire_at))
        except (TypeError, ValueError):
            return 0
        if expire.tzinfo is None:
            expire = expire.astimezone()
        remaining = (expire - datetime.now().astimezone()).total_seconds()
        return max(0, int(remaining))

    def _busy_message(self, info: dict[str, Any] | None, remaining: int) -> str:
        info = info or {}
        owner = info.get("owner") or "?"
        pid = info.get("pid") or "?"
        session = info.get("session_id") or "ses_unknown"
        return (
            f"{self.artifact} 正被 {session} 写入（owner={owner}，pid={pid}，剩余 {remaining}s），"
            f"请稍后重试或 specflow unlock {Path(self.artifact).stem} --force"
        )

    def _steal(self) -> None:
        """TTL 过期：把旧锁改名留证，随后重新尝试创建。"""
        stale = self.path.with_name(f"{self.path.name}.stale.{int(time.time())}")
        try:
            os.replace(self.path, stale)
        except FileNotFoundError:
            return
        except OSError:
            return