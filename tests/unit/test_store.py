"""ArtifactStore：版本 / 备份 / 回滚 / 原子写 / hash 一致性（docs/07 §4.2、AC-10）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from specflow.artifacts.store import ArtifactStore
from specflow.config import Settings
from specflow.errors import ArtifactError, ArtifactNotFound
from specflow.util import sha256_12

BODY = """# 技术方案 — demo

## 1 选型对比

用标准库 csv。

## 2 模块改动

改 importer。

## 3 风险与回滚

回滚到上一版。
"""


def make_store(tmp_path: Path) -> tuple[Settings, ArtifactStore]:
    settings = Settings.load(
        project_dir=tmp_path,
        overrides={"LLM_MODE": "mock", "PLATFORM_MODE": "mock", "SPECFLOW_OWNER": "tester"},
    )
    return settings, ArtifactStore(settings)


def test_save_writes_frontmatter_and_identity(tmp_path: Path) -> None:
    _settings, store = make_store(tmp_path)
    artifact = store.save("design.md", BODY, stage="S3", owner="lin.xu")

    assert store.path_of("design.md").is_file()
    assert artifact.meta["id"].startswith("ART-DESIGN-")
    assert artifact.meta["name"] == "design.md"
    assert artifact.meta["stage"] == "S3"
    assert artifact.version == 1
    assert artifact.status == "draft"
    assert artifact.meta["owner"] == "lin.xu"
    assert artifact.meta["created_at"] and artifact.meta["updated_at"]
    assert artifact.meta["hash"] == sha256_12(BODY.rstrip("\n"))
    assert artifact.hash == artifact.meta["hash"]
    assert artifact.meta["toc"] == ["1 选型对比", "2 模块改动", "3 风险与回滚"]

    on_disk = store.load("design.md")
    assert on_disk.meta == artifact.meta
    assert on_disk.hash == artifact.hash


def test_save_bumps_version_and_backs_up(tmp_path: Path) -> None:
    _settings, store = make_store(tmp_path)
    first = store.save("design.md", BODY, stage="S3")
    second = store.save("design.md", BODY + "\n补充：追加一节。\n", stage="S3")

    assert second.version == 2
    assert second.meta["id"] == first.meta["id"]
    assert second.meta["created_at"] == first.meta["created_at"]
    backups = store.versions("design.md")
    assert [path.name for path in backups] == ["design.v1.md"]
    backup_meta, _body = _read_artifact(backups[0])
    assert backup_meta["version"] == 1


def test_hash_detects_manual_edit(tmp_path: Path) -> None:
    """AC-10：approved 产物正文被手工改写后，hash 与落盘时不一致。"""
    _settings, store = make_store(tmp_path)
    artifact = store.save("design.md", BODY, stage="S3", status="approved")
    path = store.path_of("design.md")
    path.write_text(path.read_text(encoding="utf-8") + "\n> 手工追加：模型顺手优化\n", encoding="utf-8")

    reloaded = store.load("design.md")
    assert reloaded.hash != artifact.meta["hash"]
    assert reloaded.hash != artifact.hash


def test_save_is_atomic_no_tmp_files(tmp_path: Path) -> None:
    _settings, store = make_store(tmp_path)
    store.save("design.md", BODY, stage="S3")
    leftovers = [path.name for path in store.artifacts_dir.iterdir() if path.name.endswith(".tmp")]
    assert leftovers == []


def test_load_missing_and_load_or_none(tmp_path: Path) -> None:
    _settings, store = make_store(tmp_path)
    with pytest.raises(ArtifactNotFound):
        store.load("design.md")
    assert store.load_or_none("design.md") is None
    assert store.exists("design.md") is False


def test_list_sorted_by_stage(tmp_path: Path) -> None:
    _settings, store = make_store(tmp_path)
    store.save("tasks.md", "## 1 任务表\n\n## 2 依赖顺序\n", stage="S4", meta={"feature_id": "FEAT-1"})
    store.save("design.md", BODY, stage="S3")
    names = [artifact.name for artifact in store.list()]
    assert names == ["design.md", "tasks.md"]


def test_versions_and_restore(tmp_path: Path) -> None:
    _settings, store = make_store(tmp_path)
    store.save("design.md", BODY, stage="S3")
    v2_body = BODY + "\n追加 v2。\n"
    store.save("design.md", v2_body, stage="S3")
    store.save("design.md", BODY + "\n追加 v3。\n", stage="S3")

    assert [path.name for path in store.versions("design.md")] == ["design.v1.md", "design.v2.md"]
    latest = store.restore("design.md")
    assert latest.version == 2
    assert latest.body == v2_body.rstrip("\n")

    oldest = store.restore("design.md", 1)
    assert oldest.version == 1
    assert oldest.body == BODY.rstrip("\n")
    assert store.load("design.md").body == BODY.rstrip("\n")


def test_restore_missing_backup_raises(tmp_path: Path) -> None:
    _settings, store = make_store(tmp_path)
    with pytest.raises(ArtifactNotFound):
        store.restore("design.md")
    store.save("design.md", BODY, stage="S3")
    with pytest.raises(ArtifactNotFound):
        store.restore("design.md", 9)


def test_set_status_keeps_version_and_hash(tmp_path: Path) -> None:
    _settings, store = make_store(tmp_path)
    saved = store.save("design.md", BODY, stage="S3")
    approved = store.set_status("design.md", "approved", owner="reviewer")
    assert approved.status == "approved"
    assert approved.version == saved.version
    assert approved.hash == saved.hash
    assert approved.meta["owner"] == "reviewer"
    with pytest.raises(ArtifactError):
        store.set_status("design.md", "archived")


def test_save_invalid_status_raises(tmp_path: Path) -> None:
    _settings, store = make_store(tmp_path)
    with pytest.raises(ArtifactError):
        store.save("design.md", BODY, status="done")


def test_save_bump_version_false_keeps_version(tmp_path: Path) -> None:
    _settings, store = make_store(tmp_path)
    first = store.save("design.md", BODY, stage="S3")
    second = store.save("design.md", BODY, stage="S3", bump_version=False)
    assert second.version == first.version
    assert store.versions("design.md") == []


def test_summary_from_meta_or_derived(tmp_path: Path) -> None:
    _settings, store = make_store(tmp_path)
    with_summary = store.save("design.md", BODY, stage="S3", meta={"summary": "选型用标准库"})
    assert with_summary.summary() == "选型用标准库"
    without = store.save("tasks.md", "## 1 任务表\n\n把导入拆成 T1、T2。\n", stage="S4")
    assert without.summary()


def test_sections_and_section_body(tmp_path: Path) -> None:
    _settings, store = make_store(tmp_path)
    artifact = store.save("design.md", BODY, stage="S3")
    assert artifact.sections() == ["选型对比", "模块改动", "风险与回滚"]
    assert artifact.has_section("选型对比") is True
    assert artifact.has_section("2 模块改动") is True
    assert artifact.has_section("不存在的章节") is False
    assert artifact.section_body("选型对比") == "用标准库 csv。"
    assert artifact.section_body("模块改动") == "改 importer。"
    assert artifact.section_body("不存在") is None


def test_appendix_parsing(tmp_path: Path) -> None:
    _settings, store = make_store(tmp_path)
    body = BODY + '\n## 附录：结构化数据\n\n```json\n{"risks": [{"id": "R1"}]}\n```\n'
    artifact = store.save("design.md", body, stage="S3")
    assert artifact.appendix() == {"risks": [{"id": "R1"}]}
    assert artifact.meta["toc"] == ["1 选型对比", "2 模块改动", "3 风险与回滚"]

    broken = store.save(
        "tasks.md",
        "## 1 任务表\n\n## 附录：结构化数据\n\n```json\n{不是合法 JSON}\n```\n",
        stage="S4",
        meta={"feature_id": "FEAT-1"},
    )
    assert broken.appendix() is None

    no_appendix = store.save("prd.md", "## 1 背景\n\n无。\n", stage="S1", meta={"source": {"type": "file", "ref": "x"}})
    assert no_appendix.appendix() is None


def test_remove(tmp_path: Path) -> None:
    _settings, store = make_store(tmp_path)
    store.save("design.md", BODY, stage="S3")
    store.remove("design.md")
    assert store.exists("design.md") is False
    store.remove("design.md")  # 幂等


def _read_artifact(path: Path):
    from specflow.artifacts.frontmatter import parse

    meta, body = parse(path.read_text(encoding="utf-8"))
    return meta, body