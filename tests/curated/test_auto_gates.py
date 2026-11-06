from curated.auto import gates


def _record(**overrides):
    record = {
        "memory_type": "experience",
        "summary": "服务器连不上 Docker Hub 时把镜像镜像进 GHCR",
        "task_goal": "在受限网络里完成部署",
        "constraints": ["服务器只通 ghcr.io"],
        "successful_solution": {"steps": ["CI 推镜像", "服务器只拉取"], "verification": "health 返回 200"},
        "failed_attempts": [{"try_content": "直接 pull", "root_cause": "超时", "lesson": "先看 registry 可达性"}],
        "applicable_scope": "只能访问单一镜像仓库的云主机",
        "exclude_scope": "能直连 Docker Hub 的环境",
        "tags": ["docker", "ghcr"],
        "confidence": 0.9,
        "source_agent": "auto:s1:abcd1234",
    }
    record.update(overrides)
    return record


def test_valid_record_has_no_shape_errors():
    assert gates.shape_errors(_record()) == []


def test_missing_scope_is_rejected():
    errors = gates.shape_errors(_record(applicable_scope="  "))

    assert any("applicable_scope" in error for error in errors)


def test_empty_tags_are_rejected():
    assert "tags must be a non-empty list" in gates.shape_errors(_record(tags=["  "]))


def test_record_needs_a_solution_or_a_failure():
    broken = _record()
    broken.pop("successful_solution")
    broken.pop("failed_attempts")

    assert any("successful_solution.steps or failed_attempts" in error for error in gates.shape_errors(broken))


def test_tags_are_normalized():
    assert gates.normalize_tags([" Docker ", "docker", "", "GHCR"]) == ["docker", "ghcr"]


def test_secret_scan_reports_labels_not_values():
    labels = gates.secret_hits(_record(constraints=["token=ghp_abcdefghijklmnopqrstuvwxyz12"]))

    assert "github-token" in labels
    assert all("ghp_" not in label for label in labels)


def test_code_smells_catch_fences_source_lines_and_indented_blocks():
    fenced = gates.code_smells(_record(successful_solution={"steps": ["执行：\n```bash\nuvicorn app:app\n```"]}))
    sourced = gates.code_smells(_record(successful_solution={"steps": ["这样改：\n    def handler():\n        return 1"]}))
    indented = gates.code_smells(_record(successful_solution={"steps": ["这样改：\n    第一行\n    第二行\n    第三行"]}))

    assert any("fenced code block" in reason for reason in fenced)
    assert any("source line" in reason for reason in sourced)
    assert any("indented code block" in reason for reason in indented)


def test_long_field_is_rejected():
    reasons = gates.code_smells(_record(applicable_scope="长" * 1600))

    assert any("longer than 1500" in reason for reason in reasons)


def test_confidence_is_capped_by_evidence():
    assert gates.cap_confidence(_record(confidence=0.95), "verified") == 0.8
    assert gates.cap_confidence(_record(confidence=0.95), "error_then_fixed") == 0.65
    assert gates.cap_confidence(_record(confidence=0.95), "none") == 0.5
    assert gates.cap_confidence(_record(confidence="high"), "none") == 0.5
