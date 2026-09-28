"""Entry-adapter auth tests for src/api/server.py (_ctx auth core).

Verifies the fail-closed entry authentication behavior:
  - no verified trust and no INVOKE_AUTH_TOKEN promotion  → 401
  - INVOKE_AUTH_TOKEN Bearer mismatch                     → 401
  - INVOKE_AUTH_TOKEN Bearer match (ANONYMOUS → VERIFIED) → invoke reached

Skipped when the framework wheel (or FastAPI TestClient) is not installed, e.g.
local dev without the AgentCore registry — CI installs the wheel and runs these.
"""

from __future__ import annotations

import pytest

pytest.importorskip("framework", reason="AgentCore framework wheel not installed")
pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "test-entry-token")
    # Import after the env var is set so the module picks up a stable app object.
    from src.api import server

    # Neutralize the real graph so a promoted call does not run business logic.
    monkeypatch.setattr(server.agent, "invoke", lambda *a, **k: {"status": "success"})
    return TestClient(server.app)


def test_unauthenticated_invoke_is_rejected(client):
    resp = client.post("/invoke", json={"input": "who are our competitors?"})
    assert resp.status_code == 401


def test_bearer_token_mismatch_is_rejected(client):
    resp = client.post(
        "/invoke",
        json={"input": "who are our competitors?"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


def test_bearer_token_match_reaches_invoke(client):
    resp = client.post(
        "/invoke",
        json={"input": "who are our competitors?"},
        headers={"Authorization": "Bearer test-entry-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"


def test_health_needs_no_auth(client):
    assert client.get("/health").status_code == 200
