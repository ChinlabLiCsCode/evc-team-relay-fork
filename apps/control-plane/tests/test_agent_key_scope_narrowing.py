"""Regression tests for task 6f004379: POST .../agent-keys silently ignored an
unrecognised request field and fell back to the model default (`["read",
"write"]`) instead of rejecting the request — so a caller who got the scope
field name wrong (`"scope": "read"` instead of the real `scopes: list[str]`)
walked away believing it had minted a read-only key when it had actually
minted a full read+write one.

Root cause (found via live repro, not guessed): `AgentKeyCreateRequest` had no
`model_config`, so pydantic's default `extra="ignore"` silently dropped the
unrecognised field(s) and the endpoint proceeded with the field's default.
Confirmed by reproducing BOTH sides with the real endpoint before touching any
code:
  - POST {"name": "...", "scope": "read"}   -> 201, scopes=["read","write"]
  - POST {"scopes": ["read"]}  (real field) -> 201, scopes=["read"]        (already worked)

Fix: `model_config = ConfigDict(extra="forbid")` on `AgentKeyCreateRequest`
(and, for the same reason, `AgentKeyScopesUpdateRequest`) turns the first case
into a 422 instead of a silent widen.

Already-issued keys: the two keys created via the broken repro during the
original finding are revoked (task 6f004379 description). No DB migration is
possible or needed beyond that — a request using the correct `scopes` field
already produced the caller's intended value (second repro above), and the
model's default of `["read", "write"]` is itself the deliberate ADR-0001
default for a request that omits `scopes` legitimately, so a stored row of
`scopes="read,write"` cannot be distinguished after the fact from "widened by
this bug" vs. "the caller genuinely wanted the default". There is nothing to
migrate.
"""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tests.test_agent_keys import auth_headers, login


def create_folder_share(client: TestClient, token: str, path: str) -> str:
    resp = client.post(
        "/shares",
        json={"kind": "folder", "path": path, "visibility": "private"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def key_headers(raw_key: str, **extra: str) -> dict[str, str]:
    return {"X-Agent-Key": raw_key, **extra}


def err_message(resp) -> str:
    """The API wraps errors as {"error": {"code", "message"}} (middleware/errors.py)."""
    body = resp.json()
    if isinstance(body, dict) and "error" in body:
        return str(body["error"].get("message", ""))
    return str(body.get("detail", ""))


@pytest.fixture
def minio():
    """Patch the MinIO client so a real write can complete without a real bucket."""
    mock_client = MagicMock()
    mock_client.bucket_exists.return_value = True
    mock_client.put_object.return_value = None
    with patch("app.api.routers.shares._get_minio_client", return_value=mock_client):
        yield mock_client


# ---------------------------------------------------------------------------
# 1. The bug itself: an unrecognised field must be REJECTED, not ignored.
# ---------------------------------------------------------------------------


def test_create_agent_key_rejects_unrecognised_scope_field(client: TestClient) -> None:
    """The exact repro from task 6f004379: wrong field names ("name", "scope"
    instead of "label", "scopes") used to return 201 with scopes=["read","write"]
    — a silent widen of whatever the caller actually intended. Must now 422.
    """
    token = login(client, "bootstrap@example.com", "super-secret")
    share_id = create_folder_share(client, token, "vault/scope-repro.md")

    resp = client.post(
        f"/v1/web/shares/{share_id}/agent-keys",
        json={"name": "leaky-key", "scope": "read"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 422, resp.text

    # And no key was actually created with the silently-widened default.
    list_resp = client.get(
        f"/v1/web/shares/{share_id}/agent-keys",
        headers=auth_headers(token),
    )
    assert list_resp.json() == []


def test_update_agent_key_scopes_rejects_unrecognised_field(client: TestClient) -> None:
    """Same defect class on PATCH .../agent-keys/{key_id} — belt and suspenders.

    `scopes` here has no default (it's a required field), so an omitted-field
    typo already 422s on its own; the case that actually exercises
    `extra="forbid"` is a decoy field ALONGSIDE a valid `scopes`, which pydantic
    would otherwise silently drop while applying the real `scopes` value as-is.
    """
    token = login(client, "bootstrap@example.com", "super-secret")
    share_id = create_folder_share(client, token, "vault/scope-repro-patch.md")

    create_resp = client.post(
        f"/v1/web/shares/{share_id}/agent-keys",
        json={"label": "to-patch", "scopes": ["write"]},
        headers=auth_headers(token),
    )
    assert create_resp.status_code == 201, create_resp.text
    key_id = create_resp.json()["id"]

    resp = client.patch(
        f"/v1/web/shares/{share_id}/agent-keys/{key_id}",
        json={"scopes": ["read"], "scope": "read"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# 2 & 3. End-to-end, real field name: the resulting key is genuinely narrowed
#    — positive AND negative control, both through the real endpoints (not a
#    DB-direct key), so the whole platform path is proven, not just the model.
# ---------------------------------------------------------------------------


def test_api_created_read_only_key_reads_but_cannot_write(client: TestClient) -> None:
    token = login(client, "bootstrap@example.com", "super-secret")
    share_id = create_folder_share(client, token, "vault/scope-e2e-read.md")

    create_resp = client.post(
        f"/v1/web/shares/{share_id}/agent-keys",
        json={"label": "read-only", "scopes": ["read"]},
        headers=auth_headers(token),
    )
    assert create_resp.status_code == 201, create_resp.text
    body = create_resp.json()
    assert body["scopes"] == ["read"]
    raw_key = body["key"]

    # Positive control: the read-only key can still read. Without this, a 403
    # on the write attempt below would be indistinguishable from a dead key.
    index_resp = client.get(f"/v1/shares/{share_id}/files-index", headers=key_headers(raw_key))
    assert index_resp.status_code == 200, index_resp.text

    # Negative control: it physically cannot write.
    write_resp = client.put(
        f"/v1/shares/{share_id}/sync-write",
        params={"path": "notes/blocked.md"},
        content=b"should not land",
        headers=key_headers(raw_key, **{"Content-Type": "text/markdown", "If-None-Match": "*"}),
    )
    assert write_resp.status_code == 403, write_resp.text
    assert "write scope" in err_message(write_resp)


def test_api_created_write_scoped_key_can_write(client: TestClient, minio) -> None:
    """Same path, opposite scope — proves the fix does not become 'deny everyone'."""
    token = login(client, "bootstrap@example.com", "super-secret")
    share_id = create_folder_share(client, token, "vault/scope-e2e-write.md")

    create_resp = client.post(
        f"/v1/web/shares/{share_id}/agent-keys",
        json={"label": "write-scoped", "scopes": ["write"]},
        headers=auth_headers(token),
    )
    assert create_resp.status_code == 201, create_resp.text
    assert create_resp.json()["scopes"] == ["write"]
    raw_key = create_resp.json()["key"]

    write_resp = client.put(
        f"/v1/shares/{share_id}/sync-write",
        params={"path": "notes/allowed.md"},
        content=b"lands fine",
        headers=key_headers(raw_key, **{"Content-Type": "text/markdown", "If-None-Match": "*"}),
    )
    assert write_resp.status_code == 200, write_resp.text
    assert write_resp.json()["sha256"] == hashlib.sha256(b"lands fine").hexdigest()
    minio.put_object.assert_called_once()
