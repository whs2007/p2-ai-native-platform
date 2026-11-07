"""front-matter YAML 子集：解析 / 渲染 / 目录（docs/07 §4.1、ADR-003）。"""
from __future__ import annotations

import pytest

from specflow.artifacts import frontmatter
from specflow.errors import ArtifactError

SAMPLE = """---
id: ART-DESIGN-0003
name: design.md
stage: S3
version: 2
status: draft
owner: lin.xu
source: {type: file, ref: .spec/artifacts/exploration.md}
summary: 用事件表替换轮询，接口保持兼容
toc: ["1 选型对比", "2 模块改动"]
hash: 9f2c1a7b4d05
---

# 技术方案

## 1 选型对比

用标准库。

## 2 模块改动

改 importer。
"""


def test_parse_extracts_meta_and_body() -> None:
    meta, body = frontmatter.parse(SAMPLE)
    assert meta["id"] == "ART-DESIGN-0003"
    assert meta["version"] == 2
    assert meta["status"] == "draft"
    assert meta["source"] == {"type": "file", "ref": ".spec/artifacts/exploration.md"}
    assert meta["toc"] == ["1 选型对比", "2 模块改动"]
    assert body == "\n# 技术方案\n\n## 1 选型对比\n\n用标准库。\n\n## 2 模块改动\n\n改 importer。\n"


def test_parse_scalar_types() -> None:
    text = "---\nint: 3\nfloat: 1.5\nbool: true\nfalse_bool: false\nnull_value: null\nstr: abc\n---\nbody\n"
    meta, _body = frontmatter.parse(text)
    assert meta["int"] == 3 and isinstance(meta["int"], int)
    assert meta["float"] == 1.5 and isinstance(meta["float"], float)
    assert meta["bool"] is True
    assert meta["false_bool"] is False
    assert meta["null_value"] is None
    assert meta["str"] == "abc"


def test_parse_quoted_scalars_with_special_chars() -> None:
    text = (
        '---\n'
        'a: "x: y, z"\n'
        'b: "true"\n'
        'c: "123"\n'
        'd: "say \\"hi\\""\n'
        '---\n'
    )
    meta, _body = frontmatter.parse(text)
    assert meta["a"] == "x: y, z"
    assert meta["b"] == "true"
    assert meta["c"] == "123"
    assert meta["d"] == 'say "hi"'


def test_parse_nested_inline_structures() -> None:
    text = '---\nsource: {type: doc, ref: DOC-2041, meta: {claimed: true}}\ntags: [a, [b, c]]\n---\n'
    meta, _body = frontmatter.parse(text)
    assert meta["source"]["meta"] == {"claimed": True}
    assert meta["tags"] == ["a", ["b", "c"]]


def test_parse_missing_frontmatter_raises() -> None:
    with pytest.raises(ArtifactError):
        frontmatter.parse("# 没有 front-matter\n")


def test_parse_unclosed_frontmatter_raises() -> None:
    with pytest.raises(ArtifactError):
        frontmatter.parse("---\nid: ART-1\nname: x.md\n")


def test_parse_unsupported_yaml_raises() -> None:
    with pytest.raises(ArtifactError):
        frontmatter.parse("---\n- item\n---\nbody\n")
    with pytest.raises(ArtifactError):
        frontmatter.parse("---\n# comment\nid: x\n---\n")


def test_parse_bom_and_crlf() -> None:
    text = "\ufeff---\r\nid: a\r\n---\r\nbody\r\n"
    meta, body = frontmatter.parse(text)
    assert meta["id"] == "a"
    assert body == "body\n"


def test_render_roundtrip_and_stable_order() -> None:
    meta, body = frontmatter.parse(SAMPLE)
    rendered = frontmatter.render(meta, body)
    assert rendered.endswith("\n") and not rendered.endswith("\n\n")
    reparsed_meta, reparsed_body = frontmatter.parse(rendered)
    assert reparsed_meta == meta
    assert reparsed_body.rstrip("\n") == body.rstrip("\n")
    # 稳定顺序：id 在 summary 前、toc 在 hash 前；未知字段按字母序追加
    order = [line.split(":")[0] for line in rendered.split("---")[1].strip().splitlines()]
    assert order.index("id") < order.index("summary") < order.index("toc") < order.index("hash")
    with_extra = frontmatter.render({**meta, "zzz": "1", "aaa": "2"}, body)
    keys = [line.split(":")[0] for line in with_extra.split("---")[1].strip().splitlines()]
    assert keys.index("aaa") < keys.index("zzz")


def test_render_quotes_ambiguous_values_and_types() -> None:
    meta = {
        "colon": "a: b",
        "comma": "x,y",
        "bool_like": "true",
        "number_like": "123",
        "empty": "",
        "real_bool": True,
        "real_int": 7,
        "list": ["1 选型对比", "含,逗号"],
        "dict": {"type": "file", "ref": "a.md"},
    }
    rendered = frontmatter.render(meta, "")
    parsed, _body = frontmatter.parse(rendered)
    assert parsed == meta
    assert parsed["real_bool"] is True
    assert parsed["bool_like"] == "true"


def test_render_rejects_unsupported_type() -> None:
    with pytest.raises(ArtifactError):
        frontmatter.render({"bad": {1, 2}}, "body")


def test_generate_toc_ignores_subheadings_fences_and_appendix() -> None:
    body = (
        "# 标题\n\n"
        "## 1 选型对比\n\n"
        "### 1.1 细节\n\n"
        "```json\n## 不是标题\n```\n\n"
        "## 2 风险与回滚\n\n"
        "## 附录：结构化数据\n\n"
        "```json\n{}\n```\n"
    )
    assert frontmatter.generate_toc(body) == ["1 选型对比", "2 风险与回滚"]


def test_generate_toc_empty_body() -> None:
    assert frontmatter.generate_toc("") == []
    assert frontmatter.generate_toc("没有二级标题\n") == []