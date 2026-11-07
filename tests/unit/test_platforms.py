"""T4 交付验收：平台 mock 五子域、mock_data、fixture 仓库与产物模板。

覆盖 docs/07 §5.9（平台协议 + 固定返回结构 + 失败 fixture）、§5.8（演示任务契约的
PRD/领域素材）、§6（`/project-init` 画像素材）与 AC-02/AC-03/AC-19 的数据前提。

约定：unit 层不写 fixture 本体；需要运行 fixture 测试时复制到 `tmp_path` 副本。
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from specflow.config import Settings
from specflow.errors import UsageError
from specflow.platforms import get_platform
from specflow.platforms.base import PlatformAdapter
from specflow.platforms.http import HttpPlatform
from specflow.platforms.mock import MockPlatform

PLATFORM_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_REPO = PLATFORM_ROOT / "fixtures" / "demo-repo"
MOCK_DATA_DIR = PLATFORM_ROOT / "specflow" / "platforms" / "mock_data"
TEMPLATES_DIR = PLATFORM_ROOT / "specflow" / "templates"
MOCK_DATA_FILES = ("docs_shuttle", "feature", "pipeline", "rpc", "sso")
TEMPLATE_NAMES = ("prd.md", "design.md", "tasks.md", "test_unit.py", "qa-report.md")

_ABS_PATH = re.compile(r"[A-Za-z]:[\\/](?![\\/])|/Users/|/home/|\\\\Users\\\\|Desktop")


def make_settings(tmp_path: Path, fixture: str | None = None) -> Settings:
    return Settings(
        project_dir=tmp_path,
        spec_dir=tmp_path / ".spec",
        platform_fixture=fixture,
    )


def load_json(name: str) -> dict:
    return json.loads((MOCK_DATA_DIR / f"{name}.json").read_text(encoding="utf-8"))


def text_files(root: Path) -> list[Path]:
    return [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in {".md", ".py", ".toml", ".json", ".gitignore", ".txt"}
    ]


# ---------------------------------------------------------------------- #
# 1. 五个子域的结构（docs/07 §5.9 固定键）
# ---------------------------------------------------------------------- #
def test_get_platform_returns_platform_adapter(tmp_path: Path) -> None:
    platform = get_platform(make_settings(tmp_path))
    assert isinstance(platform, MockPlatform)
    assert isinstance(platform, PlatformAdapter)


def test_get_platform_returns_http_adapter_for_live_mode(tmp_path: Path) -> None:
    settings = Settings(
        project_dir=tmp_path,
        spec_dir=tmp_path / ".spec",
        platform_mode="live",
        platform_base_url="http://127.0.0.1:1",
        platform_token="test-token",
    )
    platform = get_platform(settings)
    assert isinstance(platform, HttpPlatform)


def test_whoami_returns_lin_xu(tmp_path: Path) -> None:
    user = get_platform(make_settings(tmp_path)).whoami()
    assert user["name"] == "lin.xu"
    assert user["user_id"] == "U-1001"
    assert user["authenticated"] is True
    assert "engineer" in user["roles"]


def test_docs_list_returns_items_with_doc_2041(tmp_path: Path) -> None:
    payload = get_platform(make_settings(tmp_path)).docs_list()
    assert set(payload) == {"items"}
    items = payload["items"]
    assert isinstance(items, list) and len(items) >= 1
    doc = next(item for item in items if item["doc_id"] == "DOC-2041")
    assert set(doc) == {"doc_id", "title", "status", "url"}
    assert "PRD" in doc["title"]


def test_docs_get_returns_text_images_and_claimed_by(tmp_path: Path) -> None:
    platform = get_platform(make_settings(tmp_path))
    doc = platform.docs_get("DOC-2041")
    assert set(doc) == {"doc_id", "title", "text", "images", "claimed_by"}
    assert "REQ-01" in doc["text"] and "REQ-03" in doc["text"]
    assert "store_code" in doc["text"] and "plan_date" in doc["text"]
    assert doc["images"] and {"name", "caption", "text"} <= set(doc["images"][0])
    assert doc["claimed_by"] is None


def test_docs_claim_sets_owner_and_refuses_stealing(tmp_path: Path) -> None:
    platform = get_platform(make_settings(tmp_path))
    claimed = platform.docs_claim("DOC-2041", "tester")
    assert claimed == {"doc_id": "DOC-2041", "claimed_by": "tester", "claimed": True}
    assert platform.docs_get("DOC-2041")["claimed_by"] == "tester"

    second = platform.docs_claim("DOC-2041", "someone-else")
    assert second["claimed"] is False
    assert second["claimed_by"] == "tester"
    assert platform.docs_get("DOC-2041")["claimed_by"] == "tester"


def test_docs_ocr_aggregates_image_text(tmp_path: Path) -> None:
    payload = get_platform(make_settings(tmp_path)).docs_ocr("DOC-2041")
    assert set(payload) == {"doc_id", "images", "text", "count"}
    assert payload["count"] == len(payload["images"]) == 2
    assert "store_code" in payload["text"]
    assert "保留首次" in payload["text"]


def test_docs_claim_is_per_adapter_instance(tmp_path: Path) -> None:
    get_platform(make_settings(tmp_path)).docs_claim("DOC-2041", "tester")
    fresh = get_platform(make_settings(tmp_path))
    assert fresh.docs_get("DOC-2041")["claimed_by"] is None


def test_feature_create_bind_and_contract(tmp_path: Path) -> None:
    platform = get_platform(make_settings(tmp_path))
    created = platform.feature_create("visit-import", "tester")
    assert created["feature_id"] == "FEAT-1024"
    assert created["branch"] == "feature/visit-import"
    assert created["created"] is False

    bound = platform.feature_bind("FEAT-1024", "T1", "feature/visit-import")
    assert bound == {
        "feature_id": "FEAT-1024",
        "task_id": "T1",
        "branch": "feature/visit-import",
        "bound": True,
    }

    contract = platform.feature_contract("FEAT-1024")
    assert contract["feature_id"] == "FEAT-1024"
    assert contract["branch"] == "feature/visit-import"
    assert contract["base_branch"] == "main"
    assert contract["required_checks"] and contract["reviewers"]


def test_feature_create_unknown_name_generates_next_id(tmp_path: Path) -> None:
    platform = get_platform(make_settings(tmp_path))
    created = platform.feature_create("visit-export", "tester")
    assert created["created"] is True
    assert created["feature_id"] == "FEAT-1026"
    assert created["branch"] == "feature/visit-export"
    assert platform.feature_contract("FEAT-1026")["feature_id"] == "FEAT-1026"


def test_pipeline_default_passed(tmp_path: Path) -> None:
    platform = get_platform(make_settings(tmp_path))
    triggered = platform.pipeline_trigger("feature/visit-import")
    assert triggered == {"run_id": "RUN-2031", "branch": "feature/visit-import"}

    status = platform.pipeline_status(triggered["run_id"])
    assert set(status) == {"run_id", "status", "duration_s", "url"}
    assert status["status"] == "passed"
    assert status["duration_s"] > 0
    assert status["url"].startswith("https://")


def test_pipeline_fail_fixture_returns_failed_status(tmp_path: Path) -> None:
    platform = get_platform(make_settings(tmp_path, fixture="pipeline-fail"))
    triggered = platform.pipeline_trigger("feature/visit-import")
    assert triggered["run_id"] == "RUN-2032"
    assert platform.pipeline_status(triggered["run_id"])["status"] == "failed"
    # 失败 fixture 下任何 run 的状态都按 failed 返回（QA 演示失败分支）
    assert platform.pipeline_status("RUN-2031")["status"] == "failed"


def test_rpc_validate_returns_five_cases(tmp_path: Path) -> None:
    result = get_platform(make_settings(tmp_path)).rpc_call("visit.import.validate", {"cases": 5})
    assert result == {
        "ok": True,
        "method": "visit.import.validate",
        "result": result["result"],
    }
    assert result["result"]["cases"] == 5
    assert result["result"]["valid"] is True


def test_rpc_error_fixture_returns_error_payload(tmp_path: Path) -> None:
    result = get_platform(make_settings(tmp_path, fixture="rpc-error")).rpc_call(
        "visit.import.validate", {"cases": 5}
    )
    assert result["ok"] is False
    assert result["method"] == "visit.import.validate"
    assert result["error"]["code"] == "RPC_PLATFORM_ERROR"
    assert result["error"]["message"]


def test_unknown_rpc_method_raises(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        get_platform(make_settings(tmp_path)).rpc_call("visit.import.unknown", {})


def test_unknown_doc_and_run_raise(tmp_path: Path) -> None:
    platform = get_platform(make_settings(tmp_path))
    with pytest.raises(UsageError):
        platform.docs_get("DOC-9999")
    with pytest.raises(UsageError):
        platform.pipeline_status("RUN-9999")
    with pytest.raises(UsageError):
        platform.feature_contract("FEAT-9999")


def test_unknown_fixture_raises(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        MockPlatform(make_settings(tmp_path, fixture="no-such-fixture"))


def test_returned_payloads_are_deep_copies(tmp_path: Path) -> None:
    platform = get_platform(make_settings(tmp_path))
    first = platform.docs_get("DOC-2041")
    first["text"] = "tampered"
    first["images"][0]["text"] = "tampered"
    assert platform.docs_get("DOC-2041")["text"] != "tampered"
    assert platform.docs_get("DOC-2041")["images"][0]["text"] != "tampered"

    listing = platform.docs_list()
    listing["items"][0]["title"] = "tampered"
    assert platform.docs_list()["items"][0]["title"] != "tampered"

    contract = platform.feature_contract("FEAT-1024")
    contract["required_checks"].append("tampered")
    assert "tampered" not in platform.feature_contract("FEAT-1024")["required_checks"]


# ---------------------------------------------------------------------- #
# 2. mock_data 合法性与 fixture 需求一致性（AC-19 平台段）
# ---------------------------------------------------------------------- #
def test_mock_data_files_exist_and_are_valid_json() -> None:
    assert MOCK_DATA_DIR.is_dir()
    for name in MOCK_DATA_FILES:
        data = load_json(name)
        assert isinstance(data, dict) and data, f"mock_data/{name}.json 不能为空"


def test_mock_data_covers_ac_19_platform_segments() -> None:
    docs = load_json("docs_shuttle")
    doc_ids = [doc["doc_id"] for doc in docs["documents"]]
    assert "DOC-2041" in doc_ids

    feature = load_json("feature")
    feature_ids = [item["feature_id"] for item in feature["features"]]
    assert "FEAT-1024" in feature_ids

    pipeline = load_json("pipeline")
    statuses = {run["status"] for run in pipeline["runs"].values()}
    assert {"passed", "failed"} <= statuses
    assert pipeline["default_run_id"] in pipeline["runs"]
    assert pipeline["failed_run_id"] in pipeline["runs"]

    rpc = load_json("rpc")
    assert rpc["methods"]["visit.import.validate"]["result"]["cases"] == 5
    assert rpc["error"]["code"] and rpc["error"]["message"]

    sso = load_json("sso")
    assert sso["default_user"] == "lin.xu"
    assert sso["users"]["lin.xu"]["name"] == "lin.xu"


def test_mock_data_is_consistent_with_demo_prd() -> None:
    prd = (FIXTURE_REPO / "docs" / "prd" / "visit-import.md").read_text(encoding="utf-8")
    doc_text = load_json("docs_shuttle")["documents"][0]["text"]
    for token in ("REQ-01", "REQ-02", "REQ-03"):
        assert token in prd and token in doc_text
    for field in ("store_code", "store_name", "plan_date", "owner"):
        assert field in doc_text
    assert "去重" in doc_text


def test_mock_data_has_no_absolute_paths_or_private_markers() -> None:
    for name in MOCK_DATA_FILES:
        raw = (MOCK_DATA_DIR / f"{name}.json").read_text(encoding="utf-8")
        assert not _ABS_PATH.search(raw), f"{name}.json 含疑似绝对路径：{_ABS_PATH.search(raw)}"
        assert ".spec" not in raw and "AGENTS.md" not in raw


# ---------------------------------------------------------------------- #
# 3. fixture 仓库（AC-02/AC-03 素材 + 演示任务契约）
# ---------------------------------------------------------------------- #
def test_fixture_repo_layout_is_complete() -> None:
    required = [
        "pyproject.toml",
        "README.md",
        "ruff.toml",
        ".gitignore",
        "src/visit_plan/__init__.py",
        "src/visit_plan/__main__.py",
        "src/visit_plan/models.py",
        "src/visit_plan/importer.py",
        "tests/test_models.py",
        "docs/prd/visit-import.md",
        "docs/domain/visit.md",
    ]
    for rel in required:
        assert (FIXTURE_REPO / rel).is_file(), f"fixture 缺少 {rel}"
    assert not (FIXTURE_REPO / ".spec").exists()
    assert not (FIXTURE_REPO / "AGENTS.md").exists()


def test_fixture_profile_materials_cover_ac_02() -> None:
    pyproject = (FIXTURE_REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.pytest.ini_options]" in pyproject
    assert "testpaths" in pyproject
    assert "no:cacheprovider" in pyproject  # Windows 沙箱下避免 0o700 缓存目录

    readme = (FIXTURE_REPO / "README.md").read_text(encoding="utf-8")
    for command in ("python -m pytest -q", "ruff check src tests", "python -m visit_plan"):
        assert command in readme

    main_py = (FIXTURE_REPO / "src" / "visit_plan" / "__main__.py").read_text(encoding="utf-8")
    assert "def main(" in main_py
    assert "src/visit_plan/__main__.py" in main_py

    importer = (FIXTURE_REPO / "src" / "visit_plan" / "importer.py").read_text(encoding="utf-8")
    assert "raise NotImplementedError" in importer
    assert "def import_plans(path: Path) -> list[Plan]:" in importer

    models = (FIXTURE_REPO / "src" / "visit_plan" / "models.py").read_text(encoding="utf-8")
    assert "@dataclass" in models
    for field in ("store_code", "store_name", "plan_date", "owner"):
        assert field in models


def test_fixture_prd_has_requirements_acceptance_and_scope() -> None:
    prd = (FIXTURE_REPO / "docs" / "prd" / "visit-import.md").read_text(encoding="utf-8")
    for req in ("REQ-01", "REQ-02", "REQ-03"):
        assert req in prd
    assert "验收标准" in prd
    assert "范围外" in prd
    assert "AC-0" in prd


def test_fixture_domain_has_five_rules_and_eight_terms() -> None:
    domain = (FIXTURE_REPO / "docs" / "domain" / "visit.md").read_text(encoding="utf-8")

    rules = [line for line in domain.splitlines() if line.startswith("| DR-")]
    assert len(rules) >= 5, f"核心规则不足 5 条：{len(rules)}"
    for line in rules:
        cells = [cell.strip() for cell in line.split("|")]
        assert len(cells) >= 5 and cells[1] and cells[3], f"规则缺少反例：{line}"

    term_section = domain.split("## 3 术语表", 1)[1].split("## ", 1)[0]
    term_rows = [
        line
        for line in term_section.splitlines()
        if line.startswith("|") and not line.startswith("| 术语") and "---" not in line
    ]
    assert len(term_rows) >= 8, f"术语不足 8 条：{len(term_rows)}"


def test_fixture_files_have_no_absolute_paths_or_private_markers() -> None:
    for path in text_files(FIXTURE_REPO):
        text = path.read_text(encoding="utf-8")
        assert not _ABS_PATH.search(text), f"{path} 含疑似绝对路径：{_ABS_PATH.search(text)}"
        assert ".spec/" not in text and not text.lstrip().startswith("AGENTS.md")


def test_fixture_repo_pytest_passes_in_copy(tmp_path: Path) -> None:
    dest = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, dest)
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "pytest"],
        cwd=str(dest),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "3 passed" in proc.stdout


# ---------------------------------------------------------------------- #
# 4. 产物模板（AC-03）
# ---------------------------------------------------------------------- #
def test_templates_cover_required_artifacts() -> None:
    for name in TEMPLATE_NAMES:
        assert (TEMPLATES_DIR / name).is_file(), f"缺少模板 {name}"
    assert len([p for p in TEMPLATES_DIR.iterdir() if p.is_file()]) >= 4


def test_templates_have_placeholders_and_examples() -> None:
    for name in TEMPLATE_NAMES:
        text = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
        assert "<" in text and ">" in text, f"{name} 缺少 <占位符>"
        assert "示例" in text, f"{name} 缺少示例"


def test_test_unit_template_is_valid_python() -> None:
    source = (TEMPLATES_DIR / "test_unit.py").read_text(encoding="utf-8")
    compile(source, "templates/test_unit.py", "exec")


def test_mock_docs_download(tmp_path: Path) -> None:
    platform = get_platform(make_settings(tmp_path))
    download = platform.docs_download("DOC-2041")
    assert download["bytes"] > 0
    assert download["content_type"] == "text/markdown"
    assert "拜访" in download["content"].decode("utf-8")


def test_templates_are_stable_text() -> None:
    for path in sorted(TEMPLATES_DIR.iterdir()):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert text.endswith("\n"), f"{path.name} 末尾应保留单个换行"
            assert "\r\n" not in text, f"{path.name} 必须用 LF 换行"