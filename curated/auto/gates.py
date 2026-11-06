"""Deterministic gates applied before an automatically judged record is written.

These are deliberately blunt: an LLM decides whether a session is worth keeping,
but only this module decides whether the record may leave the machine. Reasons are
labels, never the matched text, so a rejected secret is not echoed into a log.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterator, List, Tuple

REQUIRED_FIELDS = ("summary", "task_goal", "applicable_scope", "exclude_scope", "source_agent")
MAX_FIELD_CHARS = 1500
CONFIDENCE_CAPS = {"none": 0.5, "error_then_fixed": 0.65, "verified": 0.8}
CODE_LINE_RE = re.compile(r"^(def |class |function |import |from \S+ import |//|#!|<\?php|\{)")
SECRET_PATTERNS = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "provider-api-key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "github-token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws-access-key"),
    (
        re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|passwd|password)\b\s*[:=]\s*[\"']?[^\s\"',}]{6,}"),
        "inline-credential",
    ),
    (re.compile(r"(?i)\b(x-api-key|authorization)\b\s*:\s*\S+"), "auth-header"),
    (re.compile(r"https?://[^\s/:@]+:[^\s/@]+@"), "url-credential"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private-key"),
)


def iter_strings(value: Any, path: str = "record") -> Iterator[Tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from iter_strings(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from iter_strings(item, f"{path}[{index}]")


def normalize_tags(tags: Any) -> List[str]:
    if not isinstance(tags, (list, tuple)):
        return []
    seen: List[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            continue
        value = tag.strip().lower()
        if value and value not in seen:
            seen.append(value)
    return seen


def shape_errors(record: Any) -> List[str]:
    if not isinstance(record, dict):
        return ["record must be a JSON object"]
    errors: List[str] = []
    for field in REQUIRED_FIELDS:
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field} must be a non-empty string")
    if not normalize_tags(record.get("tags")):
        errors.append("tags must be a non-empty list")
    confidence = record.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0.0 <= float(confidence) <= 1.0:
        errors.append("confidence must be a number between 0 and 1")
    if record.get("memory_type") not in (None, "experience"):
        errors.append("memory_type must be 'experience'")
    solution = record.get("successful_solution")
    failures = record.get("failed_attempts")
    if solution is not None and not isinstance(solution, dict):
        errors.append("successful_solution must be an object")
    if failures is not None and not isinstance(failures, list):
        errors.append("failed_attempts must be a list")
    if not isinstance(solution, dict) or not solution.get("steps"):
        if not failures:
            errors.append("record needs successful_solution.steps or failed_attempts")
    return errors


def secret_hits(record: Any) -> List[str]:
    labels: List[str] = []
    for _, text in iter_strings(record):
        for pattern, label in SECRET_PATTERNS:
            if pattern.search(text) and label not in labels:
                labels.append(label)
    return labels


def code_smells(record: Any) -> List[str]:
    reasons: List[str] = []
    for path, text in iter_strings(record):
        if "```" in text or "~~~" in text:
            reasons.append(f"{path}: fenced code block")
        if len(text) > MAX_FIELD_CHARS:
            reasons.append(f"{path}: longer than {MAX_FIELD_CHARS} characters")
        indented = 0
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                indented = 0
                continue
            indented = indented + 1 if line.startswith(("  ", "\t")) else 0
            if indented >= 3:
                reasons.append(f"{path}: indented code block")
                break
            if CODE_LINE_RE.match(stripped):
                reasons.append(f"{path}: source line")
                break
    return list(dict.fromkeys(reasons))


def cap_confidence(record: Dict[str, Any], evidence: str) -> float:
    cap = CONFIDENCE_CAPS.get(evidence, CONFIDENCE_CAPS["none"])
    try:
        value = float(record.get("confidence"))
    except (TypeError, ValueError):
        return cap
    return round(min(max(value, 0.0), cap), 2)
