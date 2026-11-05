import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

GATEWAY_URL = os.environ.get("CURATED_GATEWAY_URL", "http://127.0.0.1:8787")


def _load_key() -> str:
    single = os.environ.get("CURATED_API_KEY", "").strip()
    if single:
        return single
    return os.environ.get("CURATED_API_KEYS", "").split(",")[0].strip()


API_KEY = _load_key()
DEFAULT_PROJECT = os.environ.get("CURATED_PROJECT_ID", "default")
TIMEOUT_SECONDS = float(os.environ.get("CURATED_DASHBOARD_TIMEOUT", "90"))

INDEX = Path(__file__).parent / "index.html"

app = FastAPI(title="mem0-curated dashboard", version="1.0.0")

_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_SECONDS))
    return _client


def require_same_origin(request: Request) -> None:
    """Writes must come from this page. A browser always sets Origin on a cross-origin POST,
    and it cannot forge it, so rejecting foreign origins closes the CSRF path that a
    localhost-bound, key-bearing service would otherwise leave open."""
    origin = request.headers.get("origin")
    if origin is None:
        return
    host = request.headers.get("host", "")
    if origin not in {f"http://{host}", f"https://{host}"}:
        raise HTTPException(status_code=403, detail="Cross-origin writes are not allowed.")


async def call_gateway(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
) -> JSONResponse:
    try:
        upstream = await get_client().request(
            method,
            f"{GATEWAY_URL}{path}",
            params=params,
            json=body,
            headers={"X-API-Key": API_KEY},
        )
    except httpx.TimeoutException:
        return JSONResponse(
            status_code=504,
            content={"detail": f"memory backend did not answer within {TIMEOUT_SECONDS:.0f}s."},
        )
    except httpx.RequestError as exc:
        return JSONResponse(
            status_code=502,
            content={"detail": f"cannot reach the gateway at {GATEWAY_URL}: {exc}"},
        )

    try:
        payload = upstream.json()
    except ValueError:
        payload = {"detail": upstream.text}
    return JSONResponse(status_code=upstream.status_code, content=payload)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(INDEX, media_type="text/html")


@app.get("/api/config")
def config() -> Dict[str, Any]:
    return {"project_id": DEFAULT_PROJECT, "writable": bool(API_KEY), "gateway": GATEWAY_URL}


@app.get("/api/records")
async def list_records(
    project_id: str = Query(..., min_length=1),
    tags: Optional[List[str]] = Query(None),
    include_deprecated: bool = False,
    limit: int = Query(500, ge=1, le=1000),
) -> JSONResponse:
    return await call_gateway(
        "GET",
        "/v1/experiences",
        params={
            "project_id": project_id,
            "tags": tags or [],
            "include_deprecated": include_deprecated,
            "limit": limit,
        },
    )


@app.get("/api/records/{memory_id}")
async def read_record(memory_id: str, project_id: str = Query(..., min_length=1)) -> JSONResponse:
    return await call_gateway(
        "GET",
        f"/v1/experiences/{memory_id}",
        params={"project_id": project_id},
    )


@app.post("/api/search")
async def search(request: Request) -> JSONResponse:
    return await call_gateway("POST", "/v1/experiences/search", body=await request.json())


@app.post("/api/dedup")
async def dedup(request: Request) -> JSONResponse:
    return await call_gateway("POST", "/v1/experiences/dedup-check", body=await request.json())


@app.post("/api/records")
async def create_record(request: Request) -> JSONResponse:
    require_same_origin(request)
    return await call_gateway("POST", "/v1/experiences", body=await request.json())


@app.post("/api/records/{memory_id}/deprecate")
async def deprecate_record(memory_id: str, request: Request) -> JSONResponse:
    require_same_origin(request)
    return await call_gateway(
        "POST",
        f"/v1/experiences/{memory_id}/deprecate",
        body=await request.json(),
    )
