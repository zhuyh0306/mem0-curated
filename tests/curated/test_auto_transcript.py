import json
import pathlib

from curated.auto.transcript import MAX_DIGEST_CHARS, digest, digest_inbox, digest_segments, read_objects


def _write(path: pathlib.Path, entries) -> pathlib.Path:
    path.write_text("".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries), "utf-8")
    return path


def _user(text, session="s1"):
    return {"type": "user", "sessionId": session, "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _assistant(text=None, tool_uses=None, session="s1"):
    content = []
    if text is not None:
        content.append({"type": "text", "text": text})
    for tool_id, name, tool_input in tool_uses or []:
        content.append({"type": "tool_use", "id": tool_id, "name": name, "input": tool_input})
    return {"type": "assistant", "sessionId": session, "message": {"role": "assistant", "content": content}}


def _tool_result(tool_id, content, error=False, status="success"):
    return {
        "type": "user",
        "sessionId": "s1",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": content}]},
        "toolUseResult": {"status": status, "isError": error},
    }


def test_digest_keeps_names_and_status_but_not_tool_payloads(tmp_path):
    path = _write(
        tmp_path / "s1.jsonl",
        [
            {"type": "workspace-directories", "sessionId": "s1", "directories": ["/work/demo"]},
            _user("排查 qdrant 启动失败"),
            _assistant("先看容器日志", [("t1", "Bash", {"command": "cat /work/demo/secret.py"})]),
            _tool_result("t1", "def leak_me():\n    return 1", status="success"),
            _assistant("镜像源不可达，改成 GHCR"),
        ],
    )

    text, meta = digest(path, 0)

    assert "TOOL Bash ok" in text
    assert "secret.py" not in text
    assert "leak_me" not in text
    assert "排查 qdrant 启动失败" in text
    assert meta["workspace"] == "demo"
    assert meta["evidence"] == "verified"
    assert meta["turns"] == 3


def test_digest_strips_fenced_code_and_redacts_secrets(tmp_path):
    path = _write(
        tmp_path / "s1.jsonl",
        [
            _user("配置部署"),
            _assistant(
                "结论如下\n```python\nprint('leak')\n```\n"
                "key=sk-abcdefgh12345678 token=ghp_abcdefghijklmnopqrstuvwxyz12 "
                "password=hunter2 /Users/alice/project"
            ),
        ],
    )

    text, _ = digest(path, 0)

    for leaked in ("print('leak')", "sk-abcdefgh12345678", "ghp_abcdefghijklmnopqrstuvwxyz12", "hunter2", "alice"):
        assert leaked not in text
    assert "[code omitted]" in text
    assert "[redacted]" in text


def test_digest_keeps_the_first_user_message_as_goal(tmp_path):
    path = _write(tmp_path / "s1.jsonl", [_user("目标" * 400), _assistant("收尾")])

    text, meta = digest(path, 0)

    goal_line = next(line for line in text.splitlines() if line.startswith("goal: "))
    assert len(goal_line) <= len("goal: ") + 500
    assert meta["session_id"] == "s1"


def test_read_objects_never_advances_past_a_partial_tail(tmp_path):
    path = _write(tmp_path / "s1.jsonl", [_user("第一轮")])
    complete = path.stat().st_size
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type": "user", "sessionId": "s1", "message"')

    entries, end = read_objects(path, 0)
    assert end == complete
    assert len(entries) == 1

    with path.open("a", encoding="utf-8") as handle:
        handle.write(': {"role": "user", "content": [{"type": "text", "text": "第二轮"}]}}\n')

    entries, end = read_objects(path, end)
    assert [entry["message"]["content"][0]["text"] for entry in entries] == ["第二轮"]


def test_digest_detects_error_then_fix(tmp_path):
    path = _write(
        tmp_path / "s1.jsonl",
        [
            _user("跑测试"),
            _assistant("先跑一次", [("t1", "Bash", {"command": "pytest"})]),
            _tool_result("t1", "failed", error=True, status="error"),
            _assistant("补依赖", [("t2", "Bash", {"command": "pip install x"})]),
            _tool_result("t2", "ok", status="success"),
        ],
    )

    _, meta = digest(path, 0)

    assert meta["evidence"] == "error_then_fixed"


def test_digest_without_tool_results_has_no_evidence(tmp_path):
    path = _write(tmp_path / "s1.jsonl", [_user("闲聊"), _assistant("好的")])

    _, meta = digest(path, 0)

    assert meta["evidence"] == "none"


def test_digest_marks_its_own_automation_session(tmp_path):
    path = _write(tmp_path / "s1.jsonl", [_user("AUTO-CURATOR-RUN：读取并执行作业手册"), _assistant("开始")])

    text, meta = digest(path, 0)

    assert meta["self_generated"] is True
    assert len(text) <= MAX_DIGEST_CHARS


def test_digest_is_bounded_for_long_sessions(tmp_path):
    entries = [_user("很长的会话")]
    for index in range(60):
        entries.append(_assistant(f"第 {index} 轮 " + "细节" * 300))
    path = _write(tmp_path / "s1.jsonl", entries)

    text, _ = digest(path, 0)

    assert len(text) <= MAX_DIGEST_CHARS
    assert text.startswith("session=s1")
    assert "goal: 很长的会话" in text


def _trajectory(path: pathlib.Path, entries) -> pathlib.Path:
    return _write(path, entries)


def test_inbox_digest_redacts_and_tracks_evidence(tmp_path):
    path = _trajectory(
        tmp_path / "conv1.jsonl",
        [
            {"kind": "meta", "session_id": "conv1", "workspace": "/Users/alice/work/demo"},
            {"kind": "user", "text": "排查部署失败"},
            {"kind": "tool", "name": "Bash", "status": "error"},
            {
                "kind": "assistant",
                "text": "换镜像源\n```bash\ndocker pull leak\n```\nkey=sk-abcdefgh12345678 password=hunter2",
            },
            {"kind": "tool", "name": "Bash", "status": "ok"},
            {"kind": "verification", "text": "health 返回 200"},
        ],
    )

    text, meta = digest_inbox(path, 0)

    assert "TOOL Bash error" in text and "TOOL Bash ok" in text
    assert "VERIFY: health 返回 200" in text
    assert "[code omitted]" in text and "[redacted]" in text
    assert "docker pull leak" not in text
    assert "sk-abcdefgh12345678" not in text
    assert "hunter2" not in text
    assert "alice" not in text
    assert meta["session_id"] == "conv1"
    assert meta["workspace"] == "demo"
    assert meta["evidence"] == "error_then_fixed"
    assert meta["turns"] == 2


def test_inbox_digest_continues_from_offset(tmp_path):
    path = _trajectory(
        tmp_path / "conv1.jsonl",
        [
            {"kind": "meta", "session_id": "conv1", "workspace": "/work/demo"},
            {"kind": "user", "text": "第一轮"},
            {"kind": "assistant", "text": "结论一"},
        ],
    )
    _, meta = digest_inbox(path, 0)
    assert meta["turns"] == 2

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": "assistant", "text": "第二轮补充"}, ensure_ascii=False) + "\n")

    text, meta = digest_inbox(path, meta["end_offset"])

    assert "第二轮补充" in text
    assert "USER: 第一轮" not in text
    assert meta["turns"] == 1


def test_inbox_digest_marks_its_own_automation_session(tmp_path):
    path = _trajectory(
        tmp_path / "conv1.jsonl",
        [
            {"kind": "meta", "session_id": "conv1", "workspace": "/work/demo"},
            {"kind": "user", "text": "AUTO-CURATOR-RUN 执行作业手册"},
            {"kind": "assistant", "text": "开始"},
        ],
    )

    _, meta = digest_inbox(path, 0)

    assert meta["self_generated"] is True


def test_inbox_verification_alone_counts_as_verified(tmp_path):
    path = _trajectory(
        tmp_path / "conv1.jsonl",
        [
            {"kind": "user", "text": "跑测试"},
            {"kind": "assistant", "text": "改完了"},
            {"kind": "verification", "text": "414 passed"},
        ],
    )

    _, meta = digest_inbox(path, 0)

    assert meta["evidence"] == "verified"


def test_digest_offset_only_reads_new_lines(tmp_path):
    path = _write(tmp_path / "s1.jsonl", [_user("第一轮")])
    offset = path.stat().st_size
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_assistant("第二轮结论"), ensure_ascii=False) + "\n")

    text, meta = digest(path, offset)

    # The goal slot always comes from the head of the file; the turn window starts at offset.
    assert "goal: 第一轮" in text
    assert "第二轮结论" in text
    assert meta["turns"] == 1
    assert meta["end_offset"] == path.stat().st_size


def _long_session(tmp_path, turns=40):
    path = tmp_path / "s1.jsonl"
    entries = [{"type": "workspace-directories", "sessionId": "s1", "directories": ["/work/demo"]}]
    for index in range(turns):
        entries.append(_user(f"第 {index} 轮 目标"))
        entries.append(_assistant(f"第 {index} 轮 结论 " + "细节" * 190))
        entries.append(_tool_result("t", "ok"))
    return _write(path, entries)


def test_digest_segments_cover_the_whole_timeline(tmp_path):
    path = _long_session(tmp_path)

    parts = digest_segments(path, 6)

    assert len(parts) >= 3
    texts = [text for text, _ in parts]
    assert texts[0].startswith("session=s1 workspace=demo")
    assert "goal: 第 0 轮 目标" in texts[0]
    assert "segment=1/" in texts[0] and f"segment={len(parts)}/" in texts[-1]
    assert "第 0 轮" in texts[0]
    assert "第 39 轮" in texts[-1]
    combined = "\n".join(texts)
    for marker in ("第 10 轮", "第 20 轮", "第 30 轮"):
        assert marker in combined
    assert all(len(text) <= MAX_DIGEST_CHARS for text in texts)
    assert all(meta["end_offset"] == path.stat().st_size for _, meta in parts)
    assert [meta["segment"] for _, meta in parts] == list(range(1, len(parts) + 1))


def test_digest_segments_keep_a_short_session_in_one_piece(tmp_path):
    path = _write(tmp_path / "s1.jsonl", [_user("一句话目标"), _assistant("一句话结论")])

    parts = digest_segments(path, 6)

    assert len(parts) == 1
    assert parts[0][1]["segments"] == 1


def test_digest_segments_redact_and_detect_evidence(tmp_path):
    path = _write(
        tmp_path / "s1.jsonl",
        [
            _user("排查部署失败"),
            _assistant("先看日志", [("t1", "Bash", {"command": "cat secret.py"})]),
            _tool_result("t1", "failed", error=True, status="error"),
            _assistant("换镜像源 key=sk-abcdefgh12345678"),
            _tool_result("t2", "ok"),
        ],
    )

    parts = digest_segments(path, 3)

    combined = "\n".join(text for text, _ in parts)
    assert "sk-abcdefgh12345678" not in combined
    assert "[redacted]" in combined
    assert any(meta["evidence"] == "error_then_fixed" for _, meta in parts)


def test_digest_segments_marks_the_automation_session(tmp_path):
    path = _write(tmp_path / "s1.jsonl", [_user("AUTO-CURATOR-RUN 执行作业手册"), _assistant("开始")])

    parts = digest_segments(path, 3)

    assert parts and all(meta["self_generated"] is True for _, meta in parts)
