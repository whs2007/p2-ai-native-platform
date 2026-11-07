---
id: <ARTIFACT-ID>
name: archive-record.md
stage: S7
version: 1
status: draft
owner: <OWNER>
summary: <一句话摘要：本次变更 + 回灌规则>
toc: ["1 变更摘要", "2 候选回灌规则", "3 复用条目"]
hash: <HASH>
---

# archive-record.md — <需求标题> 归档记录

> 模板使用说明：替换 `<...>`；候选规则编号 `R-XX`，每条必须写来源与可判定判据，
> 经 `gate approve agents-md` 后才会写入 AGENTS.md。下方「示例」生成真实产物时删除。

## 1 变更摘要

| 项 | 内容 |
|---|---|
| 需求 | <REQ-01..REQ-03> |
| 代码 | `<src/.../module.py>`、`<tests/test_...py>` |
| 验证 | `<qa-report.md verdict>` |
| 返工 | <几轮、原因> |

示例：
- 示例：实现 `import_plans`，TDD 2 轮（首轮缺去重 → 修复），QA 三段 passed。

## 2 候选回灌规则

- R-<XX> <规则正文>（来源：<本次哪个失败/评审意见>；判据：<怎么判定违反>）
- R-<XX> <规则正文>（来源：<...>；判据：<...>）

示例：
- 示例：R-07 导入类需求必须提供重复键用例（来源：TDD 返工 2 次；判据：测试文件缺少 duplicate 用例）。

## 3 复用条目

- <可复用的模板/命令/测试支架，及适用场景>

示例：
- 示例：`templates/test_unit.py` 的 `pytest.raises` 骨架复用于所有导入类任务。

## 附录：结构化数据

```json
{
  "rules_proposed": [
    {
      "id": "R-07",
      "text": "<规则正文>",
      "source": "<来源>",
      "criteria": "<可判定判据>",
      "counterexample": "<反例>"
    }
  ]
}
```
