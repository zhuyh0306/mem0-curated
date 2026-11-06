"""Cursor state for the auto curator.

Offsets only advance after a session has been written or explicitly skipped, so a
failed gateway call simply replays the same digest next run. State is small, JSON,
and written atomically; a lock keeps two overlapping runs from double-writing.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import time
from typing import Any, Dict, Iterator, Optional

STATE_DIR = pathlib.Path.home() / ".qoder" / "curated"
CURSOR_NAME = "auto-cursor.json"
LOCK_NAME = "auto.lock"
IDLE_SECONDS = 300
STALE_LOCK_SECONDS = 45 * 60
SKIP_REASONS = (
    "no_reusable_experience",
    "duplicate",
    "insufficient_evidence",
    "sensitive",
    "one_off",
    "conflicts_with_curated_record",
)


class CursorError(RuntimeError):
    """Raised when cursor state cannot be read or written."""


class LockBusy(CursorError):
    """Raised when another run still holds the lock."""


class Cursor:
    def __init__(self, directory: Optional[pathlib.Path] = None) -> None:
        self.directory = pathlib.Path(directory or STATE_DIR)
        self.path = self.directory / CURSOR_NAME
        self._sessions: Dict[str, Dict[str, Any]] = {}

    def load(self) -> "Cursor":
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            self._sessions = {}
            return self
        except (OSError, ValueError):
            with contextlib.suppress(OSError):
                os.replace(self.path, self.path.with_suffix(".corrupt"))
            self._sessions = {}
            return self
        sessions = raw.get("sessions") if isinstance(raw, dict) else None
        self._sessions = sessions if isinstance(sessions, dict) else {}
        return self

    def save(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"version": 1, "sessions": self._sessions}, indent=2, sort_keys=True), "utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as exc:
            raise CursorError(f"cannot save cursor: {exc}") from exc

    def entry(self, key: str) -> Dict[str, Any]:
        return self._sessions.get(key, {})

    def start_offset(self, key: str, size: int) -> int:
        offset = int(self.entry(key).get("offset") or 0)
        # A shorter file means it was truncated or rewritten; replay it and let the
        # idempotency probe absorb anything already recorded.
        return 0 if offset > size else offset

    def seed_at_eof(self, key: str, size: int, mtime: float) -> None:
        self._sessions[key] = {"offset": size, "size": size, "mtime": mtime, "outcome": "seeded"}

    def set_pending(self, key: str, pending: Dict[str, Any]) -> None:
        record = dict(self.entry(key))
        record["pending"] = pending
        self._sessions[key] = record

    def commit(self, key: str, outcome: str, **extra: Any) -> None:
        record = dict(self.entry(key))
        pending = record.pop("pending", None)
        if not pending:
            raise CursorError(f"no pending scan for {key}")
        record.update(
            {
                "offset": pending.get("end_offset", record.get("offset", 0)),
                "size": pending.get("size"),
                "mtime": pending.get("mtime"),
                "session_id": pending.get("session_id"),
                "digest_hash": pending.get("digest_hash"),
                "outcome": outcome,
                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
        )
        record.update(extra)
        self._sessions[key] = record

    def should_scan(self, path: pathlib.Path) -> bool:
        try:
            stat = path.stat()
        except OSError:
            return False
        record = self.entry(str(path))
        if not record:
            return True
        if record.get("pending"):
            return True
        if int(record.get("offset") or 0) > stat.st_size:
            return True
        if record.get("size") == stat.st_size and record.get("mtime") == stat.st_mtime:
            return False
        # Still being appended to; the next run sees a stable size and picks it up.
        return time.time() - stat.st_mtime >= IDLE_SECONDS


@contextlib.contextmanager
def acquire_lock(directory: Optional[pathlib.Path] = None, stale_seconds: int = STALE_LOCK_SECONDS) -> Iterator[pathlib.Path]:
    folder = pathlib.Path(directory or STATE_DIR)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = folder / LOCK_NAME
    for _ in range(2):
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                continue
            if age <= stale_seconds:
                raise LockBusy(f"another run holds {lock} ({int(age)}s old)")
            with contextlib.suppress(OSError):
                lock.unlink()
            continue
        try:
            os.write(handle, str(os.getpid()).encode("ascii"))
        finally:
            os.close(handle)
        try:
            yield lock
        finally:
            with contextlib.suppress(OSError):
                lock.unlink()
        return
    raise LockBusy(f"could not acquire {lock}")
