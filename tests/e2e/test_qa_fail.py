"""AC-19 失败分支：pipeline fixture 失败时 QA verdict=fail，且 /archive 被门控阻断。

与 test_flow.py 共享同一条离线链路，但只跑到 QA，验证"失败不能带病归档"。
"""
from __future__ import annotations

from pathlib import Path

from specflow.artifacts.store import ArtifactStore
from specflow.cli import main


def run(settings, argv: list[str], expect: int = 0) -> int:
    code = main(argv, settings=settings)
    assert code == expect, f"specflow {' '.join(argv)} 退出码 {code}（期望 {expect}）"
    return code


def test_qa_pipeline_failure_blocks_archive(settings, repo: Path) -> None:
    store = ArtifactStore(settings)
    run(settings, ["init"])
    run(settings, ["run", "/project-init"])
    run(settings, ["run", "/domain-input"])
    run(settings, ["run", "/prd-input", "docs/prd/visit-import.md"])
    run(settings, ["run", "/explore"])
    run(settings, ["run", "/tech-design"])
    run(settings, ["gate", "approve", "design", "--as", "tester"])
    run(settings, ["run", "/plan-task"])
    run(settings, ["run", "/add", "--task", "T1"])

    # 故意注入 pipeline 失败样例：QA 报告必须给出 fail 与精确定位
    run(settings, ["run", "/qa-validate", "--fixture", "pipeline-fail"])
    qa = store.load("qa-report.md")
    appendix = qa.appendix() or {}
    assert appendix["verdict"] == "fail"
    evidence = {item["type"]: item for item in appendix["evidence"]}
    assert evidence["pipeline"]["status"] == "failed"
    assert evidence["unit"]["status"] == "passed"
    assert qa.status == "draft"

    # 失败报告不能进入 S7
    run(settings, ["run", "/archive"], expect=2)

    # 默认（修复后）再跑一次 → pass，archive 放行
    run(settings, ["run", "/qa-validate"])
    assert (store.load("qa-report.md").appendix() or {})["verdict"] == "pass"
    run(settings, ["run", "/archive"])
    assert store.exists("archive-record.md")