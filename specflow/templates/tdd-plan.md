---
id: <ARTIFACT-ID>
name: tdd-plan.md
stage: S5
version: 1
status: draft
owner: <OWNER>
task_id: <T1>
round: <1>
summary: <一句话摘要：几步、先测后实现>
toc: ["1 步骤", "2 验证方式"]
hash: <HASH>
---

# tdd-plan.md — <任务标题> TDD 计划

> 模板使用说明：替换 `<...>`；步骤 ≤8 步，每步都要写目标文件与验证方式，
> 且第一步必须是「写失败测试」。下方「示例」生成真实产物时删除。

## 1 步骤

| 步 | 目标 | 目标文件 | 验证方式 |
|---|---|---|---|
| 1 | <写出失败测试> | `<tests/test_...py>` | `pytest -q` 应失败且失败原因符合预期 |
| 2 | <实现最小逻辑> | `<src/.../module.py>` | `pytest -q` 应转绿 |
| 3 | <补边界用例> | `<tests/test_...py>` | `pytest -q` 全绿 |

示例：
- 示例：步骤 1 写 `test_import_plans_skips_duplicate_store`（红）；步骤 2 实现去重（绿）。

## 2 验证方式

- 单测：`python -m pytest -q <测试文件>`
- 范围检查：只改 `<src/.../module.py>` 与本任务测试文件
- 完成判据：<全部用例通过 + 无新增 lint 错误>

## 附录：结构化数据

```json
{
  "steps": [
    {"n": 1, "goal": "<写失败测试>", "files": ["<tests/test_...py>"], "verify": "<pytest 失败原因符合预期>"},
    {"n": 2, "goal": "<实现>", "files": ["<src/.../module.py>"], "verify": "<pytest 全绿>"}
  ]
}
```
