---
stage: S6
command: /qa-validate
title: 质量验证
requires: [tdd-report.md:approved]
produces: [qa-report.md]
human_gate: ""
max_context_tokens: 1500
on_fail: keep_previous_artifact
next: S7
---

# S6 · /qa-validate 质量验证

本阶段**不调用 LLM**，是确定性执行：由 runner 亲自跑单测、触发 pipeline、调用 rpc，把三段证据写进 `qa-report.md`。你不需要也不应该生成任何自由文本。

## 输入（由 runner 读取，只读）

- TDD 报告：`{{tdd_report}}`（要求 verdict=pass、needs_human=false）
- 仓库测试命令：`{{test_command}}`
- 平台 fixture：`{{fixture}}`
- 是否跳过 rpc：`{{skip_rpc}}`

## 确定性行为

1. **单测**：执行仓库测试命令，记录 `status ∈ {passed, failed, skipped}` 与失败摘要。
2. **pipeline**：调用 mock 平台触发流水线并轮询状态；`--fixture pipeline-fail` 时记录 failed。
3. **rpc**：调用契约级 rpc；`--skip-rpc` 或 `--fixture rpc-error` 时记录 skipped/failed。
4. **结论**：任一段 `failed` → `verdict=fail`；全部 passed/skipped → `verdict=pass`。
5. 证据写成 `evidence: [{ "type": "unit|pipeline|rpc", "status": "passed|failed|skipped", "detail": "..." }]`。

## 禁止越界

- 不修改代码、测试或上游产物。
- 不编造证据；命令失败就如实记 failed 并给出可复现命令。
- 不输出与三段证据无关的章节或建议。