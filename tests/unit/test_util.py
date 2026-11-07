"""公共内核单元测试：util（docs/07 §2.3）。"""
from __future__ import annotations

from pathlib import Path

from specflow.util import (
    atomic_write_text,
    estimate_tokens,
    new_artifact_id,
    parse_kv_flags,
    read_text,
    rel_posix,
    sha256_12,
    truncate_by_tokens,
)


def test_sha256_12_stable_and_short() -> None:
    value = sha256_12("hello 世界")
    assert len(value) == 12
    assert value == sha256_12("hello 世界")
    assert value != sha256_12("hello 世界!")


def test_estimate_tokens_cjk_and_ascii() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("中文四个字") == 5
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_tokens("中a") == 2


def test_truncate_by_tokens_marks_cut() -> None:
    text = "中" * 100
    cut, truncated = truncate_by_tokens(text, 20)
    assert truncated is True
    assert cut.endswith("（已截断）")
    assert estimate_tokens(cut) <= 25
    same, flag = truncate_by_tokens("短文本", 50)
    assert same == "短文本" and flag is False


def test_atomic_write_and_read(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "a.md"
    atomic_write_text(target, "第一行\n第二行\n")
    assert read_text(target) == "第一行\n第二行\n"
    assert not list(target.parent.glob("*.tmp"))


def test_new_artifact_id_increments() -> None:
    assert new_artifact_id("design", []) == "ART-DESIGN-0001"
    assert new_artifact_id("design", ["ART-DESIGN-0002"]) == "ART-DESIGN-0003"
    assert new_artifact_id("prd", ["ART-DESIGN-0009"]) == "ART-PRD-0001"


def test_parse_kv_flags() -> None:
    positionals, flags = parse_kv_flags(
        ["docs/prd/x.md", "--task", "T1", "--skip-rpc", "--max-hours=2", "--note", "缺压测"]
    )
    assert positionals == ["docs/prd/x.md"]
    assert flags == {"task": "T1", "skip_rpc": True, "max_hours": 2, "note": "缺压测"}


def test_rel_posix(tmp_path: Path) -> None:
    nested = tmp_path / "src" / "a.py"
    nested.parent.mkdir()
    nested.write_text("x", encoding="utf-8")
    assert rel_posix(nested, tmp_path) == "src/a.py"