import logging
from typing import Dict

from mem0 import Memory

from curated.app.config import build_memory_config

logger = logging.getLogger(__name__)

# Qdrant only indexes user_id/agent_id/run_id/actor_id out of the box; these are the fields
# the curated filters and ranking actually use.
PAYLOAD_INDEXES: Dict[str, str] = {
    "tags": "keyword",
    "memory_type": "keyword",
    "deprecated": "bool",
    "confidence": "float",
    "superseded_by": "keyword",
}


def _ensure_payload_indexes(memory: Memory) -> None:
    store = memory.vector_store
    client = getattr(store, "client", None)
    if client is None or getattr(store, "is_local", True):
        return
    for field_name, field_schema in PAYLOAD_INDEXES.items():
        try:
            client.create_payload_index(
                collection_name=memory.collection_name,
                field_name=field_name,
                field_schema=field_schema,
            )
        except Exception as exc:
            logger.debug("payload index for %s already present or unavailable: %s", field_name, exc)


def _verify_embedding_dims(memory: Memory) -> None:
    expected = memory.vector_store.embedding_model_dims
    actual = len(memory.embedding_model.embed("dimension probe"))
    if actual != expected:
        raise RuntimeError(
            f"embedding dims mismatch: collection expects {expected}, embedder returned {actual}. "
            "Set EMBEDDER_DIMS to match EMBEDDER_MODEL."
        )


def build_memory() -> Memory:
    memory = Memory(build_memory_config())
    _verify_embedding_dims(memory)
    _ensure_payload_indexes(memory)
    return memory
