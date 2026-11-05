#!/usr/bin/env python3
"""Expose the curated experience memory to coding agents over MCP (stdio).

Deliberately mirrors integrations/agent-plugin-core/python/mcp_server.py: hand-rolled
JSON-RPC over line-delimited stdio, no MCP SDK. The bridge holds no policy of its own -
every call goes to the gateway with one role's API key, so read/write isolation stays a
property of the gateway rather than of this file.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "mem0-curated"
SERVER_VERSION = "1.0.0"
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8787"
REQUEST_TIMEOUT_SECONDS = 60.0

_client: Optional[httpx.Client] = None


class ToolInputError(ValueError):
    pass


class ConfigError(ValueError):
    pass


class GatewayError(RuntimeError):
    pass


def get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
    return _client


def resolve_config() -> Dict[str, str]:
    """Read the environment per call so a restarted host picks up edits without a rebuild."""
    api_key = os.environ.get("CURATED_API_KEY", "").strip()
    if not api_key:
        raise ConfigError(
            "CURATED_API_KEY is not set. Use a worker key for coding sessions, a curator key for curation."
        )

    project_id = os.environ.get("CURATED_PROJECT_ID", "").strip()
    if not project_id:
        raise ConfigError(
            "CURATED_PROJECT_ID is not set. It scopes every call, so a session cannot read or write another project's memory."
        )

    return {
        "gateway_url": os.environ.get("CURATED_GATEWAY_URL", DEFAULT_GATEWAY_URL).rstrip("/"),
        "api_key": api_key,
        "project_id": project_id,
    }


def _request(method: str, path: str, *, json_body: Optional[Dict[str, Any]] = None) -> Any:
    config = resolve_config()
    url = f"{config['gateway_url']}{path}"
    try:
        response = get_client().request(method, url, json=json_body, headers={"X-API-Key": config["api_key"]})
    except httpx.HTTPError as exc:
        raise GatewayError(f"cannot reach the curated gateway at {config['gateway_url']}: {exc}") from exc

    try:
        payload = response.json()
    except ValueError:
        payload = None

    if response.status_code >= 400:
        raise GatewayError(_error_text(payload, response.status_code))
    return payload


def _error_text(payload: Any, status: int) -> str:
    """Turn a gateway error body into one line a model can act on."""
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, list):
        parts = []
        for item in detail:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            location = ".".join(str(step) for step in item.get("loc", []) if step != "body")
            message = str(item.get("msg", item))
            parts.append(f"{location}: {message}" if location else message)
        return f"gateway rejected the request ({status}): " + "; ".join(parts)
    if isinstance(detail, str):
        return detail if status == 403 else f"gateway returned {status}: {detail}"
    return f"gateway returned {status}"


# --- argument helpers -------------------------------------------------------


def _as_dict(arguments: Any, allowed: List[str]) -> Dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ToolInputError("tool arguments must be an object.")
    unknown = sorted(set(arguments) - set(allowed))
    if unknown:
        raise ToolInputError(f"unknown argument '{unknown[0]}'; allowed: {', '.join(allowed)}")
    return arguments


def _required_text(arguments: Dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"'{key}' must be a non-empty string.")
    return value.strip()


def _optional_text(arguments: Dict[str, Any], key: str, default: str = "") -> str:
    value = arguments.get(key, default)
    if not isinstance(value, str):
        raise ToolInputError(f"'{key}' must be a string.")
    return value


def _optional_int(arguments: Dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = arguments.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ToolInputError(f"'{key}' must be an integer from {low} to {high}.")
    return value


def _optional_float(arguments: Dict[str, Any], key: str, default: float) -> float:
    value = arguments.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
        raise ToolInputError(f"'{key}' must be a number from 0.0 to 1.0.")
    return float(value)


def _optional_bool(arguments: Dict[str, Any], key: str, default: bool) -> bool:
    value = arguments.get(key, default)
    if not isinstance(value, bool):
        raise ToolInputError(f"'{key}' must be a boolean.")
    return value


def _optional_string_list(arguments: Dict[str, Any], key: str) -> List[str]:
    value = arguments.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ToolInputError(f"'{key}' must be a list of non-empty strings.")
    return [item.strip() for item in value]


# --- tools ------------------------------------------------------------------

RECORD_FIELDS = [
    "memory_type",
    "summary",
    "task_goal",
    "constraints",
    "successful_solution",
    "failed_attempts",
    "applicable_scope",
    "exclude_scope",
    "tags",
    "supersedes",
    "source_agent",
    "confidence",
]

_FAILED_ATTEMPT_SCHEMA = {
    "type": "object",
    "properties": {
        "try_content": {"type": "string", "minLength": 1, "description": "What was attempted and did not work."},
        "root_cause": {"type": "string", "minLength": 1, "description": "Why it failed, at the mechanism level."},
        "lesson": {"type": "string", "minLength": 1, "description": "What to do instead next time."},
    },
    "required": ["try_content", "root_cause", "lesson"],
    "additionalProperties": False,
}

_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "search_experiences",
        "description": (
            "Recall curated experience records for this project. Use it before re-investigating "
            "something a teammate's session may already have solved. Returns whole records, not "
            "snippets: read applicable_scope and exclude_scope before trusting one."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": "What you are trying to do, in your own words.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "description": "Narrow to these tags (case-insensitive). Omit to search everything.",
                },
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Maximum records to return. Default 5.",
                },
                "threshold": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Minimum similarity. Default 0.1.",
                },
                "include_deprecated": {
                    "type": "boolean",
                    "description": "Include superseded records. Default false; deprecated guidance is hidden on purpose.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        "allowed": ["query", "tags", "top_k", "threshold", "include_deprecated"],
    },
    {
        "name": "record_experience",
        "description": (
            "Store one reusable experience record. Curator sessions only - a worker key is refused "
            "by the gateway. Call dedup_check first and, when you are replacing an older record, "
            "set supersedes to its id. applicable_scope and exclude_scope are mandatory: without a "
            "boundary the record will mislead a later session in a different environment."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_type": {
                    "type": "string",
                    "enum": ["experience"],
                    "description": "Optional. The server accepts only this value, so omitting it is fine.",
                },
                "summary": {"type": "string", "minLength": 1, "description": "One line stating the finding."},
                "task_goal": {"type": "string", "minLength": 1, "description": "The goal the experience applies to."},
                "constraints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Conditions that held: versions, environment, configuration.",
                },
                "successful_solution": {
                    "type": "object",
                    "properties": {
                        "steps": {"type": "array", "items": {"type": "string"}},
                        "verification": {"type": "string", "description": "How you proved it worked."},
                    },
                    "additionalProperties": False,
                },
                "failed_attempts": {
                    "type": "array",
                    "items": _FAILED_ATTEMPT_SCHEMA,
                    "description": "Dead ends worth not repeating.",
                },
                "applicable_scope": {"type": "string", "minLength": 1, "description": "Where this holds."},
                "exclude_scope": {"type": "string", "minLength": 1, "description": "Where this must not be applied."},
                "tags": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                    "description": "Retrieval keys, lowercased server-side. At least one.",
                },
                "supersedes": {
                    "type": "string",
                    "description": "Id of the record this replaces. Empty when nothing is superseded.",
                },
                "source_agent": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Which session the trajectory came from.",
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "How much you trust it: verified runs push it up.",
                },
            },
            "required": [
                "summary",
                "task_goal",
                "applicable_scope",
                "exclude_scope",
                "tags",
                "source_agent",
                "confidence",
            ],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "idempotentHint": False, "openWorldHint": False},
        "allowed": RECORD_FIELDS,
    },
    {
        "name": "dedup_check",
        "description": (
            "Look for near-duplicate records before writing a new one. High scores mean the finding "
            "already exists: update or supersede it instead of writing a second copy."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": "The summary you are about to write.",
                },
                "tags": {"type": "array", "items": {"type": "string", "minLength": 1}},
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "Maximum candidates. Default 5.",
                },
                "threshold": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Duplicate cutoff. Default 0.85.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        "allowed": ["query", "tags", "top_k", "threshold"],
    },
    {
        "name": "deprecate_experience",
        "description": (
            "Retire a record that is no longer valid without deleting it. The replacement chain and "
            "history stay intact; the record simply stops surfacing in search."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "minLength": 1, "description": "Id of the record to retire."},
                "reason": {"type": "string", "minLength": 1, "description": "Why it no longer applies."},
                "superseded_by": {"type": "string", "description": "Id of the record that replaces it, if any."},
            },
            "required": ["memory_id", "reason"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "idempotentHint": True, "openWorldHint": False},
        "allowed": ["memory_id", "reason", "superseded_by"],
    },
]

_ALLOWED_BY_TOOL = {tool["name"]: tool["allowed"] for tool in _TOOLS}
_PUBLIC_TOOLS = [{key: tool[key] for key in ("name", "description", "inputSchema", "annotations")} for tool in _TOOLS]


def call_tool(name: str, arguments: Any) -> Any:
    """Run one tool and return the gateway payload to be serialized as text."""
    allowed = _ALLOWED_BY_TOOL.get(name)
    if allowed is None:
        raise ToolInputError(f"unknown tool '{name}'.")

    args = _as_dict(arguments, allowed)
    project_id = resolve_config()["project_id"]

    if name == "search_experiences":
        body = {
            "query": _required_text(args, "query"),
            "project_id": project_id,
            "tags": _optional_string_list(args, "tags"),
            "top_k": _optional_int(args, "top_k", 5, 1, 50),
            "threshold": _optional_float(args, "threshold", 0.1),
            "include_deprecated": _optional_bool(args, "include_deprecated", False),
        }
        return _request("POST", "/v1/experiences/search", json_body=body)

    if name == "dedup_check":
        body = {
            "query": _required_text(args, "query"),
            "project_id": project_id,
            "tags": _optional_string_list(args, "tags"),
            "top_k": _optional_int(args, "top_k", 5, 1, 20),
            "threshold": _optional_float(args, "threshold", 0.85),
        }
        return _request("POST", "/v1/experiences/dedup-check", json_body=body)

    if name == "record_experience":
        # Only structural checks here. The 15-field contract belongs to the server, so the
        # bridge forwards the object untouched and reports whatever it rejects.
        record = {field: args[field] for field in RECORD_FIELDS if field in args}
        return _request("POST", "/v1/experiences", json_body={"project_id": project_id, "record": record})

    if name == "deprecate_experience":
        memory_id = _required_text(args, "memory_id")
        body = {
            "project_id": project_id,
            "reason": _required_text(args, "reason"),
            "superseded_by": _optional_text(args, "superseded_by"),
        }
        return _request("POST", f"/v1/experiences/{quote(memory_id, safe='')}/deprecate", json_body=body)

    raise ToolInputError(f"tool '{name}' is declared but not implemented.")


# --- JSON-RPC over stdio ----------------------------------------------------


def _tool_response(text: str, *, is_error: bool = False) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def handle_request(message: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(message, dict):
        return None
    request_id = message.get("id")
    method = message.get("method")

    if method == "notifications/initialized":
        return None
    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": requested or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": _PUBLIC_TOOLS}}
    if method == "tools/call":
        params = message.get("params") or {}
        try:
            payload = call_tool(params.get("name"), params.get("arguments"))
            result = _tool_response(json.dumps(payload, ensure_ascii=False, default=str))
        except (ToolInputError, ConfigError, GatewayError) as exc:
            result = _tool_response(str(exc), is_error=True)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}


def main() -> int:
    # Agents pass Chinese text through these streams; a C/POSIX locale would otherwise make
    # the default ASCII codec raise mid-response.
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    try:
        resolve_config()
    except ConfigError as exc:
        print(f"curated MCP bridge: {exc}", file=sys.stderr)
        return 2

    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            message = json.loads(raw_line)
            response = handle_request(message)
        except json.JSONDecodeError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        except Exception as exc:  # a stdio server that dies takes the agent's session with it
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": f"Internal error: {exc}"}}
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
