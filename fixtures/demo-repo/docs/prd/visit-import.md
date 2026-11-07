# 拜访计划导入（visit-import）PRD

> 状态：reviewed ｜ 来源：docs-shuttle `DOC-2041` ｜ 归属：retail-tools

## 1 背景

BD 每日从 POI 导出 CSV 门店清单后，手工录入门店编码、门店名称、计划日期与归属人。
手工录入会带来两类问题：同一门店同一天出现多条计划（计划冲突），以及日期/编码
格式不一致导致下游签到流程取不到计划。目标是提供一次导入、自动去重的能力。

## 2 需求条目

### REQ-01 读取 CSV 生成拜访计划

- 输入：UTF-8 编码 CSV，表头固定为 `store_code,store_name,plan_date,owner`。
- 处理：逐行解析，生成 `visit_plan.models.Plan` 对象。
- 输出：`list[Plan]`，顺序与文件行序一致（去重后）。

**验收标准**
- AC-01.1 给定 5 行合法数据，返回 5 条 `Plan`。
- AC-01.2 `plan_date` 解析为 `datetime.date`（不是字符串、不是 datetime）。

### REQ-02 按门店编码去重

- 同一 `store_code` 只保留首次出现的记录，后续重复行丢弃且不覆盖已有字段。
- 去重是导入的强制行为，不提供开关。

**验收标准**
- AC-02.1 5 行数据含 1 条重复编码时，返回 4 条计划。
- AC-02.2 重复编码保留首次出现的 `store_name` 与 `owner`。

### REQ-03 容错与报错

- 空行（含仅含逗号的行）跳过，不产生 `Plan`。
- 表头缺失或字段顺序不符时抛 `ValueError`，错误信息包含 `store_code`。
- `plan_date` 用 `datetime.date.fromisoformat` 解析；非法日期抛 `ValueError`。

**验收标准**
- AC-03.1 文件中间夹 2 个空行，导入结果条数不受影响。
- AC-03.2 无表头 CSV 抛 `ValueError`，且未写入任何数据。
- AC-03.3 `plan_date=2026-13-01` 抛 `ValueError`。

## 3 示例数据

```csv
store_code,store_name,plan_date,owner
ST-001,望京门店,2026-11-02,lin.xu
ST-002,中关村门店,2026-11-03,chen.yu
ST-001,望京门店(重复),2026-11-05,chen.yu
ST-003,西单门店,2026-11-04,lin.xu

```

预期：4 条计划（ST-001 保留首次的 `望京门店` / `lin.xu`）。

## 4 范围外

- 不处理 xlsx / Excel 二进制格式；不调用 POI 实时接口补全门店名称。
- 不写数据库、不做计划冲突的自动合并（冲突由下游「一个门店一天一条计划」规则兜底）。
- 不提供 UI / 定时任务；不做门店编码的合法性校验（外部 POI 负责）。
- 不实现导入结果的落库与回滚。

## 5 关联文档

- 业务域规则与术语：`docs/domain/visit.md`
- 现有数据模型：`src/visit_plan/models.py`
- 待实现入口：`src/visit_plan/importer.py::import_plans`
