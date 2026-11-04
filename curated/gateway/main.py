import logging
import os
from typing import Dict, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from curated.gateway.auth import resolve_role
from curated.gateway.policy import denied_reason, is_allowed

logger = logging.getLogger(__name__)

UPSTREAM_URL = os.environ.get("UPSTREAM_URL", "http://mem0-curated:8000")
INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN", "")

_STRIP_REQUEST_HEADERS = {"host", "x-api-key", "x-internal-token", "content-length", "connection"}
_STRIP_RESPONSE_HEADERS = {
    "content-length",
    "content-type",
    "transfer-encoding",
    "connection",
    "content-encoding",
}

_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
    return _client


app = FastAPI(title="mem0-curated gateway", version="1.0.0")


@app.get("/health", summary="Gateway liveness probe")
def health():
    return {"status": "ok", "upstream": UPSTREAM_URL}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request):
    role = resolve_role(request.headers.get("x-api-key", ""))
    if role is None:
        return JSONResponse(status_code=401, content={"detail": "Missing or unknown X-API-Key."})

    full_path = "/" + path
    if not is_allowed(role, request.method, full_path):
        logger.warning("denied %s %s for role %s", request.method, full_path, role)
        return JSONResponse(status_code=403, content={"detail": denied_reason(role, request.method, full_path)})

    headers: Dict[str, str] = {
        name: value for name, value in request.headers.items() if name.lower() not in _STRIP_REQUEST_HEADERS
    }
    headers["x-internal-token"] = INTERNAL_TOKEN

    upstream_response = await get_client().request(
        request.method,
        f"{UPSTREAM_URL}{full_path}",
        content=await request.body(),
        headers=headers,
        params=request.query_params.multi_items(),
    )

    response_headers = {
        name: value for name, value in upstream_response.headers.items() if name.lower() not in _STRIP_RESPONSE_HEADERS
    }
    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )
