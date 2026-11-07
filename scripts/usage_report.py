"""统计一次 specflow 执行消耗的模型 token。

数据来源：
- `.spec/traces/stage-*.jsonl` 的 llm 事件（阶段级：prompt/completion 可拆）；
- `.spec/artifacts/tdd-report.jsonl` 的令牌事件（TDD 角色：只有总数）。

用法：
    python scripts/usage_report.py
    python scripts/usage_report.py --project-dir .demo/visit-import
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def _bucket() -> dict[str, int]:
    return {"calls": 0, "prompt": 0, "completion": 0, "tokens": 0}


def collect(project_dir: Path) -> dict:
    by_stage: dict[str, dict[str, int]] = defaultdict(_bucket)
    by_scenario: dict[str, dict[str, int]] = defaultdict(_bucket)
    for path in sorted((project_dir / ".spec" / "traces").glob("stage-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("step") != "llm":
                continue
            data = record.get("data") or {}
            usage = data.get("usage") or {}
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
            stage = str(record.get("stage") or "?")
            scenario = str(data.get("scenario") or "?")
            for bucket in (by_stage[stage], by_scenario[scenario]):
                bucket["calls"] += 1
                bucket["prompt"] += prompt
                bucket["completion"] += completion
                bucket["tokens"] += prompt + completion

    tdd_by_role: dict[str, dict[str, int]] = defaultdict(_bucket)
    tdd_path = project_dir / ".spec" / "artifacts" / "tdd-report.jsonl"
    if tdd_path.exists():
        for line in tdd_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = str(record.get("role") or "")
            tokens = int(record.get("tokens") or 0)
            if role in {"planner", "test_writer", "executor"} and tokens > 0:
                bucket = tdd_by_role[role]
                bucket["calls"] += 1
                bucket["tokens"] += tokens

    stage_calls = sum(item["calls"] for item in by_stage.values())
    stage_tokens = sum(item["tokens"] for item in by_stage.values())
    tdd_calls = sum(item["calls"] for item in tdd_by_role.values())
    tdd_tokens = sum(item["tokens"] for item in tdd_by_role.values())
    return {
        "project_dir": str(project_dir),
        "total": {
            "calls": stage_calls + tdd_calls,
            "stage_tokens": stage_tokens,
            "tdd_tokens": tdd_tokens,
            "tokens": stage_tokens + tdd_tokens,
            "prompt": sum(item["prompt"] for item in by_stage.values()),
            "completion": sum(item["completion"] for item in by_stage.values()),
        },
        "by_stage": dict(by_stage),
        "by_scenario": dict(by_scenario),
        "tdd_by_role": dict(tdd_by_role),
    }


def render(report: dict) -> str:
    lines = [f"模型用量报告：{report['project_dir']}", ""]
    total = report["total"]
    if not total["calls"]:
        lines.append("没有找到模型调用记录（可能是 mock 模式，或 trace 中没有 llm 事件）。")
        return "\n".join(lines)
    lines.append(
        f"总计：{total['calls']} 次调用，约 {total['tokens']} tokens"
        f"（阶段 {total['stage_tokens']} + TDD {total['tdd_tokens']}；"
        f"prompt {total['prompt']} / completion {total['completion']} 仅覆盖阶段调用）"
    )
    lines.append("")
    lines.append("| 阶段 | 调用 | prompt | completion |")
    lines.append("|---|---|---|---|")
    for stage, item in sorted(report["by_stage"].items()):
        lines.append(f"| {stage} | {item['calls']} | {item['prompt']} | {item['completion']} |")
    if report["tdd_by_role"]:
        lines.append("")
        lines.append("| TDD 角色 | 调用 | tokens |")
        lines.append("|---|---|---|")
        for role, item in sorted(report["tdd_by_role"].items()):
            lines.append(f"| {role} | {item['calls']} | {item['tokens']} |")
    lines.append("")
    lines.append("| scenario | 调用 | prompt | completion |")
    lines.append("|---|---|---|---|")
    for scenario, item in sorted(report["by_scenario"].items()):
        lines.append(f"| {scenario} | {item['calls']} | {item['prompt']} | {item['completion']} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="统计 specflow 的模型 token 用量")
    parser.add_argument("--project-dir", default=".")
    args = parser.parse_args(argv)
    project_dir = Path(args.project_dir).resolve()
    print(render(collect(project_dir)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())