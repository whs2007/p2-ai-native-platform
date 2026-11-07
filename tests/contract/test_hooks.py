"""Hooks 契约测试：用真子进程执行 `python -m specflow hook <Event>`（docs/07 §5.6）。

覆盖 AC-11（SessionStart 注入与预算）、AC-12（UserPromptSubmit 越级阻断 + 连续失败 exit 3）、
AC-13（PreToolUse deny/ask/allow + 替代方案）、AC-14（PostToolUse 登记文件后索引可见），
以及每个事件都会落 `.spec/traces/hooks-YYYYMMDD.jsonl`（07 §3.3 字段）。

输入样例来自 `tests/fixtures/hooks/*.json`（docs/04 §5 的原始 JSON 示例），
`cwd` 由 `tests/conftest.py` 的 `hook_runner` 注入临时仓库副本，避免读写真实 fixture。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

HOOK_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "hooks"

TRACE_FIELDS = {
    "ts",
    "session_id",
    "event",
    "decision",
    "rule_id",
    "exit_code",
    "elapsed_ms",
    "context_tokens",
    "artifact_refs",
    "error",
}


def load_hook_fixture(name: str) -> dict:
    """读 hook 输入样例；`cwd` 交给 conftest 注入真实临时仓库。"""
    payload = json.loads((HOOK_FIXTURES / name).read_text(encoding="utf-8"))
    payload.pop("cwd", None)
    return payload


def read_traces(repo: Path) -> list[dict]:
    records: list[dict] = []
    for path in sorted((repo / ".spec" / "traces").glob("hooks-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


# ---------------------------------------------------------------------- #
# SessionStart
# ---------------------------------------------------------------------- #
def test_session_start_injects_rules_templates_and_stage(hook_runner, inited_repo: Path) -> None:
    """AC-11：注入 AGENTS.md §2 规则 + 模板索引 + 当前阶段，预算 ≤ session_budget(900)。"""
    proc = hook_runner("SessionStart", load_hook_fixture("session_start.json"))

    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["continue"] is True
    output = data["hookSpecificOutput"]
    assert output["hookEventName"] == "SessionStart"

    context = output["additionalContext"]
    assert "## 基线规则" in context
    assert "R-0" in context            # init 写入的 AGENTS.md §2 规则
    assert "## 模板索引" in context
    assert "## 当前阶段" in context

    meta = data["specflow"]
    assert meta["context_tokens"] <= 900
    assert meta["degraded"] is False
    assert meta["elapsed_ms"] >= 0
    assert "AGENTS.md#2" in meta["sources"]


def test_session_start_trace_recorded(hook_runner, inited_repo: Path) -> None:
    """07 §3.3：每次 hook 决策都要写 trace JSONL，字段齐全。"""
    proc = hook_runner("SessionStart", load_hook_fixture("session_start.json"))
    assert proc.returncode == 0

    records = [item for item in read_traces(inited_repo) if item["event"] == "SessionStart"]
    assert records, "未找到 SessionStart trace"
    record = records[-1]
    assert TRACE_FIELDS <= set(record)
    assert record["decision"] == "allow"
    assert record["exit_code"] == 0
    assert record["session_id"] == load_hook_fixture("session_start.json")["session_id"]
    assert isinstance(record["elapsed_ms"], int)


def test_session_start_truncates_over_budget(hook_runner, inited_repo: Path) -> None:
    """AC-11：注入超 session_budget(900) 时截断并注明。"""
    long_rule = (
        "必须使用 pathlib 与 encoding=\"utf-8\" 处理所有文件读写，"
        "公共函数写 docstring 与类型标注，禁止裸 open 与默认编码"
    )
    rules = "\n".join(f"- R-{index:02d} {long_rule}" for index in range(1, 21))
    (inited_repo / "AGENTS.md").write_text(
        "# AGENTS.md\n\n> 版本：v0.1\n\n## 2. 工程规则（每条都必须可判定）\n"
        f"{rules}\n\n## 3. 流程规则\n- 进入 /plan-task 前 design.md 必须 approved\n",
        encoding="utf-8",
    )

    proc = hook_runner("SessionStart", load_hook_fixture("session_start.json"))
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    context = data["hookSpecificOutput"]["additionalContext"]
    meta = data["specflow"]
    assert meta["truncated"] is True
    assert meta["context_tokens"] <= 900
    assert "已截断" in context


def test_session_start_degrades_when_agents_md_missing(
    hook_runner, inited_repo: Path
) -> None:
    """AC-11 / 02 §4.4：AGENTS.md 缺失 → 最小规则 + 告警，不阻断（exit 0）。"""
    (inited_repo / "AGENTS.md").unlink()

    proc = hook_runner("SessionStart", load_hook_fixture("session_start.json"))
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["continue"] is True
    context = data["hookSpecificOutput"]["additionalContext"]
    assert "基线规则" in context
    assert "## 告警" in context
    assert data["specflow"]["degraded"] is True


# ---------------------------------------------------------------------- #
# UserPromptSubmit
# ---------------------------------------------------------------------- #
def test_user_prompt_submit_gate_failure_blocks_with_exit_2(
    hook_runner, inited_repo: Path
) -> None:
    """AC-12：越级 /plan-task（design.md 未批准）→ continue=false + exit 2 + stderr 单行。"""
    proc = hook_runner("UserPromptSubmit", load_hook_fixture("user_prompt_submit_block.json"))

    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    data = json.loads(proc.stdout)
    assert data["continue"] is False
    assert data["decision"] == "block"
    reason = data["reason"]
    assert "/plan-task" in reason
    assert "design.md" in reason
    assert "未调用模型" in reason

    stderr_lines = [line for line in proc.stderr.splitlines() if line.strip()]
    assert len(stderr_lines) == 1, "强制阻断时 stderr 必须单行（进入模型上下文）"
    assert len(stderr_lines[0]) <= 200


def test_user_prompt_submit_repeated_failures_exit_3(hook_runner, inited_repo: Path) -> None:
    """AC-12 / 07 §7：同一命令连续 3 次门控失败 → exit 3（NeedsHuman，停止自动重试）。"""
    payload = load_hook_fixture("user_prompt_submit_block.json")
    codes = [hook_runner("UserPromptSubmit", payload).returncode for _ in range(3)]
    assert codes == [2, 2, 3], codes

    last = hook_runner("UserPromptSubmit", payload)
    assert last.returncode == 3
    data = json.loads(last.stdout)
    assert data["continue"] is False
    assert data["specflow"]["needs_human"] is True
    assert "停止自动重试" in data["reason"]


def test_user_prompt_submit_unknown_command_allowed(hook_json) -> None:
    """未知 /xxx → allow + 告警（不阻断普通对话）。"""
    data, code = hook_json("UserPromptSubmit", load_hook_fixture("user_prompt_submit_unknown.json"))
    assert code == 0
    assert data["continue"] is True
    context = data["hookSpecificOutput"]["additionalContext"]
    assert "未知命令" in context
    assert "/deploy-now" in context


_VALID_PRD = """# prd.md — 拜访计划导入

## 1 背景
BD 每日按门店清单拜访，需要把 Excel/CSV 计划批量导入。

## 2 需求条目
- REQ-01 提供导入模板并按表头校验
- REQ-02 按 store_code 去重
- REQ-03 失败行给出回执

## 3 验收标准
去重后条数正确；缺表头抛 ValueError；空行跳过。

## 4 范围外
不做批量删除，不改计费字段。

## 附录：结构化数据

```json
{"requirements": [{"id": "REQ-01", "title": "导入模板", "desc": "提供 CSV 模板", "acceptance": ["表头校验通过"]}]}
```
"""


def _write_valid_prd(repo: Path) -> None:
    from specflow.artifacts.store import ArtifactStore
    from specflow.config import Settings

    settings = Settings.load(project_dir=repo, overrides={"LLM_MODE": "mock"})
    ArtifactStore(settings).save(
        "prd.md",
        _VALID_PRD,
        meta={
            "stage": "S1",
            "source": {"type": "file", "ref": "docs/prd/visit-import.md"},
            "summary": "拜访计划批量导入，需去重、失败回执；范围外：不做批量删除",
        },
    )


def test_user_prompt_submit_gate_pass_injects_upstream_summary(
    hook_runner, inited_repo: Path
) -> None:
    """门控通过 → 注入上游摘要（≤ prompt_budget=300 token）并放行。"""
    _write_valid_prd(inited_repo)

    proc = hook_runner("UserPromptSubmit", load_hook_fixture("user_prompt_submit_pass.json"))
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["continue"] is True

    meta = data["specflow"]
    assert meta["gate"] == "pass"
    assert meta["context_tokens"] <= 300
    assert "prd.md" in meta["artifact_refs"]

    context = data["hookSpecificOutput"]["additionalContext"]
    assert "门控通过" in context
    assert "prd.md" in context


def test_user_prompt_submit_without_command_allowed(hook_json) -> None:
    """无命令 → allow + 一行阶段摘要。"""
    data, code = hook_json("UserPromptSubmit", load_hook_fixture("user_prompt_submit_plain.json"))
    assert code == 0
    assert data["continue"] is True
    context = data["hookSpecificOutput"]["additionalContext"]
    assert "阶段" in context
    assert data["specflow"]["stage"]


# ---------------------------------------------------------------------- #
# PreToolUse
# ---------------------------------------------------------------------- #
def test_pre_tool_use_denies_dangerous_command_with_alternative(
    hook_json, inited_repo: Path
) -> None:
    """AC-13：D-02 git reset --hard → deny JSON（exit 0），理由带替代方案。"""
    data, code = hook_json("PreToolUse", load_hook_fixture("pre_tool_use_deny.json"))

    assert code == 0
    output = data["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "deny"
    reason = output["permissionDecisionReason"]
    assert "D-02" in reason
    assert "替代方案" in reason
    assert "git stash" in reason

    meta = data["specflow"]
    assert meta["rule_id"] == "D-02"
    assert meta["severity"] == "block"
    assert meta["elapsed_ms"] >= 0


def test_pre_tool_use_allows_whitelisted_command(hook_json) -> None:
    """白名单短路：pytest -q → allow（exit 0）。"""
    data, code = hook_json("PreToolUse", load_hook_fixture("pre_tool_use_allow.json"))
    assert code == 0
    output = data["hookSpecificOutput"]
    assert output["permissionDecision"] == "allow"
    assert data["specflow"]["rule_id"] == "W-01"


def test_pre_tool_use_asks_for_confirm(hook_json) -> None:
    """A 类：rm -rf ./build（非根）→ ask + 理由（exit 0）。"""
    data, code = hook_json("PreToolUse", load_hook_fixture("pre_tool_use_ask.json"))
    assert code == 0
    output = data["hookSpecificOutput"]
    assert output["permissionDecision"] == "ask"
    assert output["permissionDecisionReason"]
    assert data["specflow"]["severity"] == "confirm"


def test_pre_tool_use_write_scope_denied_with_context(hook_runner, inited_repo: Path) -> None:
    """C-01：executor 越界写 → deny，理由含声明范围（上下文经环境变量注入）。"""
    (inited_repo / ".spec" / "artifacts" / "tasks.md").write_text(
        "---\nid: ART-TASKS-0001\nname: tasks.md\nstage: S4\nversion: 1\n"
        "status: approved\nowner: tester\ncreated_at: 2026-10-30T20:14:03+08:00\n"
        "updated_at: 2026-10-30T20:14:03+08:00\nfeature_id: FEAT-1024\n"
        "---\n\n# tasks.md\n\n## 1 任务表\n\n## 附录：结构化数据\n\n```json\n"
        '{"tasks": [{"id": "T1", "title": "实现 CSV 导入", '
        '"files": ["src/visit_plan/importer.py", "tests/test_importer.py"], '
        '"test_first": true, "estimate_h": 1.5, "acceptance": ["去重"]}]}\n```\n',
        encoding="utf-8",
    )
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(inited_repo / "src" / "visit_plan" / "billing.py")},
    }
    proc = hook_runner(
        "PreToolUse",
        payload,
        extra_env={"SPECFLOW_ROLE": "executor", "SPECFLOW_TASK_ID": "T1"},
    )

    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    output = data["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "billing.py" in output["permissionDecisionReason"]
    assert "T1" in output["permissionDecisionReason"]
    assert "声明" in output["permissionDecisionReason"]
    assert data["specflow"]["rule_id"] == "C-01"
    assert data["specflow"]["task_id"] == "T1"


# ---------------------------------------------------------------------- #
# PostToolUse
# ---------------------------------------------------------------------- #
def test_post_tool_use_registers_file_into_index(hook_runner, inited_repo: Path) -> None:
    """AC-14：Write 后登记文件 → additionalContext + .spec/index/artifacts.json 可见。"""
    target = inited_repo / "src" / "visit_plan" / "importer.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "from pathlib import Path\n\n\ndef import_plans(path: Path) -> list:\n"
        "    return []\n",
        encoding="utf-8",
    )
    payload = load_hook_fixture("post_tool_use_write.json")
    payload["tool_input"]["file_path"] = str(target)

    proc = hook_runner("PostToolUse", payload)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["continue"] is True

    context = data["hookSpecificOutput"]["additionalContext"]
    assert "src/visit_plan/importer.py" in context
    assert data["specflow"]["index_updated"] is True

    index_path = inited_repo / ".spec" / "index" / "artifacts.json"
    assert index_path.exists(), "PostToolUse 应刷新 .spec/index/artifacts.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    entry = index["files"]["src/visit_plan/importer.py"]
    assert entry["hash"]


def test_post_tool_use_registers_artifact_into_index(hook_runner, inited_repo: Path) -> None:
    """.spec/artifacts/*.md 的写入 → register_artifact（索引 artifacts 段可见）。"""
    _write_valid_prd(inited_repo)
    payload = load_hook_fixture("post_tool_use_artifact.json")
    payload["tool_input"] = {
        "file_path": str(inited_repo / ".spec" / "artifacts" / "prd.md"),
        "old_string": "## 4 范围外",
        "new_string": "## 4 范围外\n- 不改计费字段",
    }

    proc = hook_runner("PostToolUse", payload)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["continue"] is True
    assert "prd.md" in data["hookSpecificOutput"]["additionalContext"]
    assert data["specflow"]["tracked"]["artifact"] == "prd.md"

    index = json.loads(
        (inited_repo / ".spec" / "index" / "artifacts.json").read_text(encoding="utf-8")
    )
    assert "prd.md" in index["artifacts"]


def test_post_tool_use_never_blocks_even_without_file(hook_json) -> None:
    """PostToolUse 永不阻断：非写类工具 / 文件不存在都返回 continue=true。"""
    data, code = hook_json("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}})
    assert code == 0
    assert data["continue"] is True
    assert data["specflow"]["index_updated"] is False


# ---------------------------------------------------------------------- #
# 异常降级矩阵（07 §7）
# ---------------------------------------------------------------------- #
def _corrupt_state(repo: Path) -> None:
    (repo / ".spec" / "state.json").write_text("{ not valid json", encoding="utf-8")


def test_hook_exception_defaults_to_fail_open(hook_json, inited_repo: Path) -> None:
    """默认 fail-open：hook 处理异常 → allow/continue + trace 记 error（exit 0）。"""
    _corrupt_state(inited_repo)
    data, code = hook_json(
        "UserPromptSubmit", load_hook_fixture("user_prompt_submit_plain.json")
    )
    assert code == 0
    assert data["continue"] is True
    assert data["specflow"]["error"]

    records = [item for item in read_traces(inited_repo) if item["event"] == "UserPromptSubmit"]
    assert records and records[-1]["error"], "fail-open 也必须在 trace 里记 error"


def test_hook_exception_strict_mode_fail_closed(hook_json, inited_repo: Path) -> None:
    """SPECFLOW_STRICT_HOOKS=1：PreToolUse 异常 → deny JSON（exit 0）。"""
    _corrupt_state(inited_repo)
    data, code = hook_json(
        "PreToolUse",
        load_hook_fixture("pre_tool_use_allow.json"),
        extra_env={"SPECFLOW_STRICT_HOOKS": "1"},
    )
    assert code == 0
    output = data["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "fail-closed" in output["permissionDecisionReason"]
    assert data["specflow"]["strict"] is True


# ---------------------------------------------------------------------- #
# 样例输入自带校验
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "fixture_name", sorted(path.name for path in HOOK_FIXTURES.glob("*.json"))
)
def test_hook_input_fixtures_are_valid(fixture_name: str) -> None:
    payload = json.loads((HOOK_FIXTURES / fixture_name).read_text(encoding="utf-8"))
    assert payload["hook_event_name"] in {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
    }
    assert payload["session_id"]