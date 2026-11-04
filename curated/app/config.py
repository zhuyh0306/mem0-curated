import os
from typing import Any, Dict

from mem0.configs.base import MemoryConfig

DEFAULT_QDRANT_HOST = "qdrant"
DEFAULT_QDRANT_PORT = 6333
DEFAULT_COLLECTION = "experiences"
DEFAULT_EMBEDDER_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_EMBEDDER_DIMS = 384
DEFAULT_HISTORY_DB_PATH = "/data/history/history.db"

# Memory.__init__ always builds an LLM, but no code path invokes it: every write goes through
# add(infer=False) and search only embeds. Keeping a placeholder avoids requiring a paid key.
PLACEHOLDER_LLM_KEY = "unused-infer-false"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    return int(raw)


def build_memory_config() -> MemoryConfig:
    embedder_provider = os.environ.get("EMBEDDER_PROVIDER", "fastembed")
    embedding_dims = _env_int("EMBEDDER_DIMS", DEFAULT_EMBEDDER_DIMS)
    embedder_config: Dict[str, Any] = {
        "model": os.environ.get("EMBEDDER_MODEL", DEFAULT_EMBEDDER_MODEL),
        "embedding_dims": embedding_dims,
    }
    if embedder_provider == "openai":
        embedder_config["api_key"] = os.environ.get("OPENAI_API_KEY", "")
        embedder_config.pop("embedding_dims", None)

    qdrant_config: Dict[str, Any] = {
        "host": os.environ.get("QDRANT_HOST", DEFAULT_QDRANT_HOST),
        "port": _env_int("QDRANT_PORT", DEFAULT_QDRANT_PORT),
        "collection_name": os.environ.get("QDRANT_COLLECTION", DEFAULT_COLLECTION),
        # mem0 does not sync embedder output dims into the vector store config, so this must
        # be supplied explicitly and match EMBEDDER_MODEL.
        "embedding_model_dims": embedding_dims,
    }

    return MemoryConfig(
        vector_store={"provider": "qdrant", "config": qdrant_config},
        llm={
            "provider": "openai",
            "config": {
                "api_key": os.environ.get("LLM_API_KEY", PLACEHOLDER_LLM_KEY),
                "model": os.environ.get("LLM_MODEL", "gpt-5-mini"),
            },
        },
        embedder={"provider": embedder_provider, "config": embedder_config},
        history_db_path=os.environ.get("HISTORY_DB_PATH", DEFAULT_HISTORY_DB_PATH),
    )
