"""规则回灌：把 `archive-record.md` 的候选规则追加进 `AGENTS.md`（docs/07 §4.6、docs/04 §7）。

规则：
- 去重：与既有规则（或本次已加规则）文本相似度 ≥ 0.9 视为重复 → `skipped_duplicate`；
- 冲突：相似度 ≥ 0.6 且正负极性相反 → 不自动合并，记 `conflicts` 交人工；
- 只追加：不改既有规则；`§2` 硬上限 30 条，超限抛 `UsageError`；
- 版本号 `vX.Y → vX.(Y+1)`，`§5 变更记录` 追加一行；写入前备份旧文件（`backup_path`）。
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any, Iterable

from specflow.errors import ArtifactError, UsageError
from specflow.util import atomic_write_text, now_iso, read_text

_MAX_RULES = 30
_DUPLICATE_THRESHOLD = 0.9
_CONFLICT_THRESHOLD = 0.6
_NEGATIVE_MARKERS = ("禁止", "不得", "不要", "避免", "不允许", "严禁", "不准")
_POSITIVE_MARKERS = ("必须", "需要", "应当", "使用", "保留", "统一", "优先")

_VERSION_RE = re.compile(r"版本：v(\d+)\.(\d+)")
_SECTION_2_RE = re.compile(r"^##\s*2[.、\s]")
_SECTION_5_RE = re.compile(r"^##\s*5[.、\s]")
_ANY_SECTION_RE = re.compile(r"^##\s")
_RULE_LINE_RE = re.compile(r"^-\s*(R-\d+)\s+(.*)$")
_RULE_ID_RE = re.compile(r"^R-(\d+)$")
_NOISE_RE = re.compile(r"[\s，。；、：:;,.!?！？（）()\[\]【】\"'`*_\-]+")


# ---------------------------------------------------------------------- #
# 主入口
# ---------------------------------------------------------------------- #
def apply_rules(
    agents_path: Path,
    proposals: list[dict],
    *,
    owner: str,
    source_req: str,
    backup_path: Path | None = None,
) -> dict:
    """把候选规则追加进 AGENTS.md；返回摘要（见 docs/07 §4.6）。"""
    path = Path(agents_path)
    if not path.is_file():
        raise ArtifactError(f"AGENTS.md 不存在：{path}（先运行 /project-init 生成基线规则）")
    original = read_text(path)
    lines = original.splitlines()

    section_2 = _find_section(lines, _SECTION_2_RE)
    if section_2 is None:
        raise UsageError("AGENTS.md 缺少 `## 2. 工程规则` 章节，无法回灌")
    section_5 = _find_section(lines, _SECTION_5_RE)
    if section_5 is None:
        raise UsageError("AGENTS.md 缺少 `## 5. 变更记录` 章节，无法写版本记录")

    existing = _collect_rules(lines[section_2[0] + 1 : section_2[1]])
    existing_texts = [text for _rule_id, text in existing]
    existing_ids = {rule_id for rule_id, _text in existing}
    next_number = _next_rule_number(existing_ids)

    added_ids: list[str] = []
    reduced: list[tuple[str, str, str]] = []  # (id, text, 完整行)
    skipped_duplicate: list[str] = []
    conflicts: list[str] = []
    known: list[tuple[str, str]] = list(existing)

    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        text = str(proposal.get("text") or "").strip()
        if not text:
            continue
        proposal_id = str(proposal.get("id") or "").strip()

        if _is_duplicate(text, existing_texts + [item[1] for item in reduced]):
            skipped_duplicate.append(proposal_id or text[:20])
            continue

        conflict = _find_conflict(text, known)
        if conflict is not None:
            conflicts.append(
                f"{proposal_id or text[:20]} 与 {conflict[0]} 疑似语义冲突"
                f"（相似度 {conflict[1]:.2f}），不自动合并，请人工确认"
            )
            continue

        rule_id = proposal_id
        if not rule_id or rule_id in existing_ids or rule_id in added_ids:
            rule_id = _next_free_id(existing_ids | set(added_ids), next_number)
            next_number = int(_RULE_ID_RE.match(rule_id).group(1)) + 1  # type: ignore[union-attr]
        if rule_id in existing_ids or rule_id in added_ids:
            conflicts.append(f"{proposal_id or text[:20]} 编号冲突，无法分配新编号，请人工处理")
            continue

        suffix = _proposal_suffix(proposal)
        reduced.append((rule_id, text, f"- {rule_id} {text}{suffix}"))
        added_ids.append(rule_id)
        known.append((rule_id, text))

    if len(existing) + len(reduced) > _MAX_RULES:
        raise UsageError(
            f"AGENTS.md §2 规则将达 {len(existing) + len(reduced)} 条，超过硬上限 {_MAX_RULES} 条；"
            "请先合并同类规则（specflow 不会自动合并）"
        )

    if not reduced:
        return {
            "version": _format_version(_parse_version(original)),
            "added": [],
            "skipped_duplicate": skipped_duplicate,
            "conflicts": conflicts,
            "appended": 0,
        }

    new_version = _bump_version(_parse_version(original))
    updated = _insert_rules(lines, reduced, _SECTION_2_RE)
    updated = _append_change_record(updated, new_version, added_ids, source_req, _SECTION_5_RE)
    updated_text = _update_header(updated, new_version)
    updated_text = updated_text if updated_text.endswith("\n") else updated_text + "\n"

    if backup_path is not None:
        atomic_write_text(Path(backup_path), original)
    atomic_write_text(path, updated_text)

    return {
        "version": new_version,
        "added": added_ids,
        "skipped_duplicate": skipped_duplicate,
        "conflicts": conflicts,
        "appended": len(added_ids),
    }


# ---------------------------------------------------------------------- #
# 章节与规则行
# ---------------------------------------------------------------------- #
def _find_section(lines: list[str], pattern: re.Pattern[str]) -> tuple[int, int] | None:
    """返回 (标题行下标, 下一个 `## ` 标题行下标或 len)。"""
    start: int | None = None
    for index, line in enumerate(lines):
        if start is None:
            if pattern.match(line):
                start = index
            continue
        if _ANY_SECTION_RE.match(line):
            return start, index
    if start is None:
        return None
    return start, len(lines)


def _collect_rules(lines: Iterable[str]) -> list[tuple[str, str]]:
    rules: list[tuple[str, str]] = []
    for line in lines:
        match = _RULE_LINE_RE.match(line.strip())
        if match:
            rules.append((match.group(1), match.group(2).strip()))
    return rules


def _insert_rules(
    lines: list[str],
    reduced: list[tuple[str, str, str]],
    pattern: re.Pattern[str],
) -> list[str]:
    section = _find_section(lines, pattern)
    if section is None:  # pragma: no cover - 上游已检查
        return lines
    start, end = section
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    block = [line for _rid, _text, line in reduced] + [""]
    return lines[:insert_at] + block + lines[insert_at:]


def _append_change_record(
    lines: list[str],
    new_version: str,
    added_ids: list[str],
    source_req: str,
    pattern: re.Pattern[str],
) -> list[str]:
    """§5 变更记录按「最新在上」追加（docs/04 §2 模板口径）。"""
    section = _find_section(lines, pattern)
    if section is None:  # pragma: no cover - 上游已检查
        return lines
    start, end = section
    insert_at = start + 1
    while insert_at < end and not lines[insert_at].strip():
        insert_at += 1
    record = f"- {new_version} ← 回灌 {'、'.join(added_ids)}（来源：需求 {source_req}）"
    return lines[:insert_at] + [record] + lines[insert_at:]


def _update_header(lines: list[str], new_version: str) -> str:
    version_re = re.compile(r"版本：v\d+\.\d+")
    date_re = re.compile(r"更新：\d{4}-\d{2}-\d{2}")
    for index, line in enumerate(lines[:10]):
        if "版本：" in line:
            lines[index] = version_re.sub(f"版本：{new_version}", line)
            lines[index] = date_re.sub(f"更新：{now_iso()[:10]}", lines[index])
            break
    return "\n".join(lines)


# ---------------------------------------------------------------------- #
# 版本
# ---------------------------------------------------------------------- #
def _parse_version(text: str) -> tuple[int, int]:
    match = _VERSION_RE.search(text or "")
    if not match:
        match = re.search(r"v(\d+)\.(\d+)", text or "")
    if not match:
        return (0, 1)
    return int(match.group(1)), int(match.group(2))


def _format_version(version: tuple[int, int]) -> str:
    return f"v{version[0]}.{version[1]}"


def _bump_version(version: tuple[int, int]) -> str:
    return f"v{version[0]}.{version[1] + 1}"


# ---------------------------------------------------------------------- #
# 去重 / 冲突
# ---------------------------------------------------------------------- #
def _normalize(text: str) -> str:
    return _NOISE_RE.sub("", (text or "").lower())


def _similarity(left: str, right: str) -> float:
    left_norm = _normalize(left)
    right_norm = _normalize(right)
    if not left_norm or not right_norm:
        return 0.0
    return difflib.SequenceMatcher(None, left_norm, right_norm).ratio()


def _is_duplicate(text: str, candidates: Iterable[str]) -> bool:
    return any(_similarity(text, candidate) >= _DUPLICATE_THRESHOLD for candidate in candidates)


def _polarity(text: str) -> str:
    negative = any(marker in text for marker in _NEGATIVE_MARKERS)
    positive = any(marker in text for marker in _POSITIVE_MARKERS)
    if negative and not positive:
        return "negative"
    if positive and not negative:
        return "positive"
    return "mixed"


def _find_conflict(text: str, known: Iterable[tuple[str, str]]) -> tuple[str, float] | None:
    polarity = _polarity(text)
    for rule_id, candidate in known:
        if polarity not in {"negative", "positive"}:
            continue
        candidate_polarity = _polarity(candidate)
        if candidate_polarity not in {"negative", "positive"} or candidate_polarity == polarity:
            continue
        score = _similarity(text, candidate)
        if score >= _CONFLICT_THRESHOLD:
            return rule_id, score
    return None


def _proposal_suffix(proposal: dict) -> str:
    criteria = str(proposal.get("criteria") or "").strip()
    counterexample = str(proposal.get("counterexample") or "").strip()
    parts: list[str] = []
    if criteria:
        parts.append(f"判据：{criteria}")
    if counterexample:
        parts.append(f"反例：{counterexample}")
    return f"（{'；'.join(parts)}）" if parts else ""


# ---------------------------------------------------------------------- #
# 规则编号
# ---------------------------------------------------------------------- #
def _next_rule_number(existing_ids: set[str]) -> int:
    numbers = [int(match.group(1)) for rule_id in existing_ids if (match := _RULE_ID_RE.match(rule_id))]
    return max(numbers, default=0) + 1


def _next_free_id(used: set[str], start: int) -> str:
    number = start
    while f"R-{number:02d}" in used:
        number += 1
    return f"R-{number:02d}"