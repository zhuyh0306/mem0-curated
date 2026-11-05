"""Contract tests for the curated MCP stdio bridge.

The HTTP boundary is faked with httpx.MockTransport, so the real request building runs:
only the gateway itself is replaced.
"""

import json

import httpx
import pytest

from curated.mcp import server


@pytest.fixture(autouse=True)
def configured_env(monkeypatch):
    monkeypatch.setenv("CURATED_GATEWAY_URL", "http://gateway.test:8787")
    monkeypatch.setenv("CURATED_API_KEY", "wk-secret")
    monkeypatch.setenv("CURATED_PROJECT_ID", "proj-demo")
    server._client = None
    yield
    server._client = None


def install(handler):
    """Route the bridge's own httpx client through a fake gateway."""
    server._client = httpx.Client(transport=httpx.MockTransport(handler))


def call(name, arguments):
    return server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    )


def text_of(response):
    return response["result"]["content"][0]["text"]


# --- protocol envelope ---


def test_initialize_prefers_the_requested_protocol_version():
    response = server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    )
    assert response["result"]["protocolVersion"] == "2025-06-18"
    assert response["result"]["capabilities"]["tools"]["listChanged"] is False


def test_initialize_falls_back_when_the_client_names_no_version():
    response = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert response["result"]["protocolVersion"] == server.PROTOCOL_VERSION


def test_initialized_notification_is_not_answered():
    assert server.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_ping_is_answered_with_an_empty_result():
    assert server.handle_request({"jsonrpc": "2.0", "id": 7, "method": "ping"})["result"] == {}


def test_unknown_method_returns_jsonrpc_error():
    response = server.handle_request({"jsonrpc": "2.0", "id": 3, "method": "prompts/list"})
    assert response["error"]["code"] == -32601


def test_tools_list_marks_only_the_read_tools_read_only():
    tools = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert {tool["name"] for tool in tools} == {
        "search_experiences",
        "record_experience",
        "dedup_check",
        "deprecate_experience",
    }
    read_only = {tool["name"]: tool["annotations"]["readOnlyHint"] for tool in tools}
    assert read_only == {
        "search_experiences": True,
        "dedup_check": True,
        "record_experience": False,
        "deprecate_experience": False,
    }


# --- project scope comes from configuration, never from the model ---


def test_search_sends_the_configured_project_scope():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["key"] = request.headers["x-api-key"]
        return httpx.Response(200, json=[])

    install(handler)
    response = call("search_experiences", {"query": "why did the filter skip records"})
    assert response["result"]["isError"] is False
    assert seen["url"] == "http://gateway.test:8787/v1/experiences/search"
    assert seen["key"] == "wk-secret"
    assert seen["body"] == {
        "query": "why did the filter skip records",
        "project_id": "proj-demo",
        "tags": [],
        "top_k": 5,
        "threshold": 0.1,
        "include_deprecated": False,
    }


def test_project_id_is_not_a_tool_argument():
    """A tool argument for the scope would let a session write into somebody else's project."""
    calls = []
    install(lambda request: calls.append(request) or httpx.Response(200, json=[]))

    response = call("record_experience", {"project_id": "proj-other", "summary": "s", "task_goal": "t"})
    assert response["result"]["isError"] is True
    assert "project_id" in text_of(response)
    assert calls == []


def test_record_body_nests_the_fields_under_record():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "m1", "supersedes": "", "deprecated_target": None})

    install(handler)
    arguments = {
        "summary": "boolean flags need an explicit default",
        "task_goal": "hide deprecated experiences",
        "constraints": ["qdrant 1.12"],
        "successful_solution": {"steps": ["write deprecated=false"], "verification": "search hides it"},
        "failed_attempts": [
            {
                "try_content": "omit the default",
                "root_cause": "must_not skips absent fields",
                "lesson": "write it explicitly",
            }
        ],
        "applicable_scope": "qdrant with payload filters",
        "exclude_scope": "time-based invalidation",
        "tags": ["Qdrant", "metadata-filter"],
        "source_agent": "worker-coding-1",
        "confidence": 0.9,
    }
    text = text_of(call("record_experience", arguments))
    assert json.loads(text) == {"id": "m1", "supersedes": "", "deprecated_target": None}
    assert seen["body"]["project_id"] == "proj-demo"
    assert seen["body"]["record"] == arguments


def test_memory_type_is_accepted_so_the_prompt_json_can_be_posted_as_is():
    """curator_agent.md shows a record with "memory_type": "experience".

    Rejecting the field the prompt teaches would make the first MCP write from a real Curator
    fail on a field that carries no information (the server fixes it to one literal).
    """
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "m1", "supersedes": "", "deprecated_target": None})

    install(handler)
    response = call(
        "record_experience",
        {
            "memory_type": "experience",
            "summary": "s",
            "task_goal": "t",
            "applicable_scope": "a",
            "exclude_scope": "e",
            "tags": ["x"],
            "source_agent": "curator-1",
            "confidence": 0.7,
        },
    )
    assert response["result"]["isError"] is False
    assert seen["body"]["record"]["memory_type"] == "experience"


def test_deprecate_puts_the_id_in_the_path():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "m1", "deprecated": True})

    install(handler)
    call("deprecate_experience", {"memory_id": "m1", "reason": "superseded by the v3 API"})
    assert seen["url"] == "http://gateway.test:8787/v1/experiences/m1/deprecate"
    assert seen["body"] == {"project_id": "proj-demo", "reason": "superseded by the v3 API", "superseded_by": ""}


def test_memory_id_is_percent_encoded():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"id": "x"})

    install(handler)
    call("deprecate_experience", {"memory_id": "needs encoding", "reason": "r"})
    assert seen["url"] == "http://gateway.test:8787/v1/experiences/needs%20encoding/deprecate"


def test_traversal_in_a_memory_id_stays_inside_one_path_segment():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"id": "x"})

    install(handler)
    call("deprecate_experience", {"memory_id": "../../etc/passwd", "reason": "r"})
    assert seen["url"].endswith("/deprecate")
    assert seen["url"].count("/v1/experiences/") == 1
    assert "etc/passwd" not in seen["url"]


# --- gateway errors reach the model as text, not as a crashed process ---


def test_gateway_403_text_is_passed_through():
    install(
        lambda request: httpx.Response(403, json={"detail": "role 'worker' is not allowed to POST /v1/experiences"})
    )
    response = call("record_experience", {"summary": "s", "task_goal": "t"})
    assert response["result"]["isError"] is True
    assert "role 'worker' is not allowed to POST /v1/experiences" in text_of(response)


def test_gateway_422_names_the_offending_field():
    install(
        lambda request: httpx.Response(
            422,
            json={"detail": [{"loc": ["body", "record", "tags"], "msg": "tags must contain at least one tag"}]},
        )
    )
    response = call("record_experience", {"summary": "s", "task_goal": "t", "tags": []})
    assert "tags" in text_of(response)
    assert "at least one tag" in text_of(response)


def test_connection_refused_is_reported_not_raised():
    def handler(request):
        raise httpx.ConnectError("all connection attempts failed")

    install(handler)
    response = call("search_experiences", {"query": "q"})
    assert response["result"]["isError"] is True
    assert "gateway" in text_of(response).lower()


def test_missing_api_key_is_reported_at_call_time(monkeypatch):
    monkeypatch.delenv("CURATED_API_KEY")
    response = call("search_experiences", {"query": "q"})
    assert response["result"]["isError"] is True
    assert "CURATED_API_KEY" in text_of(response)


def test_missing_project_id_names_its_variable(monkeypatch):
    monkeypatch.delenv("CURATED_PROJECT_ID")
    response = call("record_experience", {"summary": "s", "task_goal": "t"})
    assert response["result"]["isError"] is True
    assert "CURATED_PROJECT_ID" in text_of(response)


# --- argument validation happens before any request leaves the process ---


def test_blank_query_is_rejected_without_calling_the_gateway():
    calls = []
    install(lambda request: calls.append(request) or httpx.Response(200, json=[]))
    response = call("search_experiences", {"query": "   "})
    assert response["result"]["isError"] is True
    assert "query" in text_of(response)
    assert calls == []


def test_top_k_out_of_range_is_rejected_locally():
    calls = []
    install(lambda request: calls.append(request) or httpx.Response(200, json=[]))
    response = call("search_experiences", {"query": "q", "top_k": 500})
    assert response["result"]["isError"] is True
    assert calls == []


def test_unknown_tool_is_an_error_not_a_crash():
    response = call("delete_everything", {})
    assert response["result"]["isError"] is True
