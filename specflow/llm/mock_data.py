"""Mock LLM 的确定性响应表（docs/07 §5.7 / §5.8）。

约定：
- `RESPONSES[scenario]` 是「key → 输出 JSON」的映射；key 为 `"default"` 或轮次整数。
  查找顺序：`round` 精确命中 → `"default"` → 唯一元素（见 `mock.py`）。
- 每个 scenario 必须至少有一个 `"default"` 条目；`tests/unit/test_llm.py`
  会用 `specflow.output_schemas.OUTPUT_SCHEMAS` 逐字段校验 `"default"`。
- `tdd-impl` 的 round=1 是**故意有缺陷**的实现（缺去重、空行崩溃），round=2 是修复版；
  这是 AC-15/16/17「首轮失败 → 回流 → pass」演示脚本的数据来源。
- 所有代码都以真实文件内容存放，由 TestWriter / Executor 写出后交给真实 pytest 执行。
  生成的测试不使用 pytest `tmp_path`（改用系统临时目录下手动建目录），
  以便在受限沙箱（无 0o700 目录权限）中也能跑通 `/add` 的完整闭环。
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------- #
# S0B / domain-input
# ---------------------------------------------------------------------- #
_DOMAIN = {
    "domain": "门店拜访计划管理：把区域门店的拜访计划从 CSV 导入、校验、落库，并驱动后续执行与统计。",
    "rules": [
        {
            "id": "R-01",
            "rule": "拜访计划以 store_code 唯一标识门店，同一门店同一日期只保留一条计划。",
            "counterexample": "同一 store_code 在 CSV 里出现两次时，直接插入两条会导致执行端重复打卡。",
        },
        {
            "id": "R-02",
            "rule": "plan_date 必须是 ISO8601 日期字符串（YYYY-MM-DD），解析为 datetime.date。",
            "counterexample": "把 2026/11/01 这种斜杠格式当作合法日期，下游按 ISO 比较时会静默错序。",
        },
        {
            "id": "R-03",
            "rule": "CSV 表头固定为 store_code,store_name,plan_date,owner，缺表头必须报错而不是猜列序。",
            "counterexample": "缺表头时按位置硬解析，会把门店名当编号，污染整库数据。",
        },
        {
            "id": "R-04",
            "rule": "空行与纯分隔行不产生计划记录，也不计入统计口径。",
            "counterexample": "把 ',,' 这样的分隔行解析成空门店计划，导致统计里出现无名门店。",
        },
        {
            "id": "R-05",
            "rule": "导入过程只读源文件，不修改用户原始 CSV；异常时返回明确错误，不写入半截数据。",
            "counterexample": "边读边覆盖原文件，失败后留下被截断的 CSV，用户原始数据不可恢复。",
        },
    ],
    "terms": [
        {
            "term": "拜访计划",
            "meaning": "某门店在某日期由某负责人执行的一次到店拜访安排。",
            "confusable": "易与「拜访记录」混淆：计划是事前安排，记录是事后打卡结果。",
        },
        {
            "term": "store_code",
            "meaning": "门店在业务系统中的唯一编码，导入去重的依据。",
            "confusable": "易与 store_name 混淆：门店名可重复、可改名，不能作为唯一键。",
        },
        {
            "term": "plan_date",
            "meaning": "计划拜访日期，ISO8601 日期，不含时区与时间。",
            "confusable": "易与 created_at 混淆：created_at 是数据写入时间，不是拜访时间。",
        },
        {
            "term": "owner",
            "meaning": "该条拜访计划的责任人账号。",
            "confusable": "易与门店店长混淆：owner 是内部员工，不是门店联系人。",
        },
        {
            "term": "去重",
            "meaning": "同一 store_code 多条记录时只保留首次出现的那条。",
            "confusable": "易与「合并」混淆：去重是丢弃重复项，不合并字段取值。",
        },
        {
            "term": "空行",
            "meaning": "除逗号与空白外没有任何字段值的 CSV 行。",
            "confusable": "易与「字段缺省」混淆：字段缺省仍可能是一条有效记录，空行不是。",
        },
        {
            "term": "表头",
            "meaning": "CSV 第一行的列名定义，决定后续每列语义。",
            "confusable": "易与「首行数据」混淆：没有表头时首行是数据，不能当列名。",
        },
        {
            "term": "回流",
            "meaning": "Verifier 的失败项注入下一轮计划，驱动测试或实现修订。",
            "confusable": "易与「重跑」混淆：回流会改变输入，重跑只是再执行一次。",
        },
    ],
    "boundaries": [
        "本需求只做 CSV 文件导入，不做数据库落库与 HTTP 接口。",
        "不做门店主数据校验（门店是否存在由下游系统负责）。",
        "不处理 Excel、GBK 等非 UTF-8 CSV。",
    ],
}

# ---------------------------------------------------------------------- #
# S1 / prd-input
# ---------------------------------------------------------------------- #
_PRD = {
    "title": "门店拜访计划 CSV 导入（T1）",
    "background": "区域运营每周通过 CSV 下发门店拜访计划；当前靠人工复制，重复与空行导致执行端重复打卡。",
    "requirements": [
        {
            "id": "REQ-01",
            "title": "CSV 导入基础能力",
            "desc": "提供 import_plans(path) 读取 UTF-8 CSV，按固定表头解析为 Plan 列表。",
            "acceptance": [
                "给定合法 CSV，返回与数据行一一对应的 Plan 列表",
                "每行 plan_date 被解析为 datetime.date",
            ],
        },
        {
            "id": "REQ-02",
            "title": "去重与空行容错",
            "desc": "按 store_code 去重并保留首次出现；空行与纯分隔行跳过。",
            "acceptance": [
                "同一 store_code 出现多次时只保留第一次",
                "空行、空白行、纯逗号行不产生记录",
            ],
        },
        {
            "id": "REQ-03",
            "title": "表头缺失显式报错",
            "desc": "CSV 缺少约定表头时抛 ValueError，避免按位置误解析。",
            "acceptance": [
                "空文件抛 ValueError",
                "表头不符时抛 ValueError",
            ],
        },
    ],
    "out_of_scope": [
        "数据库持久化与并发写入",
        "Excel/xlsx 与 GBK 编码",
        "门店主数据校验",
    ],
}

# ---------------------------------------------------------------------- #
# S2 / explore
# ---------------------------------------------------------------------- #
_EXPLORE = {
    "modules": [
        {"path": "src/visit_plan/importer.py", "purpose": "待新增：CSV 导入入口 import_plans(path)。"},
        {"path": "src/visit_plan/models.py", "purpose": "已有 Plan 数据类（store_code/store_name/plan_date/owner）。"},
        {"path": "tests/test_importer.py", "purpose": "待新增：导入行为的 pytest 用例。"},
    ],
    "constraints": [
        "仅标准库（csv/datetime/pathlib），不引入 pandas。",
        "文件读写统一 pathlib + encoding='utf-8'。",
        "Plan 为不可变数据类，字段名与 CSV 表头一致。",
    ],
    "unknowns": [
        {"item": "去重保留首次还是最后一次", "status": "已确认"},
        {"item": "空行是否包含纯逗号分隔行", "status": "已确认"},
        {"item": "是否需要返回 skipped 计数", "status": "无"},
    ],
    "deps": [],
}

# ---------------------------------------------------------------------- #
# S3 / tech-design
# ---------------------------------------------------------------------- #
_DESIGN = {
    "summary": "用标准库 csv.DictReader 实现导入：表头校验前置，逐行过滤空行并去重，日期用 date.fromisoformat。",
    "options": [
        {
            "name": "标准库 csv + 手动循环",
            "pros": "零依赖、行为可控、易测；表头与异常可精确控制。",
            "cons": "需要手写去重与空行判断，代码稍多。",
        },
        {
            "name": "pandas.read_csv",
            "pros": "一行读取，天然处理空行。",
            "cons": "引入重依赖，与「仅标准库」约束冲突，安装成本高。",
        },
    ],
    "changes": [
        {"path": "src/visit_plan/importer.py", "change": "新增 import_plans(path: Path) -> list[Plan]。"},
        {"path": "tests/test_importer.py", "change": "新增基础、去重、空行、缺表头、空文件 5 类用例。"},
    ],
    "interfaces": [
        {
            "name": "import_plans(path) -> list[Plan]",
            "detail": "读 UTF-8 CSV；缺表头抛 ValueError；按 store_code 去重保留首次；跳过空行。",
        }
    ],
    "risks": [
        {
            "id": "RISK-01",
            "desc": "空行解析崩溃（None 上调用 strip）。",
            "mitigation": "逐行判断所有字段 strip 后是否为空，空行直接 continue。",
            "rollback": "回退到上一版 importer.py，测试保留作为回归。",
        },
        {
            "id": "RISK-02",
            "desc": "重复 store_code 造成执行端重复打卡。",
            "mitigation": "seen 集合按 store_code 去重，保留首次出现。",
            "rollback": "关闭导入入口开关，继续使用人工导入流程。",
        },
        {
            "id": "RISK-03",
            "desc": "错误日期格式被静默接受。",
            "mitigation": "date.fromisoformat 解析失败直接抛 ValueError 并带上行号。",
            "rollback": "回退到旧解析逻辑并补充数据校验脚本。",
        },
    ],
}

# ---------------------------------------------------------------------- #
# S4 / plan-task
# ---------------------------------------------------------------------- #
_PLAN_TASK = {
    "feature": {"name": "visit-plan-csv-importer"},
    "tasks": [
        {
            "id": "T1",
            "title": "实现拜访计划 CSV 导入",
            "desc": "实现 import_plans：表头校验、去重、空行跳过、日期解析。",
            "files": ["src/visit_plan/importer.py", "tests/test_importer.py"],
            "test_first": True,
            "estimate_h": 1.5,
            "acceptance": [
                "pytest -q tests/test_importer.py 全绿",
                "重复 store_code 只保留首次出现",
                "空白行与纯分隔行被跳过",
                "缺表头/空文件抛 ValueError",
            ],
        }
    ],
    "order": ["T1"],
}

# ---------------------------------------------------------------------- #
# S7 / archive
# ---------------------------------------------------------------------- #
_ARCHIVE = {
    "summary": "本次把门店拜访计划的 CSV 导入从人工复制改为可测试的 import_plans，覆盖去重与空行容错。",
    "changes": [
        "新增 src/visit_plan/importer.py 的 import_plans 实现",
        "新增 tests/test_importer.py 的 7 条回归用例",
        "修复首轮缺去重与空行崩溃的问题（2 条失败 → 全绿）",
    ],
    "rules_proposed": [
        {
            "id": "R-01",
            "text": "CSV 导入按业务唯一键（store_code）去重并保留首次出现。",
            "source": "REQ-02",
            "criteria": "同一唯一键出现多次时，输出只保留第一条。",
            "counterexample": "不去重会导致下游重复执行，出现重复打卡记录。",
        },
        {
            "id": "R-02",
            "text": "逐行导入前先把所有字段 strip，全空行跳过再解析。",
            "source": "REQ-02",
            "criteria": "空行、空白行、纯分隔行都不产生记录。",
            "counterexample": "对 None 调 strip 会直接抛 AttributeError，导入中断。",
        },
    ],
    "reusable": [
        "CSV 导入的三件套：表头校验、空行跳过、唯一键去重",
        "用 date.fromisoformat 做严格日期解析",
    ],
}

# ---------------------------------------------------------------------- #
# S5 / tdd-plan
# ---------------------------------------------------------------------- #
_TDD_PLAN_R1: dict[str, Any] = {
    "steps": [
        {
            "n": 1,
            "goal": "确认 T1 的任务范围、验收标准与文件白名单",
            "files": ["src/visit_plan/importer.py", "tests/test_importer.py"],
            "verify": "对照 tasks.md 的 files[] 与 acceptance[]，集合一致",
        },
        {
            "n": 2,
            "goal": "先落盘测试：基础导入、去重、空行、缺表头、空文件 5 条用例",
            "files": ["tests/test_importer.py"],
            "verify": "pytest -q tests/test_importer.py 能收集 5 条用例",
        },
        {
            "n": 3,
            "goal": "实现 import_plans：表头校验、逐行解析、Plan 构造、日期解析",
            "files": ["src/visit_plan/importer.py"],
            "verify": "pytest -q tests/test_importer.py，基础用例通过",
        },
        {
            "n": 4,
            "goal": "跑全量测试，收集失败项并判断是否回流",
            "files": ["tests/test_importer.py", "src/visit_plan/importer.py"],
            "verify": "pytest -q tests/test_importer.py 的输出与退出码",
        },
        {
            "n": 5,
            "goal": "给出 pass/回流结论，失败项写入 next round 的上下文",
            "files": ["src/visit_plan/importer.py"],
            "verify": "tdd-report.jsonl 中存在 verifier 事件",
        },
    ]
}

_TDD_PLAN_R2: dict[str, Any] = {
    "steps": [
        {
            "n": 1,
            "goal": "根据 failure_items 定位：去重缺失、空行 None.strip 崩溃",
            "files": ["src/visit_plan/importer.py"],
            "verify": "失败项与代码位置一一对应",
        },
        {
            "n": 2,
            "goal": "补充边界用例：去重保留首次、全空行返回空列表",
            "files": ["tests/test_importer.py"],
            "verify": "pytest -q tests/test_importer.py --collect-only 收集 7 条",
        },
        {
            "n": 3,
            "goal": "修复 import_plans：字段 strip 后全空行跳过，seen 集合按 store_code 去重",
            "files": ["src/visit_plan/importer.py"],
            "verify": "pytest -q tests/test_importer.py 全绿",
        },
        {
            "n": 4,
            "goal": "回归并记录最终结果",
            "files": ["tests/test_importer.py", "src/visit_plan/importer.py"],
            "verify": "pytest -q tests/test_importer.py 退出码 0",
        },
    ]
}

# ---------------------------------------------------------------------- #
# S5 / tdd-tests（真实 pytest 文件内容）
# ---------------------------------------------------------------------- #
_TEST_IMPORTER_R1 = '''\
from __future__ import annotations

import tempfile
import uuid
from datetime import date
from pathlib import Path

import pytest

from visit_plan.importer import import_plans
from visit_plan.models import Plan

HEADER = """store_code,store_name,plan_date,owner
"""
ROWS = """S001,城东店,2026-11-01,lin.xu
S002,城西店,2026-11-02,lin.xu
"""
BLANK_TAIL = """

,,,

"""
_TMP_ROOT = Path(tempfile.gettempdir()) / "visit_plan_importer_tests"


def _write(text: str) -> Path:
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    directory = _TMP_ROOT / uuid.uuid4().hex
    directory.mkdir()
    path = directory / "plans.csv"
    path.write_text(text, encoding="utf-8")
    return path


def test_import_basic_rows():
    plans = import_plans(_write(HEADER + ROWS))
    assert len(plans) == 2
    assert isinstance(plans[0], Plan)
    assert plans[0].store_code == "S001"
    assert plans[0].store_name == "城东店"
    assert plans[0].plan_date == date(2026, 11, 1)
    assert plans[0].owner == "lin.xu"


def test_dedupe_by_store_code():
    plans = import_plans(_write(HEADER + ROWS + "S001,城东新店,2026-11-03,lin.xu\\n"))
    assert [plan.store_code for plan in plans] == ["S001", "S002"]
    assert plans[0].store_name == "城东店"


def test_skip_blank_lines():
    plans = import_plans(_write(HEADER + ROWS + BLANK_TAIL))
    assert len(plans) == 2


def test_missing_header_raises():
    with pytest.raises(ValueError):
        import_plans(_write("""a,b,c
1,2,3
"""))


def test_empty_file_raises():
    with pytest.raises(ValueError):
        import_plans(_write(""))
'''

_TEST_IMPORTER_R2_EXTRA = '''\


def test_dedupe_keeps_first_occurrence():
    plans = import_plans(
        _write(
            HEADER
            + ROWS
            + "S001,城东新店,2026-11-03,other\\n"
            + "S002,城西新店,2026-11-04,other\\n"
        )
    )
    assert len(plans) == 2
    assert plans[0].store_name == "城东店"
    assert plans[1].store_name == "城西店"


def test_blank_rows_only_returns_empty():
    assert import_plans(_write(HEADER + BLANK_TAIL)) == []
'''

_TEST_IMPORTER_R2 = _TEST_IMPORTER_R1 + _TEST_IMPORTER_R2_EXTRA

_TDD_TESTS_R1: dict[str, Any] = {
    "files": [{"path": "tests/test_importer.py", "content": _TEST_IMPORTER_R1}],
    "cases": 5,
}

_TDD_TESTS_R2: dict[str, Any] = {
    "files": [{"path": "tests/test_importer.py", "content": _TEST_IMPORTER_R2}],
    "cases": 7,
}

# ---------------------------------------------------------------------- #
# S5 / tdd-impl（round=1 故意有缺陷：缺去重、空行崩溃；round=2 修复）
# ---------------------------------------------------------------------- #
_IMPL_BUGGY = '''\
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from .models import Plan

FIELDS = ("store_code", "store_name", "plan_date", "owner")


def import_plans(path: Path | str) -> list[Plan]:
    # round=1 首轮实现：能读正常 CSV，但故意缺少去重与空行过滤
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or tuple(reader.fieldnames) != FIELDS:
            raise ValueError("CSV 表头缺失或顺序不符：期望 store_code,store_name,plan_date,owner")
        plans: list[Plan] = []
        for row in reader:
            plans.append(
                Plan(
                    store_code=row["store_code"].strip(),
                    store_name=row["store_name"].strip(),
                    plan_date=date.fromisoformat(row["plan_date"].strip()),
                    owner=row["owner"].strip(),
                )
            )
        return plans
'''

_IMPL_FIXED = '''\
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from .models import Plan

FIELDS = ("store_code", "store_name", "plan_date", "owner")


def import_plans(path: Path | str) -> list[Plan]:
    # round=2 修复：表头校验 → 跳过空行 → 按 store_code 去重（保留首次）
    csv_path = Path(path)
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("CSV 缺少表头：期望 store_code,store_name,plan_date,owner")
        header = [name.strip() for name in reader.fieldnames]
        if tuple(header) != FIELDS:
            raise ValueError("CSV 表头不符合约定：期望 store_code,store_name,plan_date,owner")
        plans: list[Plan] = []
        seen: set[str] = set()
        for row in reader:
            values = [(value or "").strip() for value in row.values()]
            if not any(values):
                continue
            store_code = (row.get("store_code") or "").strip()
            if not store_code or store_code in seen:
                continue
            seen.add(store_code)
            plan_date = (row.get("plan_date") or "").strip()
            try:
                parsed_date = date.fromisoformat(plan_date)
            except ValueError as exc:
                raise ValueError(f"plan_date 不是合法 ISO 日期：{plan_date!r}") from exc
            plans.append(
                Plan(
                    store_code=store_code,
                    store_name=(row.get("store_name") or "").strip(),
                    plan_date=parsed_date,
                    owner=(row.get("owner") or "").strip(),
                )
            )
        return plans
'''

_TDD_IMPL_R1: dict[str, Any] = {
    "files": [{"path": "src/visit_plan/importer.py", "content": _IMPL_BUGGY}],
}

_TDD_IMPL_R2: dict[str, Any] = {
    "files": [{"path": "src/visit_plan/importer.py", "content": _IMPL_FIXED}],
}

# ---------------------------------------------------------------------- #
# 总表：key 为 "default" 或轮次整数（int 键仅用于需要多轮区分的 scenario）
# ---------------------------------------------------------------------- #
RESPONSES: dict[str, dict[Any, dict[str, Any]]] = {
    "domain-input": {"default": _DOMAIN},
    "prd-input": {"default": _PRD},
    "explore": {"default": _EXPLORE},
    "tech-design": {"default": _DESIGN},
    "plan-task": {"default": _PLAN_TASK},
    "archive": {"default": _ARCHIVE},
    "tdd-plan": {"default": _TDD_PLAN_R2, 1: _TDD_PLAN_R1, 2: _TDD_PLAN_R2},
    "tdd-tests": {"default": _TDD_TESTS_R2, 1: _TDD_TESTS_R1, 2: _TDD_TESTS_R2},
    "tdd-impl": {"default": _TDD_IMPL_R2, 1: _TDD_IMPL_R1, 2: _TDD_IMPL_R2},
}

__all__ = [
    "RESPONSES",
    "_TDD_PLAN_R1",
    "_TDD_PLAN_R2",
    "_TDD_TESTS_R1",
    "_TDD_TESTS_R2",
    "_TDD_IMPL_R1",
    "_TDD_IMPL_R2",
    "_TEST_IMPORTER_R1",
    "_TEST_IMPORTER_R2",
    "_IMPL_BUGGY",
    "_IMPL_FIXED",
]