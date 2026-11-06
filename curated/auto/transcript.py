"""Reduce a Qoder session transcript to a small, redacted digest.

The digest is the only thing a judging model ever sees. Everything expensive or
sensitive stops here: tool inputs and results collapse to a name plus a status,
fenced code blocks are removed, and credential-shaped strings are replaced before
they can reach an LLM or a shared library.
"""
from __future__ import annotations

import hashlib
import json
import math
import pathlib
import re
from typing import Any, Dict, Iterable, List, Tuple

SELF_MARKER = "AUTO-CURATOR-RUN"
MAX_READ_BYTES = 4_000_000
MAX_DIGEST_CHARS = 8_000
HEAD_SCAN_BYTES = 262_144
HEAD_USER_CHARS = 500
TURN_CHARS = 700
SEGMENT_TURN_CHARS = 400
SEGMENT_MIN_CHARS = 3_000
SEGMENT_OVERHEAD = 800
MAX_SEGMENTS = 12
MAX_TOOL_EVENTS = 40

_FENCE_RE = re.compile(r"^\s*(```|~~~).*?^\s*\1[^\n]*$", re.MULTILINE | re.DOTALL)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
)
_HOME_RE = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")
_SECRET_RES = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|passwd|password)\b\s*[:=]\s*[\"']?[^\s\"',}]{6,}"),
    re.compile(r"(?i)\b(x-api-key|authorization)\b\s*:\s*\S+"),
    re.compile(r"https?://[^\s/:@]+:[^\s/@]+@"),
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _clean(text: str) -> str:
    text = _ANSI_RE.sub("", text)
    text = _FENCE_RE.sub("[code omitted]", text)
    text = _PRIVATE_KEY_RE.sub("[redacted private key]", text)
    text = _HOME_RE.sub("~", text)
    for pattern in _SECRET_RES:
        text = pattern.sub("[redacted]", text)
    return text.strip()


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _blocks(content: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                yield item
    elif isinstance(content, str):
        yield {"type": "text", "text": content}


def _read_lines(path: pathlib.Path, start: int, skip_first: bool) -> Tuple[List[Dict[str, Any]], int]:
    """Parse complete JSON lines from `start`, never advancing past a partial tail line."""
    entries: List[Dict[str, Any]] = []
    with path.open("rb") as handle:
        handle.seek(start)
        position = start
        first = True
        for raw in handle:
            end = position + len(raw)
            if first and skip_first:
                first = False
                position = end
                continue
            first = False
            if not raw.endswith(b"\n"):
                break
            position = end
            try:
                value = json.loads(raw)
            except ValueError:
                continue
            if isinstance(value, dict):
                entries.append(value)
    return entries, position


def read_objects(
    path: pathlib.Path, offset: int = 0, max_bytes: int = MAX_READ_BYTES
) -> Tuple[List[Dict[str, Any]], int]:
    """Return parsed entries and the offset of the last complete line consumed."""
    size = path.stat().st_size
    start = offset
    skip_first = False
    if size - start > max_bytes:
        start = size - max_bytes
        skip_first = True
    return _read_lines(path, start, skip_first)


def _first_user_text(path: pathlib.Path) -> Tuple[str, str]:
    """Session id and the first user text, which is where the task goal lives."""
    session_id = path.stem
    try:
        with path.open("rb") as handle:
            raw = handle.read(HEAD_SCAN_BYTES)
    except OSError:
        return session_id, ""
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        session_id = str(entry.get("sessionId") or session_id)
        if entry.get("type") != "user":
            continue
        for block in _blocks((entry.get("message") or {}).get("content")):
            if block.get("type") == "text" and str(block.get("text") or "").strip():
                return session_id, _clip(_clean(str(block["text"])), HEAD_USER_CHARS)
    return session_id, ""


def _workspace(entries: List[Dict[str, Any]]) -> str:
    for entry in entries:
        if entry.get("type") == "workspace-directories":
            dirs = entry.get("directories") or []
            if dirs:
                return pathlib.Path(str(dirs[0])).name
    for entry in entries:
        cwd = entry.get("cwd")
        if isinstance(cwd, str) and cwd:
            return pathlib.Path(cwd).name
    return ""


def _render(
    session_id: str,
    workspace: str,
    goal: str,
    events: List[str],
    evidence: str,
    turns: int,
    end_offset: int,
    self_generated: bool,
) -> Tuple[str, Dict[str, Any]]:
    header = f"session={session_id} workspace={workspace} evidence={evidence} turns={turns}"
    recent: List[str] = []
    budget = MAX_DIGEST_CHARS - len(header) - len(goal) - 32
    for event in reversed(events):
        if len(event) + 1 > budget:
            break
        recent.append(event)
        budget -= len(event) + 1
    recent.reverse()
    text = "\n".join([header, f"goal: {goal}", "recent:", *recent])[:MAX_DIGEST_CHARS]

    meta = {
        "session_id": session_id,
        "workspace": workspace,
        "evidence": evidence,
        "turns": turns,
        "end_offset": end_offset,
        "self_generated": self_generated,
        "digest_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
    }
    return text, meta


def digest(path: pathlib.Path, offset: int, max_bytes: int = MAX_READ_BYTES) -> Tuple[str, Dict[str, Any]]:
    """Build the redacted digest for the session content starting at `offset`."""
    entries, end_offset = read_objects(path, offset, max_bytes)
    session_id, goal = _first_user_text(path)
    workspace = _workspace(entries)

    tool_names: Dict[str, str] = {}
    events: List[str] = []
    turns = 0
    saw_error = False
    saw_success = False
    error_then_fixed = False
    self_generated = SELF_MARKER in goal
    tool_events = 0

    for entry in entries:
        kind = entry.get("type")
        if kind not in ("user", "assistant"):
            continue
        message = entry.get("message") or {}
        tool_result = entry.get("toolUseResult")
        if not isinstance(tool_result, dict):
            tool_result = {}
        for block in _blocks(message.get("content")):
            block_type = block.get("type")
            if block_type == "tool_use":
                tool_names[str(block.get("id") or "")] = str(block.get("name") or "tool")
                continue
            if block_type == "tool_result":
                failed = bool(block.get("is_error")) or bool(tool_result.get("isError"))
                if failed:
                    saw_error = True
                elif saw_error:
                    error_then_fixed = True
                if not failed:
                    saw_success = True
                if tool_events < MAX_TOOL_EVENTS:
                    name = tool_names.get(str(block.get("tool_use_id") or ""), "tool").strip()
                    events.append(f"TOOL {name} {'error' if failed else 'ok'}")
                    tool_events += 1
                continue
            if block_type != "text":
                continue
            text = _clip(_clean(str(block.get("text") or "")), TURN_CHARS)
            if not text:
                continue
            if SELF_MARKER in text:
                self_generated = True
            turns += 1
            events.append(("USER: " if kind == "user" else "ASSISTANT: ") + text)

    if error_then_fixed:
        evidence = "error_then_fixed"
    elif saw_success:
        evidence = "verified"
    else:
        evidence = "none"

    return _render(session_id, workspace, goal, events, evidence, turns, end_offset, self_generated)


def _segment_events(path: pathlib.Path) -> Tuple[str, str, str, List[Tuple[str, str]], int, bool]:
    """Whole-session text events, clipped, with tool results reduced to ok/error."""
    size = path.stat().st_size
    entries, end_offset = read_objects(path, 0, max_bytes=max(size, 1))
    session_id, goal = _first_user_text(path)
    workspace = _workspace(entries)
    events: List[Tuple[str, str]] = []
    self_generated = SELF_MARKER in goal

    for entry in entries:
        kind = entry.get("type")
        if kind not in ("user", "assistant"):
            continue
        message = entry.get("message") or {}
        tool_result = entry.get("toolUseResult")
        if not isinstance(tool_result, dict):
            tool_result = {}
        for block in _blocks(message.get("content")):
            block_type = block.get("type")
            if block_type == "tool_result":
                failed = bool(block.get("is_error")) or bool(tool_result.get("isError"))
                events.append(("tool", "error" if failed else "ok"))
                continue
            if block_type != "text":
                continue
            text = _clip(_clean(str(block.get("text") or "")), SEGMENT_TURN_CHARS)
            if not text:
                continue
            if SELF_MARKER in text:
                self_generated = True
            events.append((kind, text))
    return session_id, workspace, goal, events, end_offset, self_generated


def _split_events(events: List[Tuple[str, str]], count: int) -> List[List[Tuple[str, str]]]:
    """Split a session into chronological buckets of roughly equal text volume."""
    total = sum(len(text) for kind, text in events if kind != "tool")
    target = max(1.0, total / count)
    buckets: List[List[Tuple[str, str]]] = []
    current: List[Tuple[str, str]] = []
    size = 0
    for event in events:
        current.append(event)
        if event[0] != "tool":
            size += len(event[1])
        if size >= target and len(buckets) < count - 1:
            buckets.append(current)
            current = []
            size = 0
    if current:
        buckets.append(current)
    return buckets or [[]]


def _segment_digest(
    session_id: str,
    workspace: str,
    goal: str,
    bucket: List[Tuple[str, str]],
    index: int,
    total: int,
    end_offset: int,
    self_generated: bool,
) -> Tuple[str, Dict[str, Any]]:
    lines: List[str] = []
    turns = 0
    ok = 0
    failed = 0
    saw_error = False
    error_then_fixed = False
    for kind, text in bucket:
        if kind == "tool":
            if text == "error":
                failed += 1
                saw_error = True
            else:
                ok += 1
                if saw_error:
                    error_then_fixed = True
            continue
        turns += 1
        lines.append(("USER: " if kind == "user" else "ASSISTANT: ") + text)
    if ok or failed:
        lines.append(f"TOOLS ok={ok} error={failed}")

    if error_then_fixed:
        evidence = "error_then_fixed"
    elif ok:
        evidence = "verified"
    else:
        evidence = "none"

    header = (
        f"session={session_id} workspace={workspace} evidence={evidence} "
        f"turns={turns} segment={index}/{total}"
    )
    text = "\n".join([header, f"goal: {goal}", "events:", *lines])[:MAX_DIGEST_CHARS]
    meta = {
        "session_id": session_id,
        "workspace": workspace,
        "evidence": evidence,
        "turns": turns,
        "segment": index,
        "segments": total,
        "end_offset": end_offset,
        "self_generated": self_generated,
        "digest_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
    }
    return text, meta


def digest_segments(path: pathlib.Path, segments: int = 6) -> List[Tuple[str, Dict[str, Any]]]:
    """Split one session into several redacted digests covering its whole timeline.

    The tail digest answers "what happened last"; this answers "what did this long
    session cover". Each segment is small enough for a judging model to read.
    """
    session_id, workspace, goal, events, end_offset, self_generated = _segment_events(path)
    if not events:
        return []
    text_total = sum(len(text) for kind, text in events if kind != "tool")
    wanted = max(1, min(int(segments), MAX_SEGMENTS))
    if text_total:
        # Shrink a short session instead of emitting one-line segments, but grow past
        # the requested count when a wider window would truncate its own tail: a digest
        # that silently loses half its events is worse than one extra segment.
        wanted = min(wanted, max(1, math.ceil(text_total / SEGMENT_MIN_CHARS)))
        wanted = max(wanted, math.ceil(text_total / (MAX_DIGEST_CHARS - SEGMENT_OVERHEAD)))
        wanted = min(wanted, MAX_SEGMENTS)
    buckets = _split_events(events, wanted)
    return [
        _segment_digest(
            session_id, workspace, goal, bucket, index, len(buckets), end_offset, self_generated
        )
        for index, bucket in enumerate(buckets, 1)
    ]


def _inbox_head(path: pathlib.Path) -> Tuple[str, str, str]:
    """Session id, workspace and goal from the head of a CodeBuddy trajectory file."""
    session_id = path.stem
    workspace = ""
    goal = ""
    try:
        with path.open("rb") as handle:
            raw = handle.read(HEAD_SCAN_BYTES)
    except OSError:
        return session_id, workspace, goal
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        if kind == "meta":
            session_id = str(entry.get("session_id") or session_id)
            workspace = pathlib.Path(str(entry.get("workspace") or "")).name
        elif kind == "user" and not goal:
            goal = _clip(_clean(str(entry.get("text") or "")), HEAD_USER_CHARS)
    return session_id, workspace, goal


def digest_inbox(path: pathlib.Path, offset: int, max_bytes: int = MAX_READ_BYTES) -> Tuple[str, Dict[str, Any]]:
    """Digest a trajectory an agent dropped into the inbox.

    Line format: `meta`, `user`, `assistant`, `tool` (name/status) and `verification`.
    Same redaction rules as `digest`; the writer is trusted only to be well-formed,
    never to be safe.
    """
    entries, end_offset = read_objects(path, offset, max_bytes)
    session_id, workspace, goal = _inbox_head(path)

    events: List[str] = []
    turns = 0
    saw_error = False
    saw_success = False
    error_then_fixed = False
    self_generated = SELF_MARKER in goal
    tool_events = 0

    for entry in entries:
        kind = entry.get("kind")
        if kind in ("user", "assistant"):
            text = _clip(_clean(str(entry.get("text") or "")), TURN_CHARS)
            if not text:
                continue
            if SELF_MARKER in text:
                self_generated = True
            turns += 1
            events.append(("USER: " if kind == "user" else "ASSISTANT: ") + text)
        elif kind == "tool":
            failed = str(entry.get("status") or "").lower() == "error"
            if failed:
                saw_error = True
            else:
                if saw_error:
                    error_then_fixed = True
                saw_success = True
            if tool_events < MAX_TOOL_EVENTS:
                name = str(entry.get("name") or "tool").strip()
                events.append(f"TOOL {name} {'error' if failed else 'ok'}")
                tool_events += 1
        elif kind == "verification":
            text = _clip(_clean(str(entry.get("text") or "")), TURN_CHARS)
            if not text:
                continue
            if saw_error:
                error_then_fixed = True
            saw_success = True
            events.append("VERIFY: " + text)

    if error_then_fixed:
        evidence = "error_then_fixed"
    elif saw_success:
        evidence = "verified"
    else:
        evidence = "none"

    return _render(session_id, workspace, goal, events, evidence, turns, end_offset, self_generated)
