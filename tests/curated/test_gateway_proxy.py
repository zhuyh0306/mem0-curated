import httpx
import pytest
from fastapi.testclient import TestClient

from curated.gateway import main as gateway

WORKER_KEY = "wk-test"
CURATOR_KEY = "cu-test"
INTERNAL = "shared-secret"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WORKER_API_KEYS", WORKER_KEY)
    monkeypatch.setenv("CURATOR_API_KEYS", CURATOR_KEY)
    monkeypatch.setattr(gateway, "UPSTREAM_URL", "http://backend.test")
    monkeypatch.setattr(gateway, "INTERNAL_TOKEN", INTERNAL)
    gateway._client = None
    yield
    gateway._client = None


@pytest.fixture
def client():
    return TestClient(gateway.app)


def mock_upstream(handler):
    """Install a mock transport. Request building, header stripping and URL joining stay real."""
    gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_worker_request_is_forwarded_with_the_internal_token(client):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["headers"] = request.headers
        seen["body"] = request.content
        return httpx.Response(200, json={"results": []})

    mock_upstream(handler)
    response = client.post(
        "/v1/experiences/search",
        json={"query": "kingbase8", "project_id": "proj-a"},
        headers={"X-API-Key": WORKER_KEY},
    )

    assert response.status_code == 200
    assert seen["url"] == "http://backend.test/v1/experiences/search"
    assert seen["method"] == "POST"
    assert seen["headers"]["x-internal-token"] == INTERNAL
    assert "x-api-key" not in seen["headers"]
    assert b"kingbase8" in seen["body"]


def test_query_parameters_reach_the_upstream(client):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = dict(request.url.params)
        return httpx.Response(200, json={"id": "m-1"})

    mock_upstream(handler)
    client.get("/v1/experiences/m-1", params={"project_id": "proj-a"}, headers={"X-API-Key": WORKER_KEY})

    assert captured["query"] == {"project_id": "proj-a"}


def test_upstream_status_and_body_pass_through(client):
    mock_upstream(lambda request: httpx.Response(422, json={"detail": "record.confidence too high"}))
    response = client.post("/v1/experiences", json={"project_id": "proj-a"}, headers={"X-API-Key": CURATOR_KEY})

    assert response.status_code == 422
    assert response.json() == {"detail": "record.confidence too high"}


def test_unreachable_upstream_is_a_clean_502(client):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("all connection attempts failed", request=request)

    mock_upstream(handler)
    response = client.post(
        "/v1/experiences/search", json={"query": "x", "project_id": "p"}, headers={"X-API-Key": WORKER_KEY}
    )

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "cannot reach the memory backend" in detail
    assert "http://backend.test" in detail


def test_upstream_timeout_is_504(client):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    mock_upstream(handler)
    response = client.post(
        "/v1/experiences/search", json={"query": "x", "project_id": "p"}, headers={"X-API-Key": WORKER_KEY}
    )

    assert response.status_code == 504


def test_denied_request_never_reaches_the_upstream(client):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    mock_upstream(handler)
    response = client.post("/v1/experiences", json={"project_id": "p"}, headers={"X-API-Key": WORKER_KEY})

    assert response.status_code == 403
    assert calls == []


def test_unknown_key_is_rejected_before_proxying(client):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    mock_upstream(handler)
    assert client.get("/v1/experiences/m-1", headers={"X-API-Key": "nope"}).status_code == 401
    assert calls == []


def test_health_needs_no_key_and_no_upstream(client):
    assert client.get("/health").status_code == 200
