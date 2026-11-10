import pytest

from curated.gateway import auth, policy


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("WORKER_API_KEYS", "wk-test,wk-test-2")
    monkeypatch.setenv("CURATOR_API_KEYS", "cu-test")


def test_resolve_role_maps_keys() -> None:
    assert auth.resolve_role("wk-test") == "worker"
    assert auth.resolve_role("cu-test") == "curator"
    assert auth.resolve_role("nope") is None
    assert auth.resolve_role("") is None


def test_key_listed_in_both_degrades_to_worker(monkeypatch) -> None:
    monkeypatch.setenv("WORKER_API_KEYS", "shared-key")
    monkeypatch.setenv("CURATOR_API_KEYS", "shared-key")

    assert auth.resolve_role("shared-key") == "worker"


def test_worker_cannot_write_or_deprecate() -> None:
    assert policy.is_allowed("worker", "POST", "/v1/experiences") is False
    assert policy.is_allowed("worker", "POST", "/v1/experiences/abc/deprecate") is False
    assert policy.is_allowed("worker", "DELETE", "/v1/experiences/abc") is False


def test_worker_can_read_and_search() -> None:
    assert policy.is_allowed("worker", "POST", "/v1/experiences/search") is True
    assert policy.is_allowed("worker", "POST", "/v1/experiences/dedup-check") is True
    assert policy.is_allowed("worker", "GET", "/v1/experiences/abc") is True
    assert policy.is_allowed("worker", "GET", "/v1/projects") is True


def test_worker_projects_path_with_suffix_is_denied() -> None:
    assert policy.is_allowed("worker", "GET", "/v1/projects/extra") is False
    assert policy.is_allowed("worker", "POST", "/v1/projects") is False


def test_worker_search_path_with_suffix_is_denied() -> None:
    assert policy.is_allowed("worker", "POST", "/v1/experiences/search/extra") is False
    assert policy.is_allowed("worker", "POST", "/v1/experiences/search/..%2f..") is False


def test_curator_can_write_but_never_delete() -> None:
    assert policy.is_allowed("curator", "POST", "/v1/experiences") is True
    assert policy.is_allowed("curator", "POST", "/v1/experiences/abc/deprecate") is True
    assert policy.is_allowed("curator", "DELETE", "/v1/experiences/abc") is False
    assert policy.is_allowed("curator", "PUT", "/v1/experiences/abc") is False


def test_unknown_role_is_denied() -> None:
    assert policy.is_allowed("intruder", "GET", "/v1/experiences/abc") is False
