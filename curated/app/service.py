from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from mem0 import Memory

from curated.app.models import (
    DedupCandidate,
    DedupRequest,
    ExperienceRecord,
    ExperienceResult,
    RawSearchRequest,
    SearchRequest,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_embedding_text(record: ExperienceRecord) -> str:
    parts = [
        record.summary,
        record.task_goal,
        "Constraints: " + "; ".join(record.constraints) if record.constraints else "",
        "Applicable: " + record.applicable_scope,
        "Tags: " + ", ".join(record.tags),
    ]
    return "\n".join(part for part in parts if part)


def _record_from_metadata(metadata: Dict[str, Any]) -> ExperienceRecord:
    return ExperienceRecord.model_validate(
        {
            "memory_type": metadata.get("memory_type", "experience"),
            "summary": metadata.get("summary", ""),
            "task_goal": metadata.get("task_goal", ""),
            "constraints": metadata.get("constraints", []),
            "successful_solution": metadata.get("successful_solution", {}),
            "failed_attempts": metadata.get("failed_attempts", []),
            "applicable_scope": metadata.get("applicable_scope", ""),
            "exclude_scope": metadata.get("exclude_scope", ""),
            "tags": metadata.get("tags", []),
            "supersedes": metadata.get("supersedes", ""),
            "source_agent": metadata.get("source_agent", ""),
            "confidence": metadata.get("confidence", 0.0),
        }
    )


def _to_result(item: Dict[str, Any], score: Optional[float] = None) -> ExperienceResult:
    metadata = item.get("metadata") or {}
    return ExperienceResult(
        id=item["id"],
        score=score if score is not None else item.get("score"),
        project_id=item.get("user_id"),
        deprecated=bool(metadata.get("deprecated", False)),
        superseded_by=metadata.get("superseded_by") or None,
        created_at=item.get("created_at"),
        updated_at=item.get("updated_at"),
        record=_record_from_metadata(metadata),
    )


class ExperienceService:
    def __init__(self, memory: Memory):
        self.memory = memory

    def create(self, record: ExperienceRecord, project_id: str) -> Dict[str, Any]:
        target = record.supersedes.strip()
        if target:
            self._get_scoped(target, project_id)

        # deprecated is written explicitly on every insert: Qdrant's `ne`/`must_not` matching
        # skips points that lack the field entirely, which would silently leak them into search.
        payload: Dict[str, Any] = record.model_dump()
        payload["deprecated"] = False
        payload["superseded_by"] = ""

        response = self.memory.add(
            messages=[{"role": "user", "content": build_embedding_text(record)}],
            user_id=project_id,
            metadata=payload,
            infer=False,
        )
        created_id = response["results"][0]["id"]

        if target:
            try:
                self.memory.update(
                    memory_id=target,
                    metadata={
                        "deprecated": True,
                        "deprecated_at": _now(),
                        "superseded_by": created_id,
                    },
                )
            except Exception:
                # Leaving the old record active while the new one exists would let both answer
                # queries with contradicting guidance, so drop the new one on failure.
                self.memory.delete(memory_id=created_id)
                raise

        return {"id": created_id, "supersedes": target, "deprecated_target": target or None}

    def search(self, request: SearchRequest) -> List[ExperienceResult]:
        results = self._raw_search(
            query=request.query,
            project_id=request.project_id,
            tags=request.tags,
            memory_type=request.memory_type,
            top_k=request.top_k,
            threshold=request.threshold,
            include_deprecated=request.include_deprecated,
        )
        results.sort(key=self._rank_key, reverse=True)
        return results[: request.top_k]

    def deprecate(self, memory_id: str, project_id: str, reason: str, superseded_by: str = "") -> ExperienceResult:
        self._get_scoped(memory_id, project_id)
        metadata: Dict[str, Any] = {
            "deprecated": True,
            "deprecated_at": _now(),
            "deprecated_reason": reason,
        }
        if superseded_by:
            self._get_scoped(superseded_by, project_id)
            metadata["superseded_by"] = superseded_by
        self.memory.update(memory_id=memory_id, metadata=metadata)
        return _to_result(self._get_scoped(memory_id, project_id))

    def dedup_check(self, request: DedupRequest) -> List[DedupCandidate]:
        results = self._raw_search(
            query=request.query,
            project_id=request.project_id,
            tags=request.tags,
            memory_type=None,
            top_k=request.top_k,
            threshold=request.threshold,
            include_deprecated=False,
        )
        results.sort(key=lambda result: result.score or 0.0, reverse=True)
        return [
            DedupCandidate(id=result.id, score=result.score, summary=result.record.summary, tags=result.record.tags)
            for result in results[: request.top_k]
        ]

    def raw_search(self, request: RawSearchRequest) -> Dict[str, Any]:
        filters = dict(request.filters or {})
        filters["user_id"] = request.project_id
        return self.memory.search(
            query=request.query,
            filters=filters,
            top_k=request.top_k,
            threshold=request.threshold,
        )

    def list_memories(self, project_id: str, top_k: int) -> Dict[str, Any]:
        return self.memory.get_all(filters={"user_id": project_id}, top_k=top_k)

    def get(self, memory_id: str, project_id: str) -> ExperienceResult:
        return _to_result(self._get_scoped(memory_id, project_id))

    @staticmethod
    def _rank_key(result: ExperienceResult) -> tuple:
        # Attenuate low-confidence memories without burying a highly relevant hit.
        weighted = (result.score or 0.0) * (0.5 + 0.5 * result.record.confidence)
        return (weighted, result.created_at or "")

    def _raw_search(
        self,
        *,
        query: str,
        project_id: str,
        tags: List[str],
        memory_type: Optional[str],
        top_k: int,
        threshold: float,
        include_deprecated: bool,
    ) -> List[ExperienceResult]:
        filters: Dict[str, Any] = {"user_id": project_id}
        if tags:
            filters["tags"] = {"in": tags}
        if memory_type:
            filters["memory_type"] = memory_type
        if not include_deprecated:
            filters["deprecated"] = False

        response = self.memory.search(
            query=query,
            filters=filters,
            top_k=max(top_k * 3, 10),
            threshold=threshold,
        )
        return [_to_result(item) for item in response.get("results", [])]

    def _get_scoped(self, memory_id: str, project_id: str) -> Dict[str, Any]:
        item = self.memory.get(memory_id)
        # 404 rather than 403 so a Worker cannot probe which ids exist in other projects.
        if not item or item.get("user_id") != project_id:
            raise HTTPException(status_code=404, detail=f"experience {memory_id} not found in project {project_id}")
        return item
