import json

import httpx
import pytest
from fastapi.testclient import TestClient

from curated.dashboard import main as dashboard

CURATOR_KEY = "cu-test"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(dashboard, "API_KEY", CURATOR_KEY)
    monkeypatch.setattr(dashboard, "GATEWAY_URL", "http://gateway.test")
    monkeypatch.setattr(dashboard, "DEFAULT_PROJECT", "proj-a")
    dashboard._client = None
    yield
    dashboard._client = None


@pytest.fixture
def client():
    return TestClient(dashboard.app)


def mock_gateway(handler):
    dashboard._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_key_falls_back_to_first_of_the_configured_list(monkeypatch) -> None:
    monkeypatch.delenv("CURATED_API_KEY", raising=False)
    monkeypatch.setenv("CURATED_API_KEYS", "first-key, second-key")

    assert dashboard._load_key() == "first-key"


def test_records_are_listed_through_the_gateway_with_the_curator_key(client) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-api-key")
        return httpx.Response(200, json=[{"id": "1", "deprecated": False}])

    mock_gateway(handler)
    response = client.get("/api/records", params={"project_id": "proj-a", "include_deprecated": "true"})

    assert response.status_code == 200
    assert response.json() == [{"id": "1", "deprecated": False}]
    assert "project_id=proj-a" in seen["url"]
    assert "include_deprecated=true" in seen["url"]
    assert seen["key"] == CURATOR_KEY


def test_projects_are_listed_through_the_gateway_with_the_curator_key(client) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["key"] = request.headers.get("x-api-key")
        return httpx.Response(200, json=["proj-a", "proj-b"])

    mock_gateway(handler)
    response = client.get("/api/projects")

    assert response.status_code == 200
    assert response.json() == ["proj-a", "proj-b"]
    assert seen["path"] == "/v1/projects"
    assert seen["key"] == CURATOR_KEY


def test_create_forwards_the_record_body(client) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "new-id"})

    mock_gateway(handler)
    body = {"project_id": "proj-a", "record": {"summary": "s"}}
    response = client.post("/api/records", json=body, headers={"Origin": "http://testserver"})

    assert response.status_code == 200
    assert response.json() == {"id": "new-id"}
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/experiences"
    assert seen["body"] == body


def test_cross_origin_write_is_refused_before_reaching_the_gateway(client) -> None:
    called = {"gateway": False}

    def handler(request: httpx.Request) -> httpx.Response:
        called["gateway"] = True
        return httpx.Response(200, json={"id": "should-not-happen"})

    mock_gateway(handler)
    response = client.post(
        "/api/records",
        json={"project_id": "proj-a", "record": {}},
        headers={"Origin": "http://attacker.test"},
    )

    assert response.status_code == 403
    assert called["gateway"] is False


def test_upstream_refusal_keeps_its_status_and_detail(client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "role 'worker' is not allowed"})

    mock_gateway(handler)
    response = client.post(
        "/api/records",
        json={"project_id": "proj-a", "record": {}},
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "role 'worker' is not allowed"


def test_unreachable_gateway_becomes_502(client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    mock_gateway(handler)
    response = client.get("/api/records", params={"project_id": "proj-a"})

    assert response.status_code == 502
    assert "cannot reach the gateway" in response.json()["detail"]


def test_gateway_timeout_becomes_504(client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    mock_gateway(handler)
    response = client.get("/api/records", params={"project_id": "proj-a"})

    assert response.status_code == 504


def test_index_is_served(client) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "curated" in response.text
