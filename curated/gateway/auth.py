import os
import secrets
from typing import List, Optional

ROLE_WORKER = "worker"
ROLE_CURATOR = "curator"


def _parse_keys(raw: str) -> List[str]:
    return [key.strip() for key in raw.split(",") if key.strip()]


def load_role_keys() -> dict:
    """Read keys on every call so a restarted container picks up .env changes without a rebuild."""
    return {
        ROLE_CURATOR: _parse_keys(os.environ.get("CURATOR_API_KEYS", "")),
        ROLE_WORKER: _parse_keys(os.environ.get("WORKER_API_KEYS", "")),
    }


def resolve_role(api_key: str) -> Optional[str]:
    if not api_key:
        return None
    keys = load_role_keys()
    # Worker first: if the same secret is pasted into both lists, the caller degrades to
    # read-only instead of silently gaining write access.
    for role in (ROLE_WORKER, ROLE_CURATOR):
        for candidate in keys[role]:
            if secrets.compare_digest(api_key, candidate):
                return role
    return None
