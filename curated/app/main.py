import os
import secrets
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query

from curated.app.memory_factory import build_memory
from curated.app.models import (
    CreateExperienceRequest,
    CreateResponse,
    DedupCandidate,
    DedupRequest,
    DeprecateRequest,
    ExperienceResult,
    RawSearchRequest,
    SearchRequest,
)
from curated.app.service import ExperienceService

app = FastAPI(title="mem0-curated experience API", version="1.0.0")

_service: Optional[ExperienceService] = None


def get_service() -> ExperienceService:
    global _service
    if _service is None:
        _service = ExperienceService(build_memory())
    return _service


def require_internal_token(x_internal_token: str = Header(default="")) -> None:
    """Second gate behind the network boundary: the gateway is the only intended caller."""
    expected = os.environ.get("INTERNAL_TOKEN", "")
    if not expected or not secrets.compare_digest(x_internal_token, expected):
        raise HTTPException(status_code=401, detail="Invalid internal token.")


INTERNAL_ONLY = [Depends(require_internal_token)]


@app.get("/health", summary="Liveness probe")
def health():
    return {"status": "ok"}


@app.post(
    "/v1/experiences",
    response_model=CreateResponse,
    summary="Store a curated experience record",
    dependencies=INTERNAL_ONLY,
)
def create_experience(request: CreateExperienceRequest, service: ExperienceService = Depends(get_service)):
    return service.create(request.record, request.project_id)


@app.post(
    "/v1/experiences/search",
    response_model=list[ExperienceResult],
    summary="Semantic search over curated experiences",
    dependencies=INTERNAL_ONLY,
)
def search_experiences(request: SearchRequest, service: ExperienceService = Depends(get_service)):
    return service.search(request)


@app.post(
    "/v1/experiences/dedup-check",
    response_model=list[DedupCandidate],
    summary="Find near-duplicate experiences before writing a new one",
    dependencies=INTERNAL_ONLY,
)
def dedup_check(request: DedupRequest, service: ExperienceService = Depends(get_service)):
    return service.dedup_check(request)


@app.get(
    "/v1/experiences",
    response_model=list[ExperienceResult],
    summary="List curated experiences for one project",
    dependencies=INTERNAL_ONLY,
)
def list_experiences(
    project_id: str = Query(..., min_length=1),
    tags: Optional[List[str]] = Query(None),
    include_deprecated: bool = False,
    limit: int = Query(500, ge=1, le=1000),
    service: ExperienceService = Depends(get_service),
):
    return service.list_experiences(project_id, tags or [], include_deprecated, limit)


@app.get(
    "/v1/projects",
    response_model=list[str],
    summary="List project ids that already have records",
    dependencies=INTERNAL_ONLY,
)
def list_projects(service: ExperienceService = Depends(get_service)):
    return service.list_projects()


@app.get(
    "/v1/experiences/{memory_id}",
    response_model=ExperienceResult,
    summary="Read one curated experience record",
    dependencies=INTERNAL_ONLY,
)
def get_experience(
    memory_id: str,
    project_id: str = Query(..., min_length=1),
    service: ExperienceService = Depends(get_service),
):
    return service.get(memory_id, project_id)


@app.post(
    "/v1/experiences/{memory_id}/deprecate",
    response_model=ExperienceResult,
    summary="Mark an experience as deprecated",
    dependencies=INTERNAL_ONLY,
)
def deprecate_experience(
    memory_id: str,
    request: DeprecateRequest,
    service: ExperienceService = Depends(get_service),
):
    return service.deprecate(memory_id, request.project_id, request.reason, request.superseded_by)


@app.post("/mem0/search", summary="mem0-compatible search, scoped to one project", dependencies=INTERNAL_ONLY)
def mem0_search(request: RawSearchRequest, service: ExperienceService = Depends(get_service)):
    return service.raw_search(request)


@app.get("/mem0/memories", summary="mem0-compatible listing, scoped to one project", dependencies=INTERNAL_ONLY)
def mem0_list_memories(
    project_id: str = Query(..., min_length=1),
    top_k: int = Query(100, ge=1, le=1000),
    service: ExperienceService = Depends(get_service),
):
    return service.list_memories(project_id, top_k)
