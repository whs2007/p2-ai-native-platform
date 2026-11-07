"""Web 控制台：数据聚合 / 安全校验 / HTTP 冒烟。"""
from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from specflow.config import Settings
from specflow.web import (
    build_cli_argv,
    build_run_argv,
    build_state_payload,
    make_server,
    read_agent_events,
    read_artifact,
    read_trace,
    run_cli_events,
)


def make_settings(tmp_path) -> Settings:
    return Settings.load(tmp_path, {"LLM_MODE": "mock"})


def test_state_payload_for_empty_project(tmp_path):
    settings = make_settings(tmp_path)
    payload = build_state_payload(settings)
    assert payload["initialized"] is False
    assert len(payload["stages"]) == 9
    assert payload["counts"]["artifacts"] == 0
    assert payload["mode"] == "mock"


def test_agent_events_summary(tmp_path):
    settings = make_settings(tmp_path)
    artifacts = Path(settings.artifacts_dir)
    artifacts.mkdir(parents=True, exist_ok=True)
    events = [
        {"round": 1, "role": "planner", "event": "plan_ready", "steps": 3},
        {"round": 1, "role": "verifier", "event": "fail", "passed": 2, "total": 3},
        {"round": 2, "role": "verifier", "event": "pass", "passed": 3, "total": 3},
    ]
    (artifacts / "tdd-report.jsonl").write_text(
        "\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8"
    )
    (artifacts / "tdd-report.md").write_text("---\nverdict: pass\n---\n", encoding="utf-8")
    payload = read_agent_events(settings)
    assert payload["summary"]["rounds"] == 2
    assert payload["summary"]["count"] == 3
    assert payload["summary"]["roles"] == {"planner": 1, "verifier": 2}
    assert payload["summary"]["verdict"] == "pass"


def test_run_command_allowlist(tmp_path):
    settings = make_settings(tmp_path)
    argv = build_run_argv(settings, "/project-init")
    assert argv[-2:] == ["run", "/project-init"]
    with pytest.raises(ValueError):
        build_run_argv(settings, "/rm -rf /")
    with pytest.raises(ValueError):
        build_run_argv(settings, "")


def test_terminal_cli_allowlist(tmp_path):
    settings = make_settings(tmp_path)
    argv = build_cli_argv(settings, "status --json")
    assert argv[-2:] == ["status", "--json"]
    assert build_cli_argv(settings, "--help")[-1] == "--help"
    with pytest.raises(ValueError):
        build_cli_argv(settings, "rm -rf /")
    events = list(run_cli_events(settings, "rm -rf /"))
    assert events and events[0]["type"] == "error"


def test_artifact_and_trace_path_safety(tmp_path):
    settings = make_settings(tmp_path)
    with pytest.raises(ValueError):
        read_artifact(settings, "../state.json")
    with pytest.raises(ValueError):
        read_trace(settings, "../evil.jsonl")
    with pytest.raises(ValueError):
        read_trace(settings, "notes.txt")


def test_http_server_smoke(tmp_path):
    settings = make_settings(tmp_path)
    server = make_server(settings, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/", timeout=5) as response:
            html = response.read().decode("utf-8")
        assert response.status == 200 and "specflow" in html

        with urllib.request.urlopen(base + "/api/state", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["project"] == str(tmp_path)

        with urllib.request.urlopen(base + "/api/health", timeout=5) as response:
            health = json.loads(response.read().decode("utf-8"))
        assert health["ok"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)