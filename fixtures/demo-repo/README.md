# demo-repo — 拜访计划导入（离线演示仓库）

`specflow` 的离线演示仓库：一个最小 Python 项目，用来演示
`/project-init → /domain-input → /prd-input → /explore → /tech-design → /plan-task → /add → /qa-validate → /archive`
全链路。仓库本体**只读**，演练时复制到 `.demo/<repo>` 或临时目录再跑。

## 目录结构

```text
demo-repo/
├─ pyproject.toml          # pytest 配置（无第三方依赖）
├─ ruff.toml               # 代码规范
├─ src/visit_plan/         # 业务代码（src 布局）
│  ├─ __init__.py
│  ├─ __main__.py          # 命令行入口：python -m visit_plan
│  ├─ models.py            # Plan 数据模型
│  └─ importer.py          # CSV 导入器（待 /add 的 TDD 流程实现）
├─ tests/test_models.py    # 既有测试风格样板
└─ docs/
   ├─ prd/visit-import.md  # 本需求 PRD（REQ-01..REQ-03）
   └─ domain/visit.md      # 拜访域知识（规则 + 术语）
```

## 命令基线

| 用途 | 命令 |
|---|---|
| 测试 | `python -m pytest -q`（或 `pytest -q`） |
| Lint | `ruff check src tests` |
| 运行 | `PYTHONPATH=src python -m visit_plan --help` |
| 导入演示 | `PYTHONPATH=src python -m visit_plan data/plans.csv` |

> Windows PowerShell 下设置 `PYTHONPATH`：`$env:PYTHONPATH = "src"`。

## 约定

- 只用 Python 标准库，不引入新依赖（`.env`、`requirements.txt` 均无）。
- 文件读写统一 `pathlib.Path` + `encoding="utf-8"`。
- 测试命名 `test_<行为>_<条件>_<期望>`；先写失败测试，再写实现。
- 业务规则见 `docs/domain/visit.md`，不要把术语含义猜进代码。

## 当前状态

`src/visit_plan/importer.py::import_plans` 仍是 `NotImplementedError`，
由 `/add --task T1` 的多角色 TDD 流程实现；`tests/test_models.py` 是既有的
测试风格样板，应当始终通过。
