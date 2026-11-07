---
id: <ARTIFACT-ID>
name: tasks.md
stage: S4
version: 1
status: draft
owner: <OWNER>
feature_id: <FEAT-XXXX>
summary: <一句话摘要：拆成几个任务、依赖顺序>
toc: ["1 任务表", "2 依赖顺序"]
hash: <HASH>
---

# tasks.md — <需求标题> 任务拆解

> 模板使用说明：替换 `<...>`；每个任务必须 `test_first: true`、文件范围明确、
> 预估 ≤4 小时、至少一条验收点。下方「示例」生成真实产物时删除。

## 1 任务表

| 任务 | 标题 | 文件范围 | test_first | 预估(h) | 验收点 |
|---|---|---|---|---|---|
| T1 | <任务标题> | `<src/...>`、`<tests/...>` | true | 1.5 | <怎么判定完成> |
| T2 | <任务标题> | `<src/...>` | true | 2 | <怎么判定完成> |

示例：
- 示例：T1 实现 `import_plans`；文件范围 `src/visit_plan/importer.py`、`tests/test_importer.py`；预估 1.5h。

## 2 依赖顺序

```text
T1 → T2 → T3
```

依赖理由：<为什么必须先做 T1>

## 附录：结构化数据

```json
{
  "tasks": [
    {
      "id": "T1",
      "title": "<任务标题>",
      "files": ["<src/...>", "<tests/...>"],
      "test_first": true,
      "estimate_h": 1.5,
      "acceptance": ["<验收点 1>", "<验收点 2>"]
    }
  ]
}
```
