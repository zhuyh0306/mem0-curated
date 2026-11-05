from typing import Any, Dict

import pytest
from mem0 import Memory
from mem0.configs.base import MemoryConfig
from pydantic import ValidationError

from curated.app.models import DedupRequest, ExperienceRecord, SearchRequest
from curated.app.service import ExperienceService

EMBED_DIMS = 8


class StubEmbeddings:
    """Provider-boundary stub: Memory and Qdrant stay real, only the embedder is faked."""

    def embed(self, text, memory_action=None):
        return [0.1] * EMBED_DIMS

    def embed_batch(self, texts, memory_action=None):
        return [[0.1] * EMBED_DIMS for _ in texts]


@pytest.fixture()
def service(tmp_path) -> ExperienceService:
    config = MemoryConfig(
        vector_store={
            "provider": "qdrant",
            "config": {
                "collection_name": "experiences",
                "path": str(tmp_path / "qdrant"),
                "embedding_model_dims": EMBED_DIMS,
            },
        },
        llm={"provider": "openai", "config": {"api_key": "test", "model": "gpt-5-mini"}},
        embedder={"provider": "openai", "config": {"api_key": "test", "model": "text-embedding-3-small"}},
        history_db_path=str(tmp_path / "history.db"),
    )
    memory = Memory(config)
    memory.embedding_model = StubEmbeddings()
    return ExperienceService(memory)


def make_record(**overrides: Any) -> ExperienceRecord:
    payload: Dict[str, Any] = {
        "summary": "deprecated must be written explicitly on insert",
        "task_goal": "hide deprecated experiences from search",
        "constraints": ["qdrant 1.12"],
        "successful_solution": {"steps": ["write deprecated=false"], "verification": "search hides it"},
        "failed_attempts": [
            {
                "try_content": "only write deprecated=true on deprecate",
                "root_cause": "must_not does not match points missing the field",
                "lesson": "boolean flags need an explicit default",
            }
        ],
        "applicable_scope": "qdrant remote with metadata filters",
        "exclude_scope": "expiration_date based invalidation",
        "tags": ["qdrant", "metadata-filter"],
        "source_agent": "worker-coding-1",
        "confidence": 0.9,
    }
    payload.update(overrides)
    return ExperienceRecord(**payload)


def test_create_stores_record_and_hides_nothing(service: ExperienceService) -> None:
    created = service.create(make_record(), "proj-a")

    results = service.search(SearchRequest(query="deprecated flag", project_id="proj-a"))
    assert [result.id for result in results] == [created["id"]]

    stored = service.get(created["id"], "proj-a")
    assert stored.deprecated is False
    assert stored.record.tags == ["qdrant", "metadata-filter"]
    assert stored.record.confidence == 0.9


def test_supersede_deprecates_old_and_removes_it_from_search(service: ExperienceService) -> None:
    old = service.create(make_record(), "proj-a")
    new = service.create(
        make_record(summary="deprecated written explicitly, confirmed on qdrant 1.13", supersedes=old["id"]),
        "proj-a",
    )

    old_stored = service.get(old["id"], "proj-a")
    assert old_stored.deprecated is True
    assert old_stored.superseded_by == new["id"]

    visible = service.search(SearchRequest(query="deprecated flag", project_id="proj-a"))
    assert [result.id for result in visible] == [new["id"]]

    with_deprecated = service.search(
        SearchRequest(query="deprecated flag", project_id="proj-a", include_deprecated=True)
    )
    assert {result.id for result in with_deprecated} == {old["id"], new["id"]}


def test_supersede_across_projects_is_rejected(service: ExperienceService) -> None:
    foreign = service.create(make_record(), "proj-b")

    with pytest.raises(Exception) as excinfo:
        service.create(make_record(supersedes=foreign["id"]), "proj-a")

    assert "not found in project proj-a" in str(excinfo.value)
    assert service.get(foreign["id"], "proj-b").deprecated is False


def test_unknown_memory_is_404(service: ExperienceService) -> None:
    with pytest.raises(Exception) as excinfo:
        service.get("does-not-exist", "proj-a")

    assert "not found in project proj-a" in str(excinfo.value)


def test_deprecate_marks_record(service: ExperienceService) -> None:
    created = service.create(make_record(), "proj-a")

    deprecated = service.deprecate(created["id"], "proj-a", reason="wrong for this stack")

    assert deprecated.deprecated is True
    assert service.search(SearchRequest(query="deprecated flag", project_id="proj-a")) == []


def test_tags_are_lowercased_and_deduplicated() -> None:
    record = make_record(tags=["Qdrant", "qdrant", " Metadata-Filter "])

    assert record.tags == ["qdrant", "metadata-filter"]


def test_tags_must_not_be_empty() -> None:
    with pytest.raises(ValidationError):
        make_record(tags=[])


def test_blank_tag_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_record(tags=["  "])


def test_confidence_out_of_range_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_record(confidence=1.4)


def test_blank_scope_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_record(applicable_scope="")


def test_dedup_check_returns_existing_summary(service: ExperienceService) -> None:
    created = service.create(make_record(), "proj-a")

    candidates = service.dedup_check(
        DedupRequest(query="deprecated must be written explicitly on insert", project_id="proj-a")
    )

    assert [candidate.id for candidate in candidates] == [created["id"]]
    assert candidates[0].summary == make_record().summary


def test_list_experiences_filters_tags_and_deprecated(service: ExperienceService) -> None:
    first = service.create(make_record(), "proj-a")
    second = service.create(make_record(summary="second", tags=["other"]), "proj-a")
    retired = service.create(make_record(summary="retired", tags=["legacy"]), "proj-a")
    service.deprecate(retired["id"], "proj-a", "superseded by a narrower rule")

    active = service.list_experiences("proj-a", [], False, 100)
    assert {result.id for result in active} == {first["id"], second["id"]}

    everything = service.list_experiences("proj-a", [], True, 100)
    assert {result.id for result in everything} == {first["id"], second["id"], retired["id"]}

    tagged = service.list_experiences("proj-a", ["legacy"], True, 100)
    assert [result.id for result in tagged] == [retired["id"]]


def test_list_experiences_is_scoped_to_one_project(service: ExperienceService) -> None:
    service.create(make_record(), "proj-a")
    service.create(make_record(summary="only in b"), "proj-b")

    results = service.list_experiences("proj-b", [], True, 100)

    assert [result.record.summary for result in results] == ["only in b"]
