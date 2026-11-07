"""索引：artifacts.json + index.md 三节（docs/07 §3.2 / §4.4、AC-18）。"""
from __future__ import annotations

import json
from pathlib import Path

from specflow.artifacts.indexer import Indexer
from specflow.artifacts.store import ArtifactStore
from specflow.config import Settings
from specflow.engine.state import State

DESIGN_BODY = """# 技术方案

## 1 选型对比

用标准库 csv。

## 2 风险与回滚

回滚到上一版。
"""

ARCHIVE_BODY = """# 归档记录

## 1 变更摘要

完成 CSV 导入。

## 2 候选回灌规则

见附录。

## 3 复用条目

- CSV 三件套

## 附录：结构化数据

```json
{
  "rules_proposed": [
    {"id": "R-07", "text": "CSV 导入按 store_code 去重", "source": "REQ-02",
     "criteria": "同键只留首次", "counterexample": "重复打卡"},
    {"id": "R-08", "text": "逐行 strip 后跳过空行", "source": "REQ-02",
     "criteria": "空行不产生记录", "counterexample": "None.strip 崩溃"}
  ],
  "reusable": ["CSV 三件套"]
}
```
"""


def make_env(tmp_path: Path) -> tuple[Settings, ArtifactStore, Indexer]:
    settings = Settings.load(
        project_dir=tmp_path,
        overrides={"LLM_MODE": "mock", "PLATFORM_MODE": "mock", "SPECFLOW_OWNER": "tester"},
    )
    return settings, ArtifactStore(settings), Indexer(settings)


def test_register_artifact_writes_entry(tmp_path: Path) -> None:
    _settings, store, indexer = make_env(tmp_path)
    artifact = store.save("design.md", DESIGN_BODY, stage="S3", status="approved")

    indexer.register_artifact(artifact)

    data = indexer.load()
    assert data["schema_version"] == 1
    assert data["generated_at"]
    entry = data["artifacts"]["design.md"]
    assert entry["path"] == ".spec/artifacts/design.md"
    assert entry["stage"] == "S3"
    assert entry["status"] == "approved"
    assert entry["version"] == artifact.version
    assert entry["hash"] == artifact.hash
    assert entry["mtime"]

    raw = json.loads((tmp_path / ".spec" / "index" / "artifacts.json").read_text(encoding="utf-8"))
    assert raw["artifacts"]["design.md"]["hash"] == artifact.hash
    # 重新实例化仍可读到（落盘持久化）
    assert Indexer(_settings).load()["artifacts"]["design.md"]["version"] == artifact.version


def test_register_file_relative_and_absolute(tmp_path: Path) -> None:
    """AC-18：写文件后索引登记 task_id / hash。"""
    _settings, _store, indexer = make_env(tmp_path)
    target = tmp_path / "src" / "visit_plan" / "importer.py"
    target.parent.mkdir(parents=True)
    target.write_text("def import_plans(path):\n    return []\n", encoding="utf-8")

    indexer.register_file(Path("src/visit_plan/importer.py"), task_id="T1", stage="S5", source="executor")

    entry = indexer.get_file("src/visit_plan/importer.py")
    assert entry is not None
    assert entry["task_id"] == "T1"
    assert entry["stage"] == "S5"
    assert entry["source"] == "executor"
    assert len(entry["hash"]) == 12
    assert entry["mtime"]

    # 绝对路径写同一文件 → 同一 key（不重复）
    indexer.register_file(target, task_id="T1", stage="S5")
    assert list(indexer.load()["files"]) == ["src/visit_plan/importer.py"]
    # 反斜杠路径也能查到；未知路径返回 None
    assert indexer.get_file("src\\visit_plan\\importer.py") is not None
    assert indexer.get_file("src/visit_plan/missing.py") is None


def test_register_file_missing_file_has_empty_hash(tmp_path: Path) -> None:
    _settings, _store, indexer = make_env(tmp_path)
    indexer.register_file(Path("src/not/here.py"), task_id=None, stage="S5")
    entry = indexer.get_file("src/not/here.py")
    assert entry is not None
    assert entry["hash"] == ""
    assert entry["task_id"] is None


def test_rebuild_markdown_three_sections(tmp_path: Path) -> None:
    _settings, store, indexer = make_env(tmp_path)
    design = store.save(
        "design.md",
        DESIGN_BODY,
        stage="S3",
        status="approved",
        meta={"gate": "design", "summary": "用标准库 csv 实现导入"},
    )
    indexer.register_artifact(design)
    archive = store.save(
        "archive-record.md",
        ARCHIVE_BODY,
        stage="S7",
        meta={"summary": "完成 CSV 导入"},
    )
    indexer.register_artifact(archive)
    target = tmp_path / "src" / "visit_plan" / "importer.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n", encoding="utf-8")
    indexer.register_file(Path("src/visit_plan/importer.py"), task_id="T1", stage="S5")

    path = indexer.rebuild_markdown(store, State(stage="S7"))

    assert path == tmp_path / "index.md"
    text = path.read_text(encoding="utf-8")
    assert "# 项目知识索引" in text
    assert "阶段：S7" in text
    assert "## 1 阶段产物" in text
    assert "design.md" in text and "用标准库 csv 实现导入" in text
    assert "## 2 交付代码" in text
    assert "src/visit_plan/importer.py" in text and "T1" in text
    assert "## 3 最近需求" in text
    assert "R-07" in text and "R-08" in text
    assert "CSV 三件套" in text


def test_rebuild_markdown_empty_repo(tmp_path: Path) -> None:
    _settings, store, indexer = make_env(tmp_path)
    path = indexer.rebuild_markdown(store, State())
    text = path.read_text(encoding="utf-8")
    assert "## 1 阶段产物" in text and "尚无产物" in text
    assert "## 2 交付代码" in text and "尚未登记交付文件" in text
    assert "## 3 最近需求" in text and "尚无归档记录" in text


def test_get_file_before_any_registration(tmp_path: Path) -> None:
    _settings, _store, indexer = make_env(tmp_path)
    assert indexer.get_file("src/a.py") is None
    assert indexer.load()["files"] == {}
    assert indexer.load()["artifacts"] == {}