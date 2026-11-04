"""Thin client for the mem0-curated gateway.

Run: GATEWAY_URL=http://localhost:8787 CURATOR_KEY=... WORKER_KEY=... python curated/examples/python_client.py
"""

import os
from typing import Any, Dict, List, Optional

import httpx

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8787")
CURATOR_KEY = os.environ.get("CURATOR_KEY", "")
WORKER_KEY = os.environ.get("WORKER_KEY", "")
PROJECT_ID = os.environ.get("PROJECT_ID", "proj-a")


def _post(path: str, api_key: str, payload: Dict[str, Any]) -> Any:
    response = httpx.post(f"{GATEWAY_URL}{path}", json=payload, headers={"X-API-Key": api_key}, timeout=60.0)
    response.raise_for_status()
    return response.json()


def _get(path: str, api_key: str, params: Optional[Dict[str, Any]] = None) -> Any:
    response = httpx.get(f"{GATEWAY_URL}{path}", params=params, headers={"X-API-Key": api_key}, timeout=60.0)
    response.raise_for_status()
    return response.json()


def search(api_key: str, query: str, tags: Optional[List[str]] = None, top_k: int = 5) -> List[Dict[str, Any]]:
    """Worker-side read. The gateway allows this for both roles."""
    return _post(
        "/v1/experiences/search",
        api_key,
        {"query": query, "project_id": PROJECT_ID, "tags": tags or [], "top_k": top_k},
    )


def dedup_check(api_key: str, query: str, tags: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    return _post("/v1/experiences/dedup-check", api_key, {"query": query, "project_id": PROJECT_ID, "tags": tags or []})


def create(api_key: str, record: Dict[str, Any]) -> Dict[str, Any]:
    """Curator-side write. Returns 403 if the key belongs to a Worker."""
    return _post("/v1/experiences", api_key, {"project_id": PROJECT_ID, "record": record})


def deprecate(api_key: str, memory_id: str, reason: str, superseded_by: str = "") -> Dict[str, Any]:
    return _post(
        f"/v1/experiences/{memory_id}/deprecate",
        api_key,
        {"project_id": PROJECT_ID, "reason": reason, "superseded_by": superseded_by},
    )


def get_experience(api_key: str, memory_id: str) -> Dict[str, Any]:
    return _get(f"/v1/experiences/{memory_id}", api_key, {"project_id": PROJECT_ID})


SAMPLE_RECORD = {
    "memory_type": "experience",
    "summary": "Qdrant payload 过滤漏掉缺少字段的点，deprecated 必须在插入时显式写 false",
    "task_goal": "让已废弃经验从检索结果中消失",
    "constraints": ["qdrant 1.12", "mem0 filters ne/must_not"],
    "successful_solution": {
        "steps": ["插入时 metadata 固定写入 deprecated=false", "检索默认加 filters {'deprecated': false}"],
        "verification": "插入两条、废弃其中一条后检索，只返回未废弃的那条",
    },
    "failed_attempts": [
        {
            "try_content": "只在废弃时写入 deprecated=true，不写 false",
            "root_cause": "Qdrant 的 must_not / ne 对缺少该字段的点不匹配，未废弃的点被一起过滤或一起漏出",
            "lesson": "布尔标记字段必须有显式默认值，不能依赖字段缺失表达 false",
        }
    ],
    "applicable_scope": "Qdrant 作为向量库、用 metadata 布尔位做软删除时",
    "exclude_scope": "使用 expiration_date 做时间型失效时；或向量库支持 exists 过滤时",
    "tags": ["qdrant", "mem0", "metadata-filter"],
    "supersedes": "",
    "source_agent": "worker-coding-1",
    "confidence": 0.9,
}


def main() -> None:
    if not CURATOR_KEY or not WORKER_KEY:
        raise SystemExit("Set CURATOR_KEY and WORKER_KEY first.")

    print("dedup-check:", dedup_check(CURATOR_KEY, SAMPLE_RECORD["summary"]))

    first = create(CURATOR_KEY, SAMPLE_RECORD)
    print("created:", first)

    print("worker search:", search(WORKER_KEY, "deprecated 标记 过滤 qdrant"))

    revised = dict(
        SAMPLE_RECORD, summary=SAMPLE_RECORD["summary"] + "（已按 Qdrant 1.13 更新）", supersedes=first["id"]
    )
    second = create(CURATOR_KEY, revised)
    print("superseding record:", second)

    print("after supersede, worker search:", search(WORKER_KEY, "deprecated 标记 过滤 qdrant"))

    try:
        create(WORKER_KEY, SAMPLE_RECORD)
    except httpx.HTTPStatusError as exc:
        print("worker write blocked as expected:", exc.response.status_code, exc.response.text)


if __name__ == "__main__":
    main()
