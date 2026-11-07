"""阶段状态机：`.spec/state.json`（docs/07 §3.1 / §5.3）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from specflow.engine.state import STAGE_ORDER, GateState, State, StateStore
from specflow.errors import IllegalTransition


def make_store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / ".spec" / "state.json")


def test_defaults_match_contract() -> None:
    state = State()
    assert state.schema_version == 1
    assert state.stage == "S0A"
    assert state.owner == "specflow-user"
    assert state.artifacts == {}
    assert state.gates == {}
    assert state.rounds == {}
    assert state.gate_failures == {}
    assert state.needs_human is False
    assert state.last_command == ""
    assert state.updated_at == ""
    gate = GateState()
    assert gate.status == "pending"
    assert gate.by is None and gate.note is None and gate.at is None
    assert gate.round == 0


def test_load_missing_creates_default_file(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    state = store.load()
    assert state.stage == "S0A"
    assert store.path.is_file()
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert set(payload) == {
        "schema_version",
        "stage",
        "owner",
        "artifacts",
        "gates",
        "rounds",
        "gate_failures",
        "needs_human",
        "last_command",
        "updated_at",
    }


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    state = State(owner="lin.xu", stage="S3")
    state.record_artifact("design.md", 2, "9f2c1a7b4d05", "approved", "S3")
    state.record_gate("design", "approved", by="lin.xu", note="选型可接受")
    state.rounds["S5"] = 2
    state.gate_failures["plan-task"] = 1
    state.set_needs_human(True)
    state.last_command = "/tech-design"
    store.save(state)

    loaded = make_store(tmp_path).load()
    assert loaded.to_dict() == state.to_dict()
    assert loaded.gates["design"].status == "approved"
    assert loaded.gates["design"].by == "lin.xu"
    assert loaded.artifact_hash("design.md") == "9f2c1a7b4d05"
    assert loaded.artifact_status("design.md") == "approved"
    assert loaded.artifact_hash("missing.md") is None


def test_advance_is_monotonic(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    state = store.load()
    store.advance("S3")
    assert store.state.stage == "S3"
    store.advance("S1")
    assert store.state.stage == "S3"
    store.advance("S7")
    assert store.state.stage == "S7"
    with pytest.raises(IllegalTransition):
        state.advance("S9")


def test_advance_unknown_stage_raises(tmp_path: Path) -> None:
    state = State()
    with pytest.raises(IllegalTransition):
        state.advance("S99")


def test_record_artifact_writes_all_fields(tmp_path: Path) -> None:
    state = State()
    state.record_artifact("tasks.md", 3, "abc123def456", "draft", "S4")
    info = state.artifacts["tasks.md"]
    assert info["version"] == 3
    assert info["hash"] == "abc123def456"
    assert info["status"] == "draft"
    assert info["stage"] == "S4"
    assert info["updated_at"]
    assert state.updated_at


def test_record_gate_preserves_round_and_bumps_after_reject() -> None:
    state = State()
    state.gates["design"] = GateState(status="pending", round=1)
    state.record_gate("design", "rejected", by="reviewer", note="缺压测数据")
    assert state.gates["design"].status == "rejected"
    assert state.gates["design"].note == "缺压测数据"
    assert state.gates["design"].round == 1
    state.record_gate("design", "approved", by="reviewer")
    assert state.gates["design"].status == "approved"
    assert state.gates["design"].round == 2
    with pytest.raises(IllegalTransition):
        state.record_gate("design", "done")


def test_bump_round(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.load()
    assert store.bump_round("S5") == 1
    assert store.bump_round("S5") == 2
    assert store.state.rounds["S5"] == 2
    with pytest.raises(IllegalTransition):
        store.bump_round("S9")


def test_gate_failures_increment_and_clear(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    assert store.record_gate_failure("plan-task") == 1
    assert store.record_gate_failure("plan-task") == 2
    assert store.record_gate_failure("plan-task") == 3
    # 自动落盘：新进程读到同一计数
    assert make_store(tmp_path).load().gate_failures["plan-task"] == 3
    store.clear_gate_failures("plan-task")
    assert "plan-task" not in make_store(tmp_path).load().gate_failures
    store.clear_gate_failures("never-seen")  # 不报错


def test_set_needs_human_persists(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.set_needs_human(True)
    assert make_store(tmp_path).load().needs_human is True
    store.set_needs_human(False)
    assert make_store(tmp_path).load().needs_human is False


def test_store_mutators_persist_without_explicit_save(tmp_path: Path) -> None:
    """hooks 里直接调 StateStore.record_gate_failure 后不会单独 save。"""
    store = make_store(tmp_path)
    store.load()
    store.advance("S2")
    store.record_artifact("prd.md", 1, "aaaabbbbcccc", "approved", "S1")
    store.record_gate("design", "pending", by="tester")
    reloaded = make_store(tmp_path).load()
    assert reloaded.stage == "S2"
    assert reloaded.artifacts["prd.md"]["hash"] == "aaaabbbbcccc"
    assert reloaded.gates["design"].status == "pending"


def test_from_dict_is_tolerant() -> None:
    state = State.from_dict(
        {
            "stage": "S5",
            "gates": {"design": {"status": "approved", "round": "2"}},
            "artifacts": {"design.md": None, "prd.md": {"hash": "x"}},
            "rounds": {"S5": "3", "bad": "x"},
            "gate_failures": {"add": "2"},
            "needs_human": 1,
            "unknown_field": "ignored",
        }
    )
    assert state.stage == "S5"
    assert state.gates["design"].status == "approved"
    assert state.gates["design"].round == 2
    assert state.artifacts == {"prd.md": {"hash": "x"}}
    assert state.rounds == {"S5": 3}
    assert state.gate_failures == {"add": 2}
    assert state.needs_human is True
    assert state.owner == "specflow-user"


def test_from_dict_invalid_stage_falls_back() -> None:
    assert State.from_dict({"stage": "S99"}).stage == "S0A"
    assert State.from_dict(None).stage == "S0A"


def test_save_is_atomic_no_tmp_files(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.save(State(stage="S2", updated_at="2026-10-30T20:00:00+08:00"))
    leftovers = [path.name for path in store.path.parent.iterdir() if path.name.endswith(".tmp")]
    assert leftovers == []


def test_stage_order_constant() -> None:
    assert STAGE_ORDER == ("S0A", "S0B", "S1", "S2", "S3", "S4", "S5", "S6", "S7")