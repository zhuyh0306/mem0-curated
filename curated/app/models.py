from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


def _normalize_tags(tags: List[str], *, required: bool) -> List[str]:
    normalized: List[str] = []
    for tag in tags:
        cleaned = tag.strip().lower()
        if not cleaned:
            raise ValueError("tags must not contain blank entries")
        if cleaned not in normalized:
            normalized.append(cleaned)
    if required and not normalized:
        raise ValueError("tags must contain at least one tag: tags are the primary retrieval key")
    return normalized


class FailedAttempt(BaseModel):
    try_content: str = Field(..., min_length=1)
    root_cause: str = Field(..., min_length=1)
    lesson: str = Field(..., min_length=1)


class SuccessfulSolution(BaseModel):
    steps: List[str] = Field(default_factory=list)
    verification: str = ""


class ExperienceRecord(BaseModel):
    memory_type: Literal["experience"] = "experience"
    summary: str = Field(..., min_length=1)
    task_goal: str = Field(..., min_length=1)
    constraints: List[str] = Field(default_factory=list)
    successful_solution: SuccessfulSolution = Field(default_factory=SuccessfulSolution)
    failed_attempts: List[FailedAttempt] = Field(default_factory=list)
    applicable_scope: str = Field(..., min_length=1)
    exclude_scope: str = Field(..., min_length=1)
    tags: List[str] = Field(...)
    supersedes: str = ""
    source_agent: str = Field(..., min_length=1)
    confidence: float = Field(..., ge=0.0, le=1.0)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, tags: List[str]) -> List[str]:
        return _normalize_tags(tags, required=True)


class CreateExperienceRequest(BaseModel):
    project_id: str = Field(..., min_length=1)
    record: ExperienceRecord


class CreateResponse(BaseModel):
    id: str
    supersedes: str = ""
    deprecated_target: Optional[str] = None


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    project_id: str = Field(..., min_length=1)
    tags: List[str] = Field(default_factory=list)
    memory_type: Optional[str] = None
    top_k: int = Field(5, ge=1, le=50)
    threshold: float = Field(0.1, ge=0.0, le=1.0)
    include_deprecated: bool = False

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, tags: List[str]) -> List[str]:
        return _normalize_tags(tags, required=False)


class ExperienceResult(BaseModel):
    id: str
    score: Optional[float] = None
    project_id: Optional[str] = None
    deprecated: bool = False
    superseded_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    record: ExperienceRecord


class DeprecateRequest(BaseModel):
    project_id: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)
    superseded_by: str = ""


class DedupRequest(BaseModel):
    query: str = Field(..., min_length=1)
    project_id: str = Field(..., min_length=1)
    tags: List[str] = Field(default_factory=list)
    top_k: int = Field(5, ge=1, le=20)
    threshold: float = Field(0.85, ge=0.0, le=1.0)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, tags: List[str]) -> List[str]:
        return _normalize_tags(tags, required=False)


class DedupCandidate(BaseModel):
    id: str
    score: Optional[float] = None
    summary: str
    tags: List[str]


class RawSearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    project_id: str = Field(..., min_length=1)
    filters: Optional[Dict[str, Any]] = None
    top_k: int = Field(5, ge=1, le=100)
    threshold: float = Field(0.1, ge=0.0, le=1.0)
