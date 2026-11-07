"""产物 front-matter 的 YAML 子集解析 / 渲染（docs/07 §4.1、ADR-003）。

只支持：
- `key: scalar`（字符串 / 整数 / 浮点 / 布尔 / null）
- `key: [a, b, c]`（内联列表，元素可递归为标量/列表/字典）
- `key: {k: v, k2: v2}`（内联字典，值可递归）
- 字符串在含 `: `、`,`、引号或首尾有空白时用双引号包裹

不支持多行、注释、锚点、块序列（`- item` 独占一行）——复杂结构放正文附录 JSON。
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from specflow.errors import ArtifactError

_DELIMITER = "---"
_KEY_RE = re.compile(r"^([A-Za-z0-9_.\-]+)\s*:\s*(.*)$")
_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+$")
_TRUE = {"true", "yes", "on"}
_FALSE = {"false", "no", "off"}
_NULL = {"null", "~", "none"}
#: 需要加引号的字符（避免解析歧义）
_QUOTE_CHARS = set(',:[]{}"\\')
_QUOTE_PREFIX = ("#", "-", "?", "&", "*", "!", "|", ">", "@", "`", "%", "'")

#: front-matter 推荐字段顺序（其余字段按字母序追加，保证渲染稳定）
_FIELD_ORDER: tuple[str, ...] = (
    "id",
    "name",
    "stage",
    "version",
    "status",
    "owner",
    "created_at",
    "updated_at",
    "source",
    "summary",
    "toc",
    "hash",
)


# ---------------------------------------------------------------------- #
# 解析
# ---------------------------------------------------------------------- #
def parse(text: str) -> tuple[dict[str, Any], str]:
    """把产物文本拆成 `(meta, body)`；没有 front-matter 抛 `ArtifactError`。"""
    normalized = _normalize_newlines(text)
    if normalized.startswith("\ufeff"):
        normalized = normalized[1:]
    lines = normalized.split("\n")
    if not lines or lines[0].strip() != _DELIMITER:
        raise ArtifactError("产物缺少 front-matter：文件必须以 '---' 开头")

    meta: dict[str, Any] = {}
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == _DELIMITER:
            body = "\n".join(lines[index + 1 :])
            return meta, body
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            raise ArtifactError("front-matter 不支持 YAML 注释（`#`）")
        match = _KEY_RE.match(stripped)
        if not match:
            raise ArtifactError(f"front-matter 第 {index + 1} 行不是 `key: value` 形式：{stripped!r}")
        key = match.group(1)
        if key in meta:
            raise ArtifactError(f"front-matter 字段重复：{key}")
        meta[key] = _parse_value(match.group(2))
    raise ArtifactError("front-matter 未闭合：缺少结尾的 '---'")


def _parse_value(raw: str) -> Any:
    value = raw.strip()
    if not value:
        return ""
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_value(part) for part in _split_top_level(inner)]
    if value.startswith("{") and value.endswith("}"):
        inner = value[1:-1].strip()
        if not inner:
            return {}
        result: dict[str, Any] = {}
        for part in _split_top_level(inner):
            key, found, rest = _split_key_value(part)
            if not found:
                raise ArtifactError(f"内联字典缺少 ':'：{part!r}")
            result[key] = _parse_value(rest)
        return result
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return _unquote(value[1:-1])
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    lowered = value.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    if lowered in _NULL:
        return None
    if _INT_RE.match(value):
        return int(value)
    if _FLOAT_RE.match(value):
        return float(value)
    return value


def _split_top_level(text: str) -> list[str]:
    """按顶层逗号切分（忽略引号/括号内的逗号）。"""
    parts: list[str] = []
    buffer: list[str] = []
    depth = 0
    quote: str | None = None
    escaped = False
    for char in text:
        if escaped:
            buffer.append(char)
            escaped = False
            continue
        if quote:
            buffer.append(char)
            if char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            buffer.append(char)
            continue
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            parts.append("".join(buffer))
            buffer = []
            continue
        buffer.append(char)
    if "".join(buffer).strip():
        parts.append("".join(buffer))
    return parts


def _split_key_value(text: str) -> tuple[str, bool, str]:
    quote: str | None = None
    depth = 0
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char in "[{":
            depth += 1
            continue
        if char in "]}":
            depth = max(0, depth - 1)
            continue
        if char == ":" and depth == 0:
            return text[:index].strip(), True, text[index + 1 :].strip()
    return text.strip(), False, ""


def _unquote(inner: str) -> str:
    result: list[str] = []
    escaped = False
    for char in inner:
        if escaped:
            result.append({"n": "\n", "t": "\t", "r": "\r"}.get(char, char))
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        result.append(char)
    if escaped:  # 末尾孤立反斜杠按字面量保留
        result.append("\\")
    return "".join(result)


# ---------------------------------------------------------------------- #
# 渲染
# ---------------------------------------------------------------------- #
def render(meta: Mapping[str, Any], body: str) -> str:
    """稳定字段顺序渲染；输出以单个换行结尾。"""
    lines: list[str] = [_DELIMITER]
    for key in _ordered_keys(meta):
        lines.append(f"{key}: {_render_value(meta[key])}")
    lines.append(_DELIMITER)
    text = "\n".join(lines) + "\n"
    normalized_body = _normalize_body(body)
    if normalized_body:
        text += normalized_body + "\n"
    return text


def _ordered_keys(meta: Mapping[str, Any]) -> list[str]:
    ordered = [key for key in _FIELD_ORDER if key in meta]
    ordered.extend(sorted(key for key in meta if key not in _FIELD_ORDER))
    return ordered


def _render_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _render_scalar(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_render_value(item) for item in value) + "]"
    if isinstance(value, Mapping):
        return "{" + ", ".join(f"{key}: {_render_value(item)}" for key, item in value.items()) + "}"
    raise ArtifactError(f"front-matter 不支持的值类型：{type(value).__name__}")


def _render_scalar(value: str) -> str:
    if _needs_quote(value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return value


def _needs_quote(value: str) -> bool:
    if not value:
        return True
    if value != value.strip():
        return True
    lowered = value.lower()
    if lowered in _TRUE or lowered in _FALSE or lowered in _NULL:
        return True
    if _INT_RE.match(value) or _FLOAT_RE.match(value):
        return True
    if any(char in _QUOTE_CHARS for char in value):
        return True
    if value.startswith(_QUOTE_PREFIX):
        return True
    if "\n" in value or "\r" in value or "\t" in value:
        return True
    return False


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _normalize_body(body: str) -> str:
    """正文统一换行并去掉尾部空行（渲染后由调用方补单个换行）。"""
    return _normalize_newlines(body or "").rstrip("\n")


# ---------------------------------------------------------------------- #
# 目录
# ---------------------------------------------------------------------- #
def generate_toc(body: str) -> list[str]:
    """由正文 `## ` 标题生成目录（忽略代码围栏内与「附录」标题）。"""
    toc: list[str] = []
    in_fence = False
    for line in _normalize_newlines(body or "").split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line.startswith("## "):
            continue
        title = line[3:].strip()
        if not title or title.startswith("附录"):
            continue
        toc.append(title)
    return toc