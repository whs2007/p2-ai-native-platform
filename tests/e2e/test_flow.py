"""端到端验收：离线全流程（AC-01/02/03/04/05..10/15..21）。

全程 mock LLM + socket 打桩，证明"零网络依赖"。
"""
from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from specflow.artifacts.store import ArtifactStore
from specflow.cli import main
from specflow.engine.state import StateStore


def run(settings, argv: list[str], expect: int = 0):
    code = main(argv, settings=settings)
    assert code == expect, f"specflow {' '.join(argv)} 退出码 {code}（期望 {expect}）"
    return code


def _patch_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args, **_kwargs):  # pragma: no cover - 只在误联网时触发
        raise AssertionError("离线模式下发生了网络调用")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)


def test_offline_full_flow(settings, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_socket(monkeypatch)
    store = ArtifactStore(settings)
    state = StateStore(settings.state_path)

    # ---------------- AC-01 初始化 ----------------
    run(settings, ["init"])
    for name in ("artifacts", "traces", "locks", "backup", "index"):
        assert (settings.spec_dir / name).is_dir()
    assert (repo / "hooks.json").exists()
    assert (repo / "AGENTS.md").exists()
    assert state.load().stage == "S0A"

    # ---------------- AC-02 / AC-03 Day 0 ----------------
    run(settings, ["run", "/project-init"])
    profile = store.load("repo-profile.md")
    appendix = profile.appendix() or {}
    assert appendix["language"] == "Python"
    assert appendix["test_command"].startswith("python -m pytest")
    assert appendix["entry_points"]
    assert len(appendix["commands"]) >= 3
    templates = [item for item in (repo / "templates").iterdir() if item.is_file()]
    assert len(templates) >= 3
    assert any("<" in item.read_text(encoding="utf-8") for item in templates)
    assert "## 2. 工程规则" in (repo / "AGENTS.md").read_text(encoding="utf-8")

    # ---------------- AC-04 领域产物 ----------------
    run(settings, ["run", "/domain-input"])
    domain_text = (repo / "domain.md").read_text(encoding="utf-8")
    assert "术语表" in domain_text and "核心规则" in domain_text
    assert (repo / "index.md").exists()

    # ---------------- S1 PRD ----------------
    run(settings, ["run", "/prd-input", "docs/prd/visit-import.md"])
    prd = store.load("prd.md")
    requirements = (prd.appendix() or {})["requirements"]
    assert requirements and requirements[0]["id"].startswith("REQ-")

    # ---------------- AC-05 / AC-06 / AC-09 门控 ----------------
    run(settings, ["run", "/plan-task"], expect=2)        # design 未生成
    run(settings, ["run", "/plan-task"], expect=2)
    run(settings, ["run", "/plan-task"], expect=3)        # 连续 3 次 → NeedsHuman
    assert state.load().gate_failures["plan-task"] >= 3

    # ---------------- S2 / S3 ----------------
    run(settings, ["run", "/explore"])
    exploration = store.load("exploration.md")
    assert exploration.appendix()["unknowns"]
    run(settings, ["run", "/tech-design"])
    design = store.load("design.md")
    assert design.status == "draft"
    assert len((design.appendix() or {})["risks"]) >= 3

    # ---------------- AC-07 批准后放行 ----------------
    run(settings, ["gate", "approve", "design", "--as", "tester", "--note", "选型可接受"])
    assert store.load("design.md").status == "approved"
    assert state.load().gates["design"].status == "approved"

    # ---------------- S4 ----------------
    run(settings, ["run", "/plan-task"])
    tasks = store.load("tasks.md")
    task_items = (tasks.appendix() or {})["tasks"]
    assert task_items[0]["id"] == "T1" and task_items[0]["test_first"] is True
    assert tasks.meta["feature_id"].startswith("FEAT-")

    # ---------------- AC-15 / AC-16 TDD ----------------
    run(settings, ["run", "/add", "--task", "T1"])
    report = store.load("tdd-report.md")
    assert report.appendix()["verdict"] == "pass"
    assert report.appendix()["rounds"] == 2
    assert report.status == "approved"
    events = [
        json.loads(line)
        for line in (settings.artifacts_dir / "tdd-report.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    written = next(item["ts"] for item in events if item.get("event") in {"test_written", "tests_written"})
    implemented = next(item["ts"] for item in events if item.get("event") == "impl_written")
    assert written <= implemented, "测试必须先于实现落盘"
    assert (repo / "src" / "visit_plan" / "importer.py").exists()

    # ---------------- AC-19 QA 三段证据 ----------------
    run(settings, ["run", "/qa-validate"])
    qa = store.load("qa-report.md")
    evidence = (qa.appendix() or {})["evidence"]
    assert {item["type"] for item in evidence} == {"unit", "pipeline", "rpc"}
    assert (qa.appendix() or {})["verdict"] == "pass"
    assert qa.status == "approved"

    # ---------------- S7 + AC-20 回灌 ----------------
    run(settings, ["run", "/archive"])
    archive = store.load("archive-record.md")
    proposals = (archive.appendix() or {})["rules_proposed"]
    assert proposals, "归档应产出候选规则"
    agents_before = (repo / "AGENTS.md").read_text(encoding="utf-8")
    run(settings, ["gate", "approve", "agents-md", "--as", "tester", "--note", "规则可接受"])
    agents_after = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert agents_after != agents_before
    assert proposals[0]["id"] in agents_after
    assert "变更记录" in agents_after

    # ---------------- AC-10 篡改检测 ----------------
    tamper = store.load("design.md")
    store.save(
        "design.md",
        tamper.body + "\n\n> 手工追加：模型顺手优化\n",
        meta=dict(tamper.meta),
        status="approved",
        stage="S3",
    )
    run(settings, ["run", "/plan-task"], expect=2)

    # ---------------- 状态/自检 ----------------
    run(settings, ["status", "--check"], expect=2)  # 篡改后 --check 必须报问题
    run(settings, ["doctor"])