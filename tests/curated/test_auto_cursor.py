import json
import os

import pytest

from curated.auto.cursor import Cursor, LockBusy, acquire_lock


def _transcript(path, size=10):
    path.write_bytes(b"x" * size)
    return path


def test_should_scan_only_when_content_changes(tmp_path):
    transcript = _transcript(tmp_path / "s1.jsonl")
    cursor = Cursor(tmp_path / "state").load()
    stat = transcript.stat()
    cursor.seed_at_eof(str(transcript), stat.st_size, stat.st_mtime)
    cursor.save()

    assert Cursor(tmp_path / "state").load().should_scan(transcript) is False

    with transcript.open("ab") as handle:
        handle.write(b"more")
    os.utime(transcript, (0, 0))

    assert Cursor(tmp_path / "state").load().should_scan(transcript) is True


def test_pending_forces_a_rescan(tmp_path):
    transcript = _transcript(tmp_path / "s1.jsonl")
    cursor = Cursor(tmp_path / "state").load()
    stat = transcript.stat()
    cursor.seed_at_eof(str(transcript), stat.st_size, stat.st_mtime)
    cursor.set_pending(str(transcript), {"end_offset": 10, "size": stat.st_size, "mtime": stat.st_mtime})
    cursor.save()

    assert Cursor(tmp_path / "state").load().should_scan(transcript) is True


def test_commit_advances_offset_and_clears_pending(tmp_path):
    transcript = _transcript(tmp_path / "s1.jsonl")
    cursor = Cursor(tmp_path / "state").load()
    cursor.set_pending(
        str(transcript),
        {"session_id": "s1", "digest_hash": "abc", "end_offset": 10, "size": 10, "mtime": 1.0},
    )
    cursor.commit(str(transcript), "written", id="mem-1")
    cursor.save()

    entry = Cursor(tmp_path / "state").load().entry(str(transcript))
    assert entry["offset"] == 10
    assert entry["outcome"] == "written"
    assert entry["id"] == "mem-1"
    assert "pending" not in entry


def test_start_offset_resets_when_file_shrank(tmp_path):
    transcript = _transcript(tmp_path / "s1.jsonl", size=4)
    cursor = Cursor(tmp_path / "state").load()
    cursor.seed_at_eof(str(transcript), 100, 1.0)

    assert cursor.start_offset(str(transcript), 4) == 0


def test_corrupt_state_is_moved_aside(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "auto-cursor.json").write_text("{not json", "utf-8")

    cursor = Cursor(state_dir).load()

    assert cursor.entry("anything") == {}
    assert (state_dir / "auto-cursor.corrupt").exists()


def test_lock_is_exclusive_and_released(tmp_path):
    with acquire_lock(tmp_path / "state"):
        with pytest.raises(LockBusy):
            with acquire_lock(tmp_path / "state"):
                pass
    with acquire_lock(tmp_path / "state"):
        pass


def test_stale_lock_is_taken_over(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    lock = state_dir / "auto.lock"
    lock.write_text("999999", "utf-8")
    os.utime(lock, (0, 0))

    with acquire_lock(state_dir, stale_seconds=60):
        assert lock.exists()


def test_state_file_keeps_scan_settings_separate(tmp_path):
    cursor = Cursor(tmp_path / "state").load()
    cursor.save()
    payload = json.loads((tmp_path / "state" / "auto-cursor.json").read_text("utf-8"))

    assert payload["version"] == 1
    assert payload["sessions"] == {}
    assert oct((tmp_path / "state" / "auto-cursor.json").stat().st_mode & 0o777) == oct(0o600)
