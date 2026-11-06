import hashlib
import io
import json
import os
import pathlib
import time

import httpx
import pytest

from curated.auto import cli
from curated.auto.cursor import Cursor

RECORD = {
    "summary": "受限网络里把依赖镜像一并推入 GHCR",
    "task_goal": "在只能访问一个 registry 的主机上完成部署",
    "constraints": ["服务器只通 ghcr.io"],
    "successful_solution": {"steps": ["CI 镜像依赖", "服务器只拉取"], "verification": "health 返回 200"},
    "failed_attempts": [{"try_content": "直接 pull", "root_cause": "Docker Hub 超时", "lesson": "先验证 registry 可达"}],
    "applicable_scope": "只能访问单一镜像仓库的云主机",
    "exclude_scope": "能直连 Docker Hub 的环境",
    "tags": ["Docker", "GHCR"],
    "confidence": 0.99,
}


class Gateway:
    def __init__(self, existing=None, dedup=None, fail=None):
        self.existing = existing or []
        self.dedup = dedup or []
        self.fail = fail
        self.calls = []
        self.created = 0

    def __call__(self, request):
        self.calls.append((request.method, request.url.path, request.content, str(request.url)))
        if self.fail:
            raise self.fail
        if request.method == "GET":
            return httpx.Response(200, json=self.existing)
        if request.url.path.endswith("/dedup-check"):
            return httpx.Response(200, json=self.dedup)
        self.created += 1
        return httpx.Response(200, json={"id": f"mem-{self.created}", "supersedes": "", "deprecated_target": ""})


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CURATED_GATEWAY_URL", "http://gateway.test")
    monkeypatch.setenv("CURATED_API_KEY", "curator-key")
    monkeypatch.setenv("CURATED_PROJECT_ID", "proj")
    monkeypatch.setenv("CURATED_AUTO_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("QODER_SETTINGS", str(tmp_path / "no-settings.json"))
    projects = tmp_path / "projects"
    monkeypatch.setattr(cli, "PROJECTS_ROOT", projects)
    inbox = tmp_path / "inbox"
    doubao_inbox = tmp_path / "doubao-inbox"
    monkeypatch.setattr(cli, "INBOX_ROOT", inbox)
    monkeypatch.setattr(cli, "INBOX_ROOT_DOUBAO", doubao_inbox)
    return {
        "tmp": tmp_path,
        "state": tmp_path / "state",
        "projects": projects,
        "inbox": inbox,
        "doubao_inbox": doubao_inbox,
    }


def _transcript(env, text="第一轮：排查部署失败", assistant="结论：镜像源不可达", workspace="demo", name="s1", age_days=0):
    folder = env["projects"] / f"-Users-x-{workspace}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.jsonl"
    entries = [
        {"type": "workspace-directories", "sessionId": name, "directories": [f"/work/{workspace}"]},
        {"type": "user", "sessionId": name, "message": {"role": "user", "content": [{"type": "text", "text": text}]}},
        {"type": "assistant", "sessionId": name, "message": {"role": "assistant", "content": [{"type": "text", "text": assistant}]}},
    ]
    path.write_text("".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries), "utf-8")
    old = time.time() - 600 - age_days * 86400
    os.utime(path, (old, old))
    return path


def _trajectory(env, workspace="demo", name="conv1", text="排查部署失败", assistant="结论：镜像源不可达", inbox="inbox"):
    folder = env[inbox]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.jsonl"
    entries = [
        {"kind": "meta", "session_id": name, "workspace": f"/work/{workspace}"},
        {"kind": "user", "text": text},
        {"kind": "tool", "name": "Bash", "status": "ok"},
        {"kind": "assistant", "text": assistant},
        {"kind": "verification", "text": "health 返回 200"},
    ]
    path.write_text("".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries), "utf-8")
    old = time.time() - 600
    os.utime(path, (old, old))
    return path


def _pending(env, path, evidence="verified"):
    stat = path.stat()
    cursor = Cursor(pathlib.Path(env["state"])).load()
    cursor.set_pending(
        str(path),
        {
            "session_id": "s1",
            "digest_hash": "abcdef0123456789",
            "end_offset": stat.st_size,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "evidence": evidence,
        },
    )
    cursor.save()


def _run(monkeypatch, args, record=None, transport=None):
    if record is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(record, ensure_ascii=False)))
    if transport is not None and not isinstance(transport, httpx.BaseTransport):
        transport = httpx.MockTransport(transport)
    return cli.main(args, transport=transport)


def test_first_scan_seeds_and_backfill_replays(env):
    path = _transcript(env)
    config = cli.load_config()

    seeded = cli.scan(config, limit=1)
    assert seeded["count"] == 0
    assert Cursor(pathlib.Path(env["state"])).load().entry(str(path))["outcome"] == "seeded"

    replayed = cli.scan(config, limit=1, backfill=True)
    assert replayed["count"] == 1
    assert "排查部署失败" in replayed["candidates"][0]["digest"]


def test_backfill_days_leaves_older_sessions_retired(env):
    fresh = _transcript(env, name="recent", age_days=2)
    old = _transcript(env, name="old", age_days=9)
    config = cli.load_config()

    cli.scan(config, limit=1)
    result = cli.scan(config, limit=1, backfill=True, backfill_days=7)

    assert result["count"] == 1
    assert result["candidates"][0]["key"] == str(fresh)
    cursor = Cursor(pathlib.Path(env["state"])).load()
    assert cursor.entry(str(old))["outcome"] == "seeded"
    assert cursor.entry(str(fresh))["pending"]["session_id"] == "recent"


def test_backfill_skips_pending_but_scan_repeats_it(env):
    _transcript(env)
    config = cli.load_config()
    cli.scan(config, limit=1, backfill=True)

    assert cli.scan(config, limit=1, backfill=True)["count"] == 0
    assert cli.scan(config, limit=1)["count"] == 1


def test_scan_repeats_a_pending_candidate_that_was_seeded_first(env):
    _transcript(env)
    config = cli.load_config()

    assert cli.scan(config, limit=1)["count"] == 0
    assert cli.scan(config, limit=1, backfill=True)["count"] == 1
    assert cli.scan(config, limit=1, backfill=True)["count"] == 0
    assert cli.scan(config, limit=1)["count"] == 1


def test_scan_skips_its_own_session(env):
    _transcript(env, text="AUTO-CURATOR-RUN 执行作业手册")
    config = cli.load_config()

    assert cli.scan(config, limit=1, backfill=True)["count"] == 0


def test_scan_respects_excluded_workspaces(env):
    _transcript(env, workspace="secret-repo")
    (pathlib.Path(env["state"])).mkdir(parents=True, exist_ok=True)
    (pathlib.Path(env["state"]) / "auto-config.json").write_text(json.dumps({"exclude_workspaces": ["secret"]}), "utf-8")

    assert cli.scan(cli.load_config(), limit=1, backfill=True)["count"] == 0


def test_scan_reads_inbox_trajectories(env):
    path = _trajectory(env)
    config = cli.load_config()

    result = cli.scan(config, limit=1, backfill=True)

    assert result["count"] == 1
    candidate = result["candidates"][0]
    assert candidate["source"] == "inbox"
    assert candidate["session_id"] == "conv1"
    assert candidate["workspace"] == "demo"
    assert candidate["evidence"] == "verified"
    assert "排查部署失败" in candidate["digest"]
    assert Cursor(pathlib.Path(env["state"])).load().entry(str(path))["pending"]["session_id"] == "conv1"


def test_scan_excludes_inbox_workspaces_by_name(env):
    _trajectory(env, workspace="secret-repo")
    (pathlib.Path(env["state"])).mkdir(parents=True, exist_ok=True)
    (pathlib.Path(env["state"]) / "auto-config.json").write_text(json.dumps({"exclude_workspaces": ["secret"]}), "utf-8")

    assert cli.scan(cli.load_config(), limit=1, backfill=True)["count"] == 0


def test_scan_reads_the_doubao_inbox(env):
    path = _trajectory(env, name="db1", inbox="doubao_inbox")

    result = cli.scan(cli.load_config(), limit=1, backfill=True)

    assert result["count"] == 1
    assert result["candidates"][0]["key"] == str(path)
    assert result["candidates"][0]["source"] == "inbox"


def test_scan_accepts_a_list_of_inbox_dirs(env):
    path = _trajectory(env, name="db2", inbox="doubao_inbox")
    state = pathlib.Path(env["state"])
    state.mkdir(parents=True, exist_ok=True)
    (state / "auto-config.json").write_text(
        json.dumps({"inbox_dir": [str(env["inbox"]), str(env["doubao_inbox"])]}), "utf-8"
    )

    result = cli.scan(cli.load_config(), limit=1, backfill=True)

    assert result["count"] == 1
    assert result["candidates"][0]["key"] == str(path)


def _record_key(record):
    return hashlib.sha256(record["summary"].strip().encode("utf-8")).hexdigest()[:8]


def test_write_sends_a_prepared_record(env, monkeypatch, capsys):
    path = _transcript(env)
    _pending(env, path)
    gateway = Gateway()

    code = _run(monkeypatch, ["write", "--key", str(path)], RECORD, gateway)
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["outcome"] == "written"
    assert output["id"] == "mem-1"
    create_call = [call for call in gateway.calls if call[0] == "POST" and call[1] == "/v1/experiences"]
    assert len(create_call) == 1
    body = json.loads(create_call[0][2])
    assert body["project_id"] == "proj"
    sent = body["record"]
    assert sent["tags"] == ["auto", "unreviewed", "docker", "ghcr"]
    assert sent["confidence"] == 0.8
    assert sent["source_agent"] == f"auto:s1:abcdef01:{_record_key(RECORD)}"
    assert Cursor(pathlib.Path(env["state"])).load().entry(str(path))["outcome"] == "written"


def test_write_is_idempotent_per_digest(env, monkeypatch, capsys):
    path = _transcript(env)
    _pending(env, path)
    existing = [{"id": "old", "record": {"source_agent": f"auto:s1:abcdef01:{_record_key(RECORD)}"}}]
    gateway = Gateway(existing=existing)

    code = _run(monkeypatch, ["write", "--key", str(path)], RECORD, gateway)
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["outcome"] == "already_written"
    assert [call[1] for call in gateway.calls] == ["/v1/experiences"]
    # Deprecated records must still block a rewrite: a human may have retired it on purpose.
    assert "include_deprecated=true" in gateway.calls[0][3]
    assert Cursor(pathlib.Path(env["state"])).load().entry(str(path))["outcome"] == "already_written"


def test_write_allows_a_new_topic_from_the_same_session(env, monkeypatch, capsys):
    path = _transcript(env)
    _pending(env, path)
    gateway = Gateway(existing=[{"id": "old", "record": {"source_agent": "auto:s1:deadbeef"}}])

    code = _run(monkeypatch, ["write", "--key", str(path)], RECORD, gateway)
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["outcome"] == "written"
    assert [call[1] for call in gateway.calls].count("/v1/experiences") == 2


def test_write_skips_a_duplicate(env, monkeypatch, capsys):
    path = _transcript(env)
    _pending(env, path)
    gateway = Gateway(dedup=[{"id": "mem-old", "score": 0.93, "summary": "同一条", "tags": ["auto"]}])

    code = _run(monkeypatch, ["write", "--key", str(path)], RECORD, gateway)
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["outcome"] == "duplicate"
    assert not [call for call in gateway.calls if call[0] == "POST" and call[1] == "/v1/experiences"]


def test_write_never_supersedes_a_related_automatic_record(env, monkeypatch, capsys):
    path = _transcript(env)
    _pending(env, path)
    gateway = Gateway(dedup=[{"id": "mem-auto", "score": 0.8, "summary": "相关但不同", "tags": ["auto", "docker"]}])

    _run(monkeypatch, ["write", "--key", str(path)], RECORD, gateway)
    output = json.loads(capsys.readouterr().out)

    assert output["outcome"] == "written"
    assert output["near_duplicate"] == "mem-auto"
    create_call = [call for call in gateway.calls if call[0] == "POST" and call[1] == "/v1/experiences"][0]
    assert not json.loads(create_call[2])["record"].get("supersedes")


def test_write_refuses_to_override_a_handwritten_record(env, monkeypatch, capsys):
    path = _transcript(env)
    _pending(env, path)
    gateway = Gateway(dedup=[{"id": "mem-human", "score": 0.8, "summary": "人工条目", "tags": ["docker"]}])

    code = _run(monkeypatch, ["write", "--key", str(path)], RECORD, gateway)
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["outcome"] == "conflicts_with_curated_record"
    assert not [call for call in gateway.calls if call[0] == "POST" and call[1] == "/v1/experiences"]


def test_write_rejects_code_before_touching_the_network(env, monkeypatch, capsys):
    path = _transcript(env)
    _pending(env, path)
    gateway = Gateway()
    record = dict(RECORD, successful_solution={"steps": ["改这里：\n```python\nprint(1)\n```"]})

    code = _run(monkeypatch, ["write", "--key", str(path)], record, gateway)
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["outcome"] == "rejected"
    assert gateway.calls == []


def test_write_keeps_the_cursor_when_the_gateway_is_down(env, monkeypatch, capsys):
    path = _transcript(env)
    _pending(env, path)
    gateway = Gateway(fail=httpx.ConnectError("gateway down"))

    code = _run(monkeypatch, ["write", "--key", str(path)], RECORD, gateway)
    json.loads(capsys.readouterr().out)
    entry = Cursor(pathlib.Path(env["state"])).load().entry(str(path))

    assert code == 2
    assert entry.get("outcome") != "written"
    assert entry.get("pending")


def test_write_records_a_schema_rejection(env, monkeypatch, capsys):
    path = _transcript(env)
    _pending(env, path)

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/dedup-check"):
            return httpx.Response(200, json=[])
        return httpx.Response(422, json={"detail": "applicable_scope is required"})

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(RECORD, ensure_ascii=False)))
    code = cli.main(["write", "--key", str(path)], transport=httpx.MockTransport(handler))
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["outcome"] == "rejected"
    assert Cursor(pathlib.Path(env["state"])).load().entry(str(path))["outcome"] == "rejected"


def test_mark_rejects_unknown_reasons(env, monkeypatch, capsys):
    path = _transcript(env)
    _pending(env, path)

    with pytest.raises(SystemExit) as exited:
        _run(monkeypatch, ["mark", "--key", str(path), "--reason", "made-up"])
    capsys.readouterr()

    assert exited.value.code == 2


def _long_transcript(env, name="long", turns=40, workspace="demo"):
    folder = env["projects"] / f"-Users-x-{workspace}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.jsonl"
    entries = [{"type": "workspace-directories", "sessionId": name, "directories": [f"/work/{workspace}"]}]
    for index in range(turns):
        entries.append(
            {"type": "user", "sessionId": name, "message": {"role": "user", "content": [{"type": "text", "text": f"第 {index} 轮 目标"}]}}
        )
        entries.append(
            {
                "type": "assistant",
                "sessionId": name,
                "message": {"role": "assistant", "content": [{"type": "text", "text": f"第 {index} 轮 结论 " + "细节" * 190}]},
            }
        )
        entries.append(
            {
                "type": "user",
                "sessionId": name,
                "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]},
                "toolUseResult": {"status": "success"},
            }
        )
    path.write_text("".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries), "utf-8")
    old = time.time() - 600
    os.utime(path, (old, old))
    return path


def test_scan_session_splits_a_whole_session(env):
    path = _long_transcript(env)

    result = cli.scan_session(cli.load_config(), "long", segments=6)

    assert result["count"] == 6
    assert result["candidates"][0]["key"] == f"{path}::seg-1"
    assert result["candidates"][0]["segment"] == "1/6"
    assert result["candidates"][-1]["segment"] == "6/6"
    combined = "".join(candidate["digest"] for candidate in result["candidates"])
    assert "第 0 轮" in combined and "第 39 轮" in combined
    state = Cursor(pathlib.Path(env["state"])).load()
    assert state.entry(f"{path}::seg-6")["pending"]["segment"] == 6
    # The hourly per-file cursor is untouched, so a deep scan cannot rewind it.
    assert state.entry(str(path)) == {}


def test_scan_session_honours_the_limit(env):
    _long_transcript(env)

    result = cli.scan_session(cli.load_config(), "long", segments=6, limit=2)

    assert result["count"] == 2


def test_scan_session_rejects_an_unknown_id(env):
    with pytest.raises(cli.CursorError):
        cli.scan_session(cli.load_config(), "missing-session")


def test_scan_session_rejects_inbox_trajectories(env):
    path = _trajectory(env)

    with pytest.raises(cli.CursorError):
        cli.scan_session(cli.load_config(), str(path))


def test_write_keep_lets_one_candidate_yield_several_records(env, monkeypatch, capsys):
    path = _transcript(env)
    _pending(env, path)
    gateway = Gateway()
    second = dict(RECORD, summary="另一条独立经验：只读副本不能当主库", task_goal="避免写入打到只读副本")

    _run(monkeypatch, ["write", "--key", str(path), "--keep"], RECORD, gateway)
    first = json.loads(capsys.readouterr().out)
    assert first["outcome"] == "written"
    kept = Cursor(pathlib.Path(env["state"])).load().entry(str(path))["pending"]
    assert kept["written"][first["record_key"]] == "mem-1"

    _run(monkeypatch, ["write", "--key", str(path), "--keep"], second, gateway)
    second_out = json.loads(capsys.readouterr().out)
    assert second_out["outcome"] == "written"
    assert second_out["record_key"] != first["record_key"]

    creates = [json.loads(call[2]) for call in gateway.calls if call[0] == "POST" and call[1] == "/v1/experiences"]
    assert len(creates) == 2
    assert creates[0]["record"]["source_agent"] != creates[1]["record"]["source_agent"]
    assert creates[1]["record"]["source_agent"].endswith(second_out["record_key"])

    _run(monkeypatch, ["finalize", "--key", str(path)])
    final = json.loads(capsys.readouterr().out)
    assert final["outcome"] == "finalized"
    assert final["count"] == 2
    entry = Cursor(pathlib.Path(env["state"])).load().entry(str(path))
    assert entry["outcome"] == "finalized"
    assert "pending" not in entry
    assert entry["offset"] == path.stat().st_size


def test_write_keep_short_circuits_a_repeated_summary(env, monkeypatch, capsys):
    path = _transcript(env)
    _pending(env, path)
    gateway = Gateway()

    _run(monkeypatch, ["write", "--key", str(path), "--keep"], RECORD, gateway)
    first = json.loads(capsys.readouterr().out)
    _run(monkeypatch, ["write", "--key", str(path), "--keep"], RECORD, gateway)
    second = json.loads(capsys.readouterr().out)

    assert second["outcome"] == "already_written"
    assert second["id"] == first["id"]
    assert len([call for call in gateway.calls if call[0] == "POST" and call[1] == "/v1/experiences"]) == 1


def test_write_keep_caps_records_per_candidate(env, monkeypatch, capsys):
    path = _transcript(env)
    _pending(env, path)
    gateway = Gateway()

    outcomes = []
    for index in range(cli.MAX_RECORDS_PER_CANDIDATE + 1):
        record = dict(RECORD, summary=f"第 {index} 条独立经验")
        _run(monkeypatch, ["write", "--key", str(path), "--keep"], record, gateway)
        outcomes.append(json.loads(capsys.readouterr().out))

    assert [item["outcome"] for item in outcomes] == ["written"] * cli.MAX_RECORDS_PER_CANDIDATE + ["cap_reached"]
    assert len([call for call in gateway.calls if call[0] == "POST" and call[1] == "/v1/experiences"]) == cli.MAX_RECORDS_PER_CANDIDATE


def test_finalize_without_records_marks_the_candidate_skipped(env, monkeypatch, capsys):
    path = _transcript(env)
    _pending(env, path)

    _run(monkeypatch, ["finalize", "--key", str(path)])
    output = json.loads(capsys.readouterr().out)

    assert output["outcome"] == "finalized"
    assert output["count"] == 0
    entry = Cursor(pathlib.Path(env["state"])).load().entry(str(path))
    assert entry["outcome"] == "skipped"
    assert entry["reason"] == "no_reusable_experience"
