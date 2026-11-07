---
id: <ARTIFACT-ID>
name: qa-report.md
stage: S6
version: 1
status: draft
owner: <OWNER>
verdict: <pass|fail>
summary: <一句话摘要：三段证据结论>
toc: ["1 单测", "2 pipeline", "3 rpc", "4 结论"]
hash: <HASH>
---

# qa-report.md — <需求标题> 验证报告

> 模板使用说明：替换 `<...>`；三段证据（单测 / pipeline / rpc）缺一不可，
> 任一段 failed 则 `verdict=fail`。下方「示例」生成真实产物时删除。

## 1 单测

- 命令：`<python -m pytest -q>`
- 结果：<passed 数>/<total 数>，耗时 <N>s
- 失败项：<无 | 用例名 + 错误摘要>

示例：
- 示例：`python -m pytest -q` → 5 passed / 5 total；无失败。

## 2 pipeline

- 触发：`pipeline_trigger(<branch>)` → `run_id=<RUN-XXXX>`
- 状态：`<passed|failed>`，耗时 `<duration_s>`s
- 链接：`<pipeline url>`

示例：
- 示例：`pipeline_trigger(feature/visit-import)` → RUN-2031，状态 passed。

## 3 rpc

- 方法：`<visit.import.validate>`
- 结果：`ok=<true|false>`，`result=<...>` / `error.code=<...>`

示例：
- 示例：`rpc_call("visit.import.validate", {"cases": 5})` → ok=true，cases=5。

## 4 结论

- 判定：`<pass|fail>`
- 阻塞项 / 已知问题：<无 | 描述>
- 复现命令：`<完整命令>`

## 附录：结构化数据

```json
{
  "verdict": "pass",
  "evidence": [
    {"type": "unit", "status": "passed", "detail": "<pytest 结果摘要>"},
    {"type": "pipeline", "status": "passed", "detail": "<run_id + 耗时>"},
    {"type": "rpc", "status": "passed", "detail": "<method + result>"}
  ]
}
```
