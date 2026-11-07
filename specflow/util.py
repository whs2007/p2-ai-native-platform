"""通用工具：时间 / hash / token 估算 / 原子读写 / 参数解析（docs/07 §2.3）。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from specflow.errors import UsageError

_TMP_SUFFIX = ".tmp"


# ---------------------------------------------------------------------- #
# 时间
# ---------------------------------------------------------------------- #
def now_iso() -> str:
    """本地时区 ISO8601，精确到秒，带偏移：2026-10-30T20:14:03+08:00。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


# ---------------------------------------------------------------------- #
# hash / id
# ---------------------------------------------------------------------- #
def sha256_12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()[:12]


def new_artifact_id(prefix: str, existing: Iterable[str]) -> str:
    """生成 ART-<PREFIX>-000N，N 在已有同前缀 id 上递增。"""
    prefix = prefix.upper()
    pattern = re.compile(rf"ART-{re.escape(prefix)}-(\d+)$")
    highest = 0
    for value in existing:
        match = pattern.match(str(value))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"ART-{prefix}-{highest + 1:04d}"


def short_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6].upper()}"


# ---------------------------------------------------------------------- #
# token 估算（只用于预算与指标，不追求与真实 tokenizer 一致）
# ---------------------------------------------------------------------- #
def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3000 <= code <= 0x303F
        or 0x3400 <= code <= 0x4DBF
        or 0xFF00 <= code <= 0xFFEF
    )


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for char in text if _is_cjk(char))
    other = len(text) - cjk
    return max(1, math.ceil(cjk + other / 4))


def truncate_by_tokens(text: str, budget: int) -> tuple[str, bool]:
    """按估算 token 截断，返回 (文本, 是否截断)。截断时追加标记。"""
    if budget <= 0 or estimate_tokens(text) <= budget:
        return text, False
    marker = "\n…（已截断）"
    marker_cost = estimate_tokens(marker)
    allowance = max(1, budget - marker_cost)
    cost = 0.0
    cut = 0
    for index, char in enumerate(text):
        cost += 1.0 if _is_cjk(char) else 0.25
        if cost > allowance:
            cut = index
            break
        cut = index + 1
    return text[:cut].rstrip() + marker, True


# ---------------------------------------------------------------------- #
# 文件与 JSON
# ---------------------------------------------------------------------- #
def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_text(path: Path, text: str) -> None:
    """先写同目录临时文件再 os.replace，避免半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}{_TMP_SUFFIX}")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise UsageError(f"JSON 文件损坏：{path}（{exc}）") from exc


def dump_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def append_jsonl(path: Path, record: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)


def rel_posix(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


# ---------------------------------------------------------------------- #
# 参数解析
# ---------------------------------------------------------------------- #
def parse_kv_flags(argv: list[str]) -> tuple[list[str], dict[str, Any]]:
    """`--task T1 --skip-rpc --fixture=pipeline-fail` →

    (["T1"], {"task": "T1", "skip_rpc": True, "fixture": "pipeline-fail"})。
    支持 `--key=value` / `--key value` / 无值布尔；纯数字值转 int。
    """
    positionals: list[str] = []
    flags: dict[str, Any] = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token.startswith("--") and len(token) > 2:
            body = token[2:]
            if "=" in body:
                key, value = body.split("=", 1)
            else:
                key = body
                value = None
                if index + 1 < len(argv) and not argv[index + 1].startswith("-"):
                    value = argv[index + 1]
                    index += 1
            key = key.replace("-", "_")
            if value is None:
                flags[key] = True
            elif value.isdigit():
                flags[key] = int(value)
            else:
                flags[key] = value
        else:
            positionals.append(token)
        index += 1
    return positionals, flags