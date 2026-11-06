"""Command line entry point for the auto curator.

`scan` hands session digests to a judging model, `write` applies every deterministic
gate and sends one record to the gateway, `mark` records an explicit skip. Only this
process holds a curator key; worker sessions stay read-only.

Exit codes: 0 done, 2 gateway/config unreachable (retry next run), 3 gate rejection,
4 local state error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from curated.auto import gates
from curated.auto.cursor import STATE_DIR, SKIP_REASONS, Cursor, CursorError, LockBusy, acquire_lock
from curated.auto.transcript import MAX_READ_BYTES, digest, digest_inbox, digest_segments

EXIT_OK = 0
EXIT_NETWORK = 2
EXIT_REJECTED = 3
EXIT_STATE = 4

AUTO_TAGS = ("auto", "unreviewed")
MAX_RECORDS_PER_CANDIDATE = 4
CONFIG_NAME = "auto-config.json"
AUDIT_NAME = "auto-log.jsonl"
PROJECTS_ROOT = pathlib.Path.home() / ".qoder" / "projects"
INBOX_ROOT = pathlib.Path.home() / ".codebuddy" / "curated-inbox"
INBOX_ROOT_DOUBAO = pathlib.Path.home() / ".doubao" / "curated-inbox"
SETTINGS_PATH = pathlib.Path.home() / ".qoder" / "settings.json"
DEFAULT_CONFIG: Dict[str, Any] = {"exclude_workspaces": [], "max_per_run": 2, "max_read_bytes": MAX_READ_BYTES}


class ConfigError(RuntimeError):
    """Raised when the gateway coordinates are missing."""


class Config:
    def __init__(self, gateway: str, api_key: str, project_id: str, state_dir: pathlib.Path, scan: Dict[str, Any]):
        self.gateway = gateway.rstrip("/")
        self.api_key = api_key
        self.project_id = project_id
        self.state_dir = state_dir
        self.scan = scan


def _settings_env() -> Dict[str, str]:
    path = pathlib.Path(os.environ.get("QODER_SETTINGS", str(SETTINGS_PATH)))
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    env = ((servers or {}).get("curated-curator") or {}).get("env")
    if not isinstance(env, dict):
        return {}
    return {key: value for key, value in env.items() if isinstance(value, str)}


def load_config(state_dir: Optional[pathlib.Path] = None) -> Config:
    fallback = _settings_env()
    gateway = os.environ.get("CURATED_GATEWAY_URL") or fallback.get("CURATED_GATEWAY_URL") or "http://127.0.0.1:8787"
    api_key = os.environ.get("CURATED_API_KEY") or fallback.get("CURATED_API_KEY") or ""
    project_id = os.environ.get("CURATED_PROJECT_ID") or fallback.get("CURATED_PROJECT_ID") or ""
    if not api_key or not project_id:
        raise ConfigError("CURATED_API_KEY and CURATED_PROJECT_ID must come from the environment or the curator connector")
    folder = pathlib.Path(state_dir or os.environ.get("CURATED_AUTO_STATE") or STATE_DIR)
    scan = dict(DEFAULT_CONFIG)
    try:
        overrides = json.loads((folder / CONFIG_NAME).read_text("utf-8"))
        if isinstance(overrides, dict):
            scan.update(overrides)
    except (OSError, ValueError):
        pass
    return Config(gateway, api_key, project_id, folder, scan)


def _client(config: Config, transport: Optional[httpx.BaseTransport] = None) -> httpx.Client:
    return httpx.Client(
        base_url=config.gateway,
        headers={"X-API-Key": config.api_key},
        timeout=60.0,
        transport=transport,
    )


def _audit(config: Config, payload: Dict[str, Any]) -> None:
    try:
        config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = config.state_dir / AUDIT_NAME
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        os.chmod(target, 0o600)
    except OSError:
        pass


def _workspace_excluded(name: str, scan: Dict[str, Any]) -> bool:
    entries = scan.get("exclude_workspaces") or []
    return any(isinstance(item, str) and item and item in str(name) for item in entries)


def _excluded(path: pathlib.Path, scan: Dict[str, Any]) -> bool:
    return _workspace_excluded(path.parent.name, scan)


def _inbox_roots(scan: Dict[str, Any]) -> List[pathlib.Path]:
    configured = scan.get("inbox_dir")
    if not configured:
        return [INBOX_ROOT, INBOX_ROOT_DOUBAO]
    items = configured if isinstance(configured, list) else [configured]
    return [pathlib.Path(str(item)).expanduser() for item in items if item]


def discover(config: Config) -> List[Tuple[pathlib.Path, str]]:
    """Qoder transcripts plus trajectories agents dropped into a local inbox."""
    found: List[Tuple[pathlib.Path, str]] = []
    if PROJECTS_ROOT.exists():
        found.extend((path, "qoder") for path in PROJECTS_ROOT.glob("*/*.jsonl") if path.is_file())
    for inbox in _inbox_roots(config.scan):
        if inbox.exists():
            found.extend((path, "inbox") for path in inbox.glob("*.jsonl") if path.is_file())
    return sorted(found, key=lambda item: item[0].stat().st_mtime, reverse=True)


def scan(
    config: Config,
    limit: Optional[int] = None,
    backfill: bool = False,
    backfill_days: Optional[int] = None,
) -> Dict[str, Any]:
    state = Cursor(config.state_dir)
    first_run = not state.path.exists()
    state.load()
    max_bytes = int(config.scan.get("max_read_bytes") or MAX_READ_BYTES)
    budget = int(limit if limit is not None else config.scan.get("max_per_run") or 2)
    cutoff = time.time() - backfill_days * 86400 if backfill_days else None
    candidates: List[Dict[str, Any]] = []
    with acquire_lock(config.state_dir):
        for path, source in discover(config):
            if budget and len(candidates) >= budget:
                break
            if source == "qoder" and _excluded(path, config.scan):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            key = str(path)
            seen = state.entry(key)
            if cutoff is not None and stat.st_mtime < cutoff:
                # Outside the replay window: seed it if needed, never touch existing state.
                if not seen:
                    state.seed_at_eof(key, stat.st_size, stat.st_mtime)
                continue
            if backfill and seen.get("pending"):
                # Backfill drains the seeded backlog; pending candidates belong to the normal scan.
                continue
            if not seen and first_run and not backfill:
                # First run only seeds the backlog; replay it deliberately with --backfill.
                state.seed_at_eof(key, stat.st_size, stat.st_mtime)
                continue
            replay = backfill and (not seen or seen.get("outcome") == "seeded")
            if not replay:
                if seen and not state.should_scan(path):
                    continue
                start = state.start_offset(key, stat.st_size)
                # A backfilled candidate was read from the head of a session whose
                # committed offset already sits at EOF; re-reading from the offset
                # would yield an empty digest and silently drop the pending record.
                start = int((seen.get("pending") or {}).get("start_offset", start))
            else:
                start = 0
            reader = digest if source == "qoder" else digest_inbox
            text, meta = reader(path, start, max_bytes)
            if source == "inbox" and _workspace_excluded(meta["workspace"], config.scan):
                state.seed_at_eof(key, stat.st_size, stat.st_mtime)
                state.save()
                continue
            pending = {
                "session_id": meta["session_id"],
                "digest_hash": meta["digest_hash"],
                "start_offset": start,
                "end_offset": meta["end_offset"],
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "evidence": meta["evidence"],
            }
            if meta["self_generated"] or meta["turns"] < 2:
                state.seed_at_eof(key, stat.st_size, stat.st_mtime)
                state.save()
                continue
            state.set_pending(key, pending)
            candidates.append(
                {
                    "key": key,
                    "source": source,
                    "session_id": meta["session_id"],
                    "workspace": meta["workspace"],
                    "evidence": meta["evidence"],
                    "turns": meta["turns"],
                    "digest_hash": meta["digest_hash"],
                    "digest": text,
                }
            )
        state.save()
    return {"candidates": candidates, "count": len(candidates)}


def _resolve_session(session: str) -> pathlib.Path:
    candidate = pathlib.Path(session).expanduser()
    if candidate.is_file():
        return candidate
    matches: List[pathlib.Path] = []
    for root in (PROJECTS_ROOT, INBOX_ROOT, INBOX_ROOT_DOUBAO):
        if not root.exists():
            continue
        matches.extend(path for path in root.glob(f"*/{session}*.jsonl") if path.is_file())
        matches.extend(path for path in root.glob(f"{session}*.jsonl") if path.is_file())
    if not matches:
        raise CursorError(f"no transcript matches {session}")
    return sorted(matches, key=lambda path: path.stat().st_mtime, reverse=True)[0]


def _session_source(config: Config, path: pathlib.Path) -> str:
    for root in _inbox_roots(config.scan):
        try:
            path.relative_to(root)
            return "inbox"
        except ValueError:
            continue
    return "qoder"


def scan_session(config: Config, session: str, segments: int = 6, limit: Optional[int] = None) -> Dict[str, Any]:
    """Split one named session into several candidates so a long session can yield
    more than its final tail. Keys are virtual (`<path>::seg-N`) and never collide
    with the normal per-file cursor."""
    path = _resolve_session(session)
    source = _session_source(config, path)
    if source != "qoder":
        raise CursorError("segmented scan supports Qoder transcripts only; inbox trajectories are already single-session files")
    if _excluded(path, config.scan):
        return {"candidates": [], "count": 0}
    parts = digest_segments(path, segments)
    stat = path.stat()
    state = Cursor(config.state_dir).load()
    budget = limit if limit is not None else len(parts)
    candidates: List[Dict[str, Any]] = []
    with acquire_lock(config.state_dir):
        for index, (text, meta) in enumerate(parts, 1):
            if budget and len(candidates) >= budget:
                break
            if _workspace_excluded(meta["workspace"], config.scan):
                continue
            if meta["self_generated"] or meta["turns"] < 2:
                continue
            key = f"{path}::seg-{index}"
            previous = state.entry(key).get("pending") or {}
            # Re-scanning the same digest must not forget which records already came
            # out of it, or the next write would duplicate them.
            written = dict(previous.get("written") or {}) if previous.get("digest_hash") == meta["digest_hash"] else {}
            state.set_pending(
                key,
                {
                    "session_id": meta["session_id"],
                    "digest_hash": meta["digest_hash"],
                    "start_offset": 0,
                    "end_offset": meta["end_offset"],
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "evidence": meta["evidence"],
                    "segment": index,
                    "written": written,
                },
            )
            candidates.append(
                {
                    "key": key,
                    "source": source,
                    "session_id": meta["session_id"],
                    "workspace": meta["workspace"],
                    "evidence": meta["evidence"],
                    "turns": meta["turns"],
                    "segment": f"{index}/{meta['segments']}",
                    "digest_hash": meta["digest_hash"],
                    "digest": text,
                }
            )
        state.save()
    return {"candidates": candidates, "count": len(candidates)}


def _prepare(record: Dict[str, Any], evidence: str) -> Dict[str, Any]:
    prepared = dict(record)
    prepared.pop("supersedes", None)
    raw_tags = record.get("tags") if isinstance(record.get("tags"), list) else []
    prepared["tags"] = gates.normalize_tags([*AUTO_TAGS, *raw_tags])
    prepared["confidence"] = gates.cap_confidence(prepared, evidence)
    prepared.setdefault("memory_type", "experience")
    return prepared


def _finish(
    config: Config,
    key: str,
    record: Dict[str, Any],
    outcome: str,
    keep: bool = False,
    record_key: str = "",
    **extra: Any,
) -> Dict[str, Any]:
    state = Cursor(config.state_dir).load()
    pending = state.entry(key).get("pending")
    if pending:
        if keep:
            # One transcript can hold several unrelated lessons. Keep the candidate
            # open until the judge finalizes it, and remember which records already
            # came out of this digest so a retry cannot duplicate them.
            stored = dict(pending.get("written") or {})
            if record_key and extra.get("id"):
                stored[record_key] = extra["id"]
            updated = dict(pending)
            updated["written"] = stored
            state.set_pending(key, updated)
        else:
            state.commit(key, outcome, **extra)
        state.save()
    _audit(
        config,
        {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "key": key,
            "outcome": outcome,
            "project_id": config.project_id,
            "record_key": record_key or None,
            **extra,
        },
    )
    result = {"outcome": outcome, **extra}
    if record_key:
        result["record_key"] = record_key
    return result


def _post(client: httpx.Client, path: str, payload: Dict[str, Any]) -> Any:
    response = client.post(path, json=payload)
    response.raise_for_status()
    return response.json()


def write_record(
    config: Config,
    key: str,
    record: Dict[str, Any],
    transport: Optional[httpx.BaseTransport] = None,
    keep: bool = False,
) -> Dict[str, Any]:
    state = Cursor(config.state_dir).load()
    pending = state.entry(key).get("pending")
    if not pending:
        raise CursorError(f"no pending scan for {key}; run scan first")
    evidence = str(pending.get("evidence") or "none")
    session_id = str(pending.get("session_id") or "")
    prepared = _prepare(record, evidence)
    record_key = hashlib.sha256(prepared["summary"].strip().encode("utf-8")).hexdigest()[:8]
    prepared["source_agent"] = f"auto:{session_id}:{pending.get('digest_hash', '')[:8]}:{record_key}"
    result = {"source_agent": prepared["source_agent"]}

    already = (pending.get("written") or {}).get(record_key)
    if already:
        return {"outcome": "already_written", "id": already, "record_key": record_key, **result}
    if keep and len(pending.get("written") or {}) >= MAX_RECORDS_PER_CANDIDATE:
        return {
            "outcome": "cap_reached",
            "detail": f"at most {MAX_RECORDS_PER_CANDIDATE} records per candidate; run finalize --key",
            "record_key": record_key,
            **result,
        }

    problems = gates.shape_errors(prepared)
    if not problems:
        problems = [f"secret: {label}" for label in gates.secret_hits(prepared)]
    if not problems:
        problems = [f"code: {label}" for label in gates.code_smells(prepared)]
    if problems:
        return _finish(config, key, prepared, "rejected", keep=keep, record_key=record_key, reasons=problems)

    with _client(config, transport) as client:
        try:
            existing = client.get(
                "/v1/experiences",
                params={
                    "project_id": config.project_id,
                    "tags": "auto",
                    "include_deprecated": "true",
                    "limit": 1000,
                },
            )
            existing.raise_for_status()
            for item in existing.json():
                source = ((item.get("record") or {}).get("source_agent") or "")
                # Exact digest plus record hash: re-judging the same tail is blocked,
                # while one digest can still produce several distinct records.
                if source == prepared["source_agent"]:
                    return _finish(
                        config, key, prepared, "already_written", keep=keep, record_key=record_key,
                        id=item.get("id"), **result,
                    )

            candidates = _post(
                client,
                "/v1/experiences/dedup-check",
                {
                    "query": f"{prepared['summary']} {prepared.get('task_goal', '')}".strip(),
                    "project_id": config.project_id,
                    "tags": [],
                    "top_k": 5,
                    "threshold": 0.75,
                },
            )
            best = max(candidates, key=lambda item: item.get("score") or 0.0) if candidates else None
            if best and (best.get("score") or 0.0) >= 0.9:
                return _finish(
                    config, key, prepared, "duplicate", keep=keep, record_key=record_key,
                    id=best.get("id"), **result,
                )
            near_duplicate = None
            if best and (best.get("score") or 0.0) >= 0.75:
                if "auto" not in (best.get("tags") or []):
                    return _finish(
                        config, key, prepared, "conflicts_with_curated_record", keep=keep,
                        record_key=record_key, id=best.get("id"), score=best.get("score"), **result,
                    )
                # Similarity is not equivalence: a 0.75-0.9 match is related, not superseded.
                near_duplicate = best.get("id")

            created = _post(client, "/v1/experiences", {"project_id": config.project_id, "record": prepared})
            return _finish(
                config, key, prepared, "written", keep=keep, record_key=record_key,
                id=created.get("id"), near_duplicate=near_duplicate, **result,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 422:
                raise
            # A schema rejection is deterministic for this digest; do not retry it forever.
            reason = f"server-422: {exc.response.text[:300]}"
            return _finish(config, key, prepared, "rejected", keep=keep, record_key=record_key, reasons=[reason])


def finalize_record(config: Config, key: str, reason: str = "no_reusable_experience") -> Dict[str, Any]:
    """Close a candidate that `write --keep` left open, advancing the cursor once."""
    if reason not in SKIP_REASONS:
        raise CursorError(f"reason must be one of {', '.join(SKIP_REASONS)}")
    state = Cursor(config.state_dir).load()
    pending = state.entry(key).get("pending")
    if not pending:
        raise CursorError(f"no pending scan for {key}; run scan first")
    written = pending.get("written") or {}
    records = sorted(set(written.values()))
    if records:
        state.commit(key, "finalized", records=records)
    else:
        state.commit(key, "skipped", reason=reason)
    state.save()
    result = {"outcome": "finalized", "key": key, "records": records, "count": len(records)}
    _audit(
        config,
        {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "project_id": config.project_id,
            **result,
        },
    )
    return result


def mark_record(config: Config, key: str, reason: str) -> Dict[str, Any]:
    if reason not in SKIP_REASONS:
        raise CursorError(f"reason must be one of {', '.join(SKIP_REASONS)}")
    state = Cursor(config.state_dir).load()
    if not state.entry(key).get("pending"):
        raise CursorError(f"no pending scan for {key}; run scan first")
    state.commit(key, "skipped", reason=reason)
    state.save()
    _audit(config, {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "key": key, "outcome": "skipped", "reason": reason})
    return {"outcome": "skipped", "reason": reason}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="curated-auto", description="Automatic experience curation")
    parser.add_argument("--state-dir", help="override the state directory (defaults to ~/.qoder/curated)")
    sub = parser.add_subparsers(dest="command", required=True)

    scan_parser = sub.add_parser("scan", help="emit digests for sessions that changed")
    scan_parser.add_argument("--limit", type=int, help="maximum candidates to emit")
    scan_parser.add_argument("--backfill", action="store_true", help="replay every session instead of seeding the backlog")
    scan_parser.add_argument(
        "--backfill-days",
        type=int,
        metavar="N",
        help="only replay sessions modified in the last N days; older unseen ones are left seeded",
    )
    scan_parser.add_argument(
        "--session",
        help="split one named session (path or id prefix) into segments instead of scanning changed files",
    )
    scan_parser.add_argument("--segments", type=int, default=6, help="maximum segments for --session (default 6)")

    write_parser = sub.add_parser("write", help="validate and store one record read from stdin")
    write_parser.add_argument("--key", required=True, help="transcript path reported by scan")
    write_parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the candidate open so more records can come from the same digest",
    )

    finalize_parser = sub.add_parser("finalize", help="close a candidate kept open by write --keep")
    finalize_parser.add_argument("--key", required=True)
    finalize_parser.add_argument("--reason", default="no_reusable_experience", choices=SKIP_REASONS)

    mark_parser = sub.add_parser("mark", help="record a skip and advance the cursor")
    mark_parser.add_argument("--key", required=True)
    mark_parser.add_argument("--reason", required=True, choices=SKIP_REASONS)
    return parser


def main(argv: Optional[List[str]] = None, transport: Optional[httpx.BaseTransport] = None) -> int:
    args = build_parser().parse_args(argv)
    state_dir = pathlib.Path(args.state_dir) if args.state_dir else None
    try:
        config = load_config(state_dir)
    except ConfigError as exc:
        print(json.dumps({"outcome": "config_error", "detail": str(exc)}, ensure_ascii=False))
        return EXIT_NETWORK
    try:
        if args.command == "scan":
            payload = (
                scan_session(config, args.session, args.segments, args.limit)
                if args.session
                else scan(config, args.limit, args.backfill, args.backfill_days)
            )
            print(json.dumps(payload, ensure_ascii=False))
        elif args.command == "write":
            record = json.load(sys.stdin)
            print(json.dumps(write_record(config, args.key, record, transport, keep=args.keep), ensure_ascii=False))
        elif args.command == "finalize":
            print(json.dumps(finalize_record(config, args.key, args.reason), ensure_ascii=False))
        else:
            print(json.dumps(mark_record(config, args.key, args.reason), ensure_ascii=False))
    except LockBusy as exc:
        print(json.dumps({"outcome": "busy", "detail": str(exc)}, ensure_ascii=False))
        return EXIT_OK
    except CursorError as exc:
        print(json.dumps({"outcome": "state_error", "detail": str(exc)}, ensure_ascii=False))
        return EXIT_STATE
    except (httpx.HTTPError, OSError) as exc:
        print(json.dumps({"outcome": "unavailable", "detail": exc.__class__.__name__}, ensure_ascii=False))
        return EXIT_NETWORK
    except json.JSONDecodeError as exc:
        print(json.dumps({"outcome": "invalid_json", "detail": str(exc)}, ensure_ascii=False))
        return EXIT_REJECTED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
