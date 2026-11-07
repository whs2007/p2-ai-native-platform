"""`.spec/state.json`：阶段状态机（docs/07 §3.1 / §5.3）。

字段与 07 §3.1 完全一致；所有写入走 `util.dump_json`（tmp + os.replace 原子替换）。
`StateStore` 的变更类方法会顺手落盘（hooks 里直接调用后不再单独 save 也能持久化）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from specflow.errors import IllegalTransition
from specflow.util import dump_json, load_json, now_iso

#: 阶段顺序（S0A < S0B < S1 < ... < S7），用于 `advance` 的「只进不退」语义
STAGE_ORDER: tuple[str, ...] = ("S0A", "S0B", "S1", "S2", "S3", "S4", "S5", "S6", "S7")
_STAGE_INDEX: dict[str, int] = {stage: index for index, stage in enumerate(STAGE_ORDER)}

GATE_STATUSES: tuple[str, ...] = ("pending", "approved", "rejected", "superseded")


@dataclass
class GateState:
    """人门状态：`gates.<name>`（design / agents-md / tdd / qa ...）。"""

    status: str = "pending"
    by: str | None = None
    note: str | None = None
    at: str | None = None
    round: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "by": self.by,
            "note": self.note,
            "at": self.at,
            "round": self.round,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "GateState":
        data = dict(data or {})
        try:
            round_no = int(data.get("round") or 0)
        except (TypeError, ValueError):
            round_no = 0
        return cls(
            status=str(data.get("status") or "pending"),
            by=data.get("by"),
            note=data.get("note"),
            at=data.get("at"),
            round=round_no,
        )


@dataclass
class State:
    """阶段 / 产物 / 门控 / 轮次的持久化状态。"""

    schema_version: int = 1
    stage: str = "S0A"
    owner: str = "specflow-user"
    artifacts: dict[str, dict] = field(default_factory=dict)
    gates: dict[str, GateState] = field(default_factory=dict)
    rounds: dict[str, int] = field(default_factory=dict)
    gate_failures: dict[str, int] = field(default_factory=dict)
    needs_human: bool = False
    last_command: str = ""
    updated_at: str = ""

    # ------------------------------------------------------------------ #
    # 序列化
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": int(self.schema_version),
            "stage": self.stage,
            "owner": self.owner,
            "artifacts": {name: dict(info or {}) for name, info in (self.artifacts or {}).items()},
            "gates": {name: gate.to_dict() for name, gate in (self.gates or {}).items()},
            "rounds": {name: int(value) for name, value in (self.rounds or {}).items()},
            "gate_failures": {name: int(value) for name, value in (self.gate_failures or {}).items()},
            "needs_human": bool(self.needs_human),
            "last_command": self.last_command,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "State":
        data = dict(data or {})
        gates_raw = data.get("gates") if isinstance(data.get("gates"), Mapping) else {}
        if gates_raw and all(isinstance(value, GateState) for value in gates_raw.values()):
            gates = {str(name): value for name, value in gates_raw.items()}
        else:
            gates = {str(name): GateState.from_dict(value) for name, value in gates_raw.items()}
        artifacts_raw = data.get("artifacts") if isinstance(data.get("artifacts"), Mapping) else {}
        artifacts = {
            str(name): dict(info or {}) for name, info in artifacts_raw.items() if isinstance(info, Mapping)
        }
        rounds = _int_map(data.get("rounds"))
        gate_failures = _int_map(data.get("gate_failures"))
        stage = str(data.get("stage") or "S0A")
        if stage not in _STAGE_INDEX:
            stage = "S0A"
        try:
            schema_version = int(data.get("schema_version") or 1)
        except (TypeError, ValueError):
            schema_version = 1
        return cls(
            schema_version=schema_version,
            stage=stage,
            owner=str(data.get("owner") or "specflow-user"),
            artifacts=artifacts,
            gates=gates,
            rounds=rounds,
            gate_failures=gate_failures,
            needs_human=bool(data.get("needs_human") or False),
            last_command=str(data.get("last_command") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )

    # ------------------------------------------------------------------ #
    # 只读查询
    # ------------------------------------------------------------------ #
    def artifact_hash(self, name: str) -> str | None:
        info = (self.artifacts or {}).get(name) or {}
        value = info.get("hash")
        return str(value) if value else None

    def artifact_status(self, name: str) -> str | None:
        info = (self.artifacts or {}).get(name) or {}
        value = info.get("status")
        return str(value) if value else None

    # ------------------------------------------------------------------ #
    # 变更（返回 self，便于链式调用；updated_at 自动刷新）
    # ------------------------------------------------------------------ #
    def advance(self, stage_id: str) -> "State":
        """`stage = max(当前, stage_id)`：只进不退。"""
        if stage_id not in _STAGE_INDEX:
            raise IllegalTransition(f"未知阶段：{stage_id!r}（可选：{'/'.join(STAGE_ORDER)}）")
        if _STAGE_INDEX[stage_id] > _STAGE_INDEX.get(self.stage, 0):
            self.stage = stage_id
        self.updated_at = now_iso()
        return self

    def record_artifact(
        self,
        name: str,
        version: int,
        hash: str,
        status: str,
        stage: str | None = None,
    ) -> "State":
        self.artifacts[name] = {
            "version": int(version),
            "hash": str(hash),
            "status": str(status),
            "stage": str(stage or self.stage),
            "updated_at": now_iso(),
        }
        self.updated_at = now_iso()
        return self

    def record_gate(
        self,
        name: str,
        status: str,
        *,
        by: str | None = None,
        note: str | None = None,
    ) -> "State":
        if status not in GATE_STATUSES:
            raise IllegalTransition(
                f"非法的人门状态：{status!r}（可选：{'/'.join(GATE_STATUSES)}）"
            )
        previous = (self.gates or {}).get(name)
        round_no = previous.round if isinstance(previous, GateState) else 0
        # 打回后重新批准 → 审批轮次 +1（07 §3.1 的 gate.round 语义）
        if previous is not None and previous.status == "rejected" and status == "approved":
            round_no += 1
        self.gates[name] = GateState(
            status=status,
            by=by or (previous.by if previous is not None else None),
            note=note if note is not None else (previous.note if previous is not None else None),
            at=now_iso(),
            round=round_no,
        )
        self.updated_at = now_iso()
        return self

    def bump_round(self, stage_id: str) -> int:
        if stage_id not in _STAGE_INDEX:
            raise IllegalTransition(f"未知阶段：{stage_id!r}")
        current = int((self.rounds or {}).get(stage_id) or 0) + 1
        self.rounds[stage_id] = current
        self.updated_at = now_iso()
        return current

    def record_gate_failure(self, slug: str) -> int:
        current = int((self.gate_failures or {}).get(slug) or 0) + 1
        self.gate_failures[slug] = current
        self.updated_at = now_iso()
        return current

    def clear_gate_failures(self, slug: str) -> None:
        if self.gate_failures and slug in self.gate_failures:
            self.gate_failures.pop(slug, None)
        self.updated_at = now_iso()

    def set_needs_human(self, flag: bool) -> "State":
        self.needs_human = bool(flag)
        self.updated_at = now_iso()
        return self


def _int_map(raw: Any) -> dict[str, int]:
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, int] = {}
    for key, value in raw.items():
        try:
            result[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return result


class StateStore:
    """`.spec/state.json` 的读写入口（原子写）。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._state: State | None = None

    # ------------------------------------------------------------------ #
    # 读写
    # ------------------------------------------------------------------ #
    def load(self) -> State:
        if self.path.is_file():
            data = load_json(self.path, None)
            state = State.from_dict(data if isinstance(data, Mapping) else {})
        else:
            state = State(owner="specflow-user", updated_at=now_iso())
            dump_json(self.path, state.to_dict())
        self._state = state
        return state

    def save(self, state: State) -> None:
        if not isinstance(state, State):
            raise IllegalTransition("StateStore.save 只接受 State 实例")
        if state.updated_at == "":
            state.updated_at = now_iso()
        dump_json(self.path, state.to_dict())
        self._state = state

    @property
    def state(self) -> State:
        if self._state is None:
            return self.load()
        return self._state

    # ------------------------------------------------------------------ #
    # 便捷变更（自动落盘）
    # ------------------------------------------------------------------ #
    def advance(self, stage_id: str) -> State:
        return self._mutate(lambda state: state.advance(stage_id))

    def record_artifact(
        self,
        name: str,
        version: int,
        hash: str,
        status: str,
        stage: str | None = None,
    ) -> State:
        return self._mutate(lambda state: state.record_artifact(name, version, hash, status, stage))

    def record_gate(
        self,
        name: str,
        status: str,
        *,
        by: str | None = None,
        note: str | None = None,
    ) -> State:
        return self._mutate(lambda state: state.record_gate(name, status, by=by, note=note))

    def bump_round(self, stage_id: str) -> int:
        state = self.state
        value = state.bump_round(stage_id)
        self.save(state)
        return value

    def record_gate_failure(self, slug: str) -> int:
        state = self.state
        value = state.record_gate_failure(slug)
        self.save(state)
        return value

    def clear_gate_failures(self, slug: str) -> None:
        state = self.state
        state.clear_gate_failures(slug)
        self.save(state)

    def set_needs_human(self, flag: bool) -> State:
        return self._mutate(lambda state: state.set_needs_human(flag))

    def _mutate(self, callback) -> State:
        state = self.state
        callback(state)
        self.save(state)
        return state