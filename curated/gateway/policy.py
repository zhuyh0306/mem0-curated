import re
from typing import Dict, List, Pattern, Tuple

from curated.gateway.auth import ROLE_CURATOR, ROLE_WORKER

Rule = Tuple[str, Pattern[str]]

# Allowlist, not denylist: anything not listed here is refused for that role. That is what makes
# "a Worker can never write" a property of the code rather than a property of today's route table.
_WORKER_RULES: List[Rule] = [
    ("GET", re.compile(r"^/health$")),
    ("GET", re.compile(r"^/v1/projects$")),
    ("GET", re.compile(r"^/v1/experiences/[^/]+$")),
    ("POST", re.compile(r"^/v1/experiences/search$")),
    ("POST", re.compile(r"^/v1/experiences/dedup-check$")),
    ("GET", re.compile(r"^/mem0/memories$")),
    ("POST", re.compile(r"^/mem0/search$")),
]

# Curator writes and deprecates, but never deletes: removal would break the supersedes chain that
# makes old guidance auditable.
_CURATOR_RULES: List[Rule] = [
    ("GET", re.compile(r"^/health$")),
    ("GET", re.compile(r"^/v1/.*")),
    ("POST", re.compile(r"^/v1/.*")),
    ("GET", re.compile(r"^/mem0/.*")),
    ("POST", re.compile(r"^/mem0/.*")),
]

_POLICY: Dict[str, List[Rule]] = {
    ROLE_WORKER: _WORKER_RULES,
    ROLE_CURATOR: _CURATOR_RULES,
}


def is_allowed(role: str, method: str, path: str) -> bool:
    rules = _POLICY.get(role)
    if rules is None:
        return False
    upper_method = method.upper()
    return any(upper_method == allowed_method and pattern.match(path) for allowed_method, pattern in rules)


def denied_reason(role: str, method: str, path: str) -> str:
    return f"role '{role}' is not allowed to {method.upper()} {path}"
