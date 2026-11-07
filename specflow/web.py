"""`specflow web`：本地 Web 控制台（零第三方依赖，仅标准库）。

功能：
- 展示流水线阶段 / 门控 / 产物 / Trace / 校验问题（只读聚合自 `.spec/`）。
- 执行阶段命令（子进程跑 `specflow run ...`，SSE 实时日志）。
- 人工门控 approve/reject/supersede/revise、初始化、自检。

安全：默认只绑定 127.0.0.1；阶段命令走白名单校验；产物/ Trace 读取做路径穿越防护。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

from specflow import __version__
from specflow.config import Settings
from specflow.errors import SpecflowError

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "web_static"
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


# --------------------------------------------------------------------------- #
# 数据聚合
# --------------------------------------------------------------------------- #
def build_state_payload(settings: Settings, *, check: bool = False) -> dict[str, Any]:
    from specflow.engine.status import collect_status
    from specflow.engine.stages import load_stages

    state_exists = settings.state_path.exists()
    report = collect_status(settings, check=check) if state_exists else _empty_report(settings)
    traces = list_trace_files(settings)
    commands: list[dict[str, Any]] = []
    try:
        for command, stage in load_stages(settings.skills_dir).items():
            commands.append(
                {
                    "command": command,
                    "stage": stage.id,
                    "title": stage.title,
                    "gate": stage.human_gate,
                }
            )
    except SpecflowError:
        commands = []

    counts = {
        "stages": len(report.get("stages") or []),
        "artifacts": len(report.get("artifacts") or []),
        "approved": sum(1 for item in (report.get("artifacts") or []) if item.get("status") == "approved"),
        "problems": len(report.get("problems") or []),
        "traces": len(traces),
    }
    return {
        "specflow_version": __version__,
        "project": str(settings.project_dir),
        "initialized": state_exists,
        "mode": settings.effective_llm_mode,
        "model": settings.llm_model,
        "owner": settings.owner,
        "stage": report.get("stage"),
        "needs_human": report.get("needs_human", False),
        "ok": report.get("ok", False),
        "stages": report.get("stages") or [],
        "gates": report.get("gates") or {},
        "artifacts": report.get("artifacts") or [],
        "problems": report.get("problems") or [],
        "commands": commands,
        "counts": counts,
        "traces": traces,
        "checked": check,
    }


def read_agent_events(settings: Settings, *, limit: int = 500) -> dict[str, Any]:
    """读取 TDD 四角色事件（`.spec/artifacts/tdd-report.jsonl`）。"""
    path = Path(settings.artifacts_dir) / "tdd-report.jsonl"
    events: list[dict[str, Any]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                events.append(item)
    events = events[-limit:]
    roles: dict[str, int] = {}
    for event in events:
        role = str(event.get("role") or "?")
        roles[role] = roles.get(role, 0) + 1
    rounds = max((int(event.get("round") or 0) for event in events), default=0)
    verdict = None
    report = Path(settings.artifacts_dir) / "tdd-report.md"
    if report.is_file():
        match = re.search(r"^verdict:\s*(\w+)", report.read_text(encoding="utf-8", errors="replace"), re.M)
        verdict = match.group(1) if match else None
    return {
        "ok": True,
        "path": ".spec/artifacts/tdd-report.jsonl",
        "events": events,
        "summary": {
            "count": len(events),
            "rounds": rounds,
            "roles": roles,
            "verdict": verdict,
        },
    }


def _empty_report(settings: Settings) -> dict[str, Any]:
    """未初始化时的只读骨架：不触发 StateStore.load（它会写 state.json）。"""
    from specflow.engine.stages import load_stages

    rows: list[dict[str, Any]] = []
    try:
        for index, (command, stage) in enumerate(load_stages(settings.skills_dir).items()):
            rows.append(
                {
                    "stage": stage.id,
                    "command": command,
                    "title": stage.title,
                    "current": index == 0,
                    "reached": False,
                    "produced": [],
                    "gate": (
                        {"name": stage.human_gate, "status": "pending", "by": None, "note": None}
                        if stage.human_gate
                        else None
                    ),
                }
            )
    except SpecflowError:
        rows = []
    return {
        "ok": True,
        "stage": "S0A",
        "owner": settings.owner,
        "needs_human": False,
        "stages": rows,
        "gates": {},
        "artifacts": [],
        "problems": [],
        "checked": False,
    }


def list_trace_files(settings: Settings) -> list[dict[str, Any]]:
    if not settings.traces_dir.is_dir():
        return []
    items = []
    for path in settings.traces_dir.glob("*.jsonl"):
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append({"name": path.name, "size": stat.st_size, "mtime": int(stat.st_mtime)})
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


def safe_trace_name(name: str) -> str:
    candidate = Path(name).name
    if candidate != name or not candidate.endswith(".jsonl"):
        raise ValueError("非法 trace 文件名")
    return candidate


def read_trace(settings: Settings, name: str, *, limit: int = 500) -> dict[str, Any]:
    filename = safe_trace_name(name)
    path = (settings.traces_dir / filename).resolve()
    if settings.traces_dir.resolve() not in path.parents:
        raise ValueError("非法 trace 路径")
    if not path.is_file():
        raise FileNotFoundError(filename)
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"raw": line})
        if len(events) >= limit:
            break
    return {"name": filename, "events": events}


def read_artifact(settings: Settings, name: str) -> dict[str, Any]:
    from specflow.artifacts.store import ArtifactStore

    if "/" in name or "\\" in name or ".." in name:
        raise ValueError("非法产物名")
    store = ArtifactStore(settings)
    try:
        artifact = store.load(name)
    except Exception:  # noqa: BLE001 - 统一转成可读错误
        raise FileNotFoundError(name) from None
    return {
        "name": artifact.name,
        "meta": artifact.meta,
        "body": artifact.body,
        "path": str(artifact.path),
        "hash": artifact.hash,
    }


# --------------------------------------------------------------------------- #
# 阶段命令
# --------------------------------------------------------------------------- #
def build_run_argv(settings: Settings, command_line: str) -> list[str]:
    """校验并构造子进程参数；只允许 9 个阶段命令，不经过 shell。"""
    from specflow.engine.stages import load_stages

    words = shlex.split(command_line.strip())
    if not words:
        raise ValueError("命令为空")
    first = words[0]
    if not first.startswith("/"):
        first = "/" + first
    stages = load_stages(settings.skills_dir)
    if first not in stages:
        raise ValueError(f"未知阶段命令：{first}（可用：{'、'.join(stages)}）")
    words[0] = first
    return [sys.executable, "-m", "specflow", "run", " ".join(words)]


def run_stage_events(settings: Settings, command_line: str) -> Iterator[dict[str, Any]]:
    """执行阶段命令并逐行产出日志事件（供 SSE）。"""
    try:
        argv = build_run_argv(settings, command_line)
    except ValueError as exc:
        yield {"type": "error", "message": str(exc)}
        return
    env = dict(os.environ)
    platform_root = str(settings.platform_root)
    env["PYTHONPATH"] = platform_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONIOENCODING"] = "utf-8"
    yield {"type": "start", "argv": command_line, "cwd": str(settings.project_dir)}
    proc = subprocess.Popen(
        argv,
        cwd=str(settings.project_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            yield {"type": "log", "line": line.rstrip("\n")}
    finally:
        proc.stdout.close()
    code = proc.wait()
    yield {"type": "done", "code": code}


# --------------------------------------------------------------------------- #
# 终端模式（通用 CLI 子命令）
# --------------------------------------------------------------------------- #
_ALLOWED_CLI = {
    "status", "doctor", "init", "run", "stage", "gate", "restore", "unlock",
    "--version", "-h", "--help", "help",
}


def build_cli_argv(settings: Settings, command_line: str) -> list[str]:
    """校验并构造通用 CLI 参数：仅允许 specflow 子命令，不经过 shell。"""
    words = shlex.split(command_line.strip())
    if not words:
        raise ValueError("命令为空")
    if words[0] not in _ALLOWED_CLI:
        raise ValueError(
            "终端模式只支持 specflow 子命令：status / doctor / init / run / gate / restore / unlock"
        )
    if words[0] in {"help", "?"}:
        words[0] = "--help"
    return [sys.executable, "-m", "specflow", *words]


def run_cli_events(settings: Settings, command_line: str) -> Iterator[dict[str, Any]]:
    """执行通用 CLI 命令并逐行产出日志事件（供终端视图）。"""
    try:
        argv = build_cli_argv(settings, command_line)
    except ValueError as exc:
        yield {"type": "error", "message": str(exc)}
        return
    env = dict(os.environ)
    platform_root = str(settings.platform_root)
    env["PYTHONPATH"] = platform_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONIOENCODING"] = "utf-8"
    yield {"type": "start", "argv": command_line, "cwd": str(settings.project_dir)}
    proc = subprocess.Popen(
        argv, cwd=str(settings.project_dir),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            yield {"type": "log", "line": line.rstrip("\n")}
    finally:
        proc.stdout.close()
    yield {"type": "done", "code": proc.wait()}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    settings: Settings
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ---- 基础响应 ----
    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"ok": False, "error": message}, status=status)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/health":
                return self._json({"ok": True, "version": __version__})
            if path == "/api/state":
                check = parse_qs(parsed.query).get("check", ["0"])[0] in {"1", "true", "yes"}
                return self._json(build_state_payload(self.settings, check=check))
            if path == "/api/agents":
                return self._json(read_agent_events(self.settings))
            if path == "/api/doctor":
                from specflow.doctor import run_doctor

                return self._json(run_doctor(self.settings))
            if path == "/api/artifact":
                name = parse_qs(parsed.query).get("name", [""])[0]
                if not name:
                    return self._error("缺少 name")
                return self._json({"ok": True, **read_artifact(self.settings, name)})
            if path == "/api/traces":
                return self._json({"traces": list_trace_files(self.settings)})
            if path == "/api/trace":
                name = parse_qs(parsed.query).get("name", [""])[0]
                return self._json({"ok": True, **read_trace(self.settings, name)})
            if path == "/api/run/stream":
                command_line = parse_qs(parsed.query).get("cmd", [""])[0]
                return self._sse(command_line, run_stage_events)
            if path == "/api/cli/stream":
                command_line = parse_qs(parsed.query).get("cmd", [""])[0]
                return self._sse(command_line, run_cli_events)
            return self._error("Not Found", status=404)
        except (ValueError, FileNotFoundError) as exc:
            return self._error(str(exc), status=400)
        except Exception as exc:  # noqa: BLE001
            log.exception("GET %s failed", path)
            return self._error(f"{type(exc).__name__}: {exc}", status=500)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/gate":
                return self._gate(body)
            if path == "/api/init":
                return self._init()
            if path == "/api/check":
                return self._json(build_state_payload(self.settings, check=True))
            return self._error("Not Found", status=404)
        except (ValueError, json.JSONDecodeError) as exc:
            return self._error(str(exc), status=400)
        except Exception as exc:  # noqa: BLE001
            log.exception("POST %s failed", path)
            return self._error(f"{type(exc).__name__}: {exc}", status=500)

    # ---- 静态文件 ----
    def _static(self, relative: str) -> None:
        target = (STATIC_DIR / relative).resolve()
        if STATIC_DIR not in target.parents and target != STATIC_DIR:
            return self._error("非法路径", status=400)
        if not target.is_file():
            return self._error("Not Found", status=404)
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # ---- 动作 ----
    def _gate(self, body: dict[str, Any]) -> None:
        from specflow.engine.actions import gate_action

        name = str(body.get("name") or "").strip()
        action = str(body.get("action") or "approve").strip()
        note = str(body.get("note") or "").strip() or None
        if not name:
            return self._error("缺少门控名")
        if action not in {"approve", "reject", "supersede", "revise"}:
            return self._error("非法门控动作")
        try:
            result = gate_action(
                self.settings, action=action, name=name, by=self.settings.owner, note=note
            )
        except SpecflowError as exc:
            return self._error(exc.render(), status=400)
        return self._json({"ok": True, "summary": result["summary"], "action": action, "name": name})

    def _init(self) -> None:
        from specflow.cli import cmd_init

        args = argparse.Namespace(target=str(self.settings.project_dir), force=False)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cmd_init(args, self.settings)
        return self._json({"ok": code == 0, "output": buffer.getvalue()})

    # ---- SSE ----
    def _sse(self, command_line: str, runner: Any) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def send(payload: dict[str, Any]) -> None:
            self.wfile.write(
                f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
            )
            self.wfile.flush()

        try:
            for event in runner(self.settings, command_line):
                send(event)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("run failed")
            send({"type": "error", "message": f"{type(exc).__name__}: {exc}"})


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #
def make_server(settings: Settings, host: str = "127.0.0.1", port: int = 8030) -> ThreadingHTTPServer:
    handler = type("SpecflowWebHandler", (Handler,), {"settings": settings})
    return ThreadingHTTPServer((host, port), handler)


def serve(settings: Settings, *, host: str = "127.0.0.1", port: int = 8030, open_browser: bool = False) -> None:
    server = make_server(settings, host, port)
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"[specflow] web 控制台: {url}")
    print(f"[specflow] 目标仓库: {settings.project_dir}（模式 {settings.effective_llm_mode}）")
    if not settings.state_path.exists():
        print("[specflow] 尚未初始化：可点击页面上的“初始化”，或先运行 specflow init")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[specflow] 已停止")
    finally:
        server.server_close()