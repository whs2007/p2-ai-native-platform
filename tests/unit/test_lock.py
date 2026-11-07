"""跨进程写锁：TTL / 抢占 / 第二写者拦截（docs/07 §4.5、docs/04 §8、AC-22）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from specflow.artifacts.lock import FileLock
from specflow.errors import LockBusy

PLATFORM_ROOT = Path(__file__).resolve().parents[2]


def lock_path(tmp_path: Path) -> Path:
    return tmp_path / ".spec" / "locks" / "design.lock"


def make_lock(
    tmp_path: Path,
    *,
    owner: str = "alice",
    session_id: str = "ses_a",
    ttl_s: int = 120,
) -> FileLock:
    return FileLock(lock_path(tmp_path), "design.md", owner=owner, session_id=session_id, ttl_s=ttl_s)


def read_payload(tmp_path: Path) -> dict:
    return json.loads(lock_path(tmp_path).read_text(encoding="utf-8"))


def write_expired_lock(tmp_path: Path, *, owner: str = "old", session_id: str = "ses_old") -> None:
    path = lock_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone()
    payload = {
        "artifact": "design.md",
        "pid": 424242,
        "owner": owner,
        "session_id": session_id,
        "host": "OLD-HOST",
        "acquired_at": (now - timedelta(seconds=300)).isoformat(timespec="seconds"),
        "expire_at": (now - timedelta(seconds=10)).isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_acquire_writes_lock_payload(tmp_path: Path) -> None:
    lock = make_lock(tmp_path)
    lock.acquire()
    try:
        assert lock.acquired is True
        payload = read_payload(tmp_path)
        assert payload["artifact"] == "design.md"
        assert payload["pid"] == os.getpid()
        assert payload["owner"] == "alice"
        assert payload["session_id"] == "ses_a"
        assert payload["host"]
        acquired = datetime.fromisoformat(payload["acquired_at"])
        expire = datetime.fromisoformat(payload["expire_at"])
        assert 118 <= (expire - acquired).total_seconds() <= 122
    finally:
        lock.release()


def test_second_writer_blocked(tmp_path: Path) -> None:
    """AC-22：后写者拿锁失败，LockBusy 语义 = 退出码 3。"""
    first = make_lock(tmp_path)
    first.acquire()
    try:
        second = make_lock(tmp_path, owner="bob", session_id="ses_b")
        with pytest.raises(LockBusy) as excinfo:
            second.acquire()
        assert excinfo.value.exit_code == 3
        message = excinfo.value.render()
        assert "design.md" in message
        assert "owner=alice" in message
        assert "ses_a" in message
        assert "剩余" in message
        assert second.acquired is False
        assert lock_path(tmp_path).is_file(), "失败不得破坏已有锁"
    finally:
        first.release()
    assert not lock_path(tmp_path).exists()


def test_second_process_gets_exit_code_three(tmp_path: Path) -> None:
    """真·第二个进程：LockBusy → 进程退出码 3。"""
    first = make_lock(tmp_path, owner="alice")
    first.acquire()
    script = """
import sys
from pathlib import Path
from specflow.artifacts.lock import FileLock
from specflow.errors import LockBusy

lock = FileLock(Path(sys.argv[1]), "design.md", "bob", "ses_other")
try:
    lock.acquire()
except LockBusy as exc:
    print(exc.render())
    raise SystemExit(exc.exit_code)
raise SystemExit(0)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PLATFORM_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script, str(lock_path(tmp_path))],
            cwd=str(PLATFORM_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
    finally:
        first.release()
    assert proc.returncode == 3, proc.stderr
    assert "剩余" in proc.stdout
    assert "owner=alice" in proc.stdout


def test_context_manager_releases(tmp_path: Path) -> None:
    with make_lock(tmp_path) as lock:
        assert lock.acquired is True
        assert lock_path(tmp_path).is_file()
    assert not lock_path(tmp_path).exists()


def test_release_missing_is_noop(tmp_path: Path) -> None:
    lock = make_lock(tmp_path)
    lock.release()  # 没拿到锁也不报错
    assert lock.acquired is False


def test_expired_lock_can_be_taken_over(tmp_path: Path) -> None:
    write_expired_lock(tmp_path)
    lock = make_lock(tmp_path)
    lock.acquire()
    try:
        assert read_payload(tmp_path)["owner"] == "alice"
        stale = list(lock_path(tmp_path).parent.glob("design.lock.stale.*"))
        assert len(stale) == 1, "过期抢占必须留证"
        stale_payload = json.loads(stale[0].read_text(encoding="utf-8"))
        assert stale_payload["owner"] == "old"
    finally:
        lock.release()


def test_corrupt_lock_is_treated_as_stale(tmp_path: Path) -> None:
    path = lock_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{不是 JSON}", encoding="utf-8")
    lock = make_lock(tmp_path)
    lock.acquire()
    try:
        assert read_payload(tmp_path)["owner"] == "alice"
        assert list(path.parent.glob("design.lock.stale.*"))
    finally:
        lock.release()


def test_renew_refreshes_expire_at(tmp_path: Path) -> None:
    lock = make_lock(tmp_path)
    lock.acquire()
    try:
        before = read_payload(tmp_path)
        lock.renew()
        after = read_payload(tmp_path)
        assert after["owner"] == "alice"
        assert datetime.fromisoformat(after["expire_at"]) >= datetime.fromisoformat(before["expire_at"])
        assert after["pid"] == before["pid"]
    finally:
        lock.release()


def test_renew_missing_lock_raises(tmp_path: Path) -> None:
    lock = make_lock(tmp_path)
    lock.acquire()
    lock.release()
    with pytest.raises(LockBusy):
        lock.renew()