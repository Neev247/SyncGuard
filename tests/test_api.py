import hashlib
import json
from uuid import uuid4

import pytest


def test_public_docs_health_favicon_and_openapi(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/").url.path == "/docs"
    assert "/favicon.svg" in client.get("/docs").text
    assert "/favicon.svg" in client.get("/redoc").text
    assert client.get("/favicon.ico").headers["content-type"].startswith("image/svg+xml")
    schema = client.get("/openapi.json").json()
    assert schema["paths"]["/v1/sync"]["post"]["security"] == [{"HTTPBearer": []}]
    assert "409" in schema["paths"]["/v1/sync"]["post"]["responses"]
    assert client.delete("/v1/sync").status_code == 405
    assert client.get("/nonexistent").status_code == 404


def test_authentication_required(client):
    for header in (None, "Bearer invalid", "Basic something", "Bearer"):
        headers = {"Authorization": header} if header else {}
        response = client.get("/v1/documents", headers=headers)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.local_only(reason="Inspects token storage directly in SQLite.")
def test_only_token_hash_is_stored(client):
    user = client.post("/v1/users", json={"name": "User"}).json()
    with client.app.state.database.transaction() as connection:
        row = dict(connection.execute("SELECT * FROM users").fetchone())
    assert row["token_hash"] == hashlib.sha256(user["access_token"].encode()).hexdigest()
    assert user["access_token"] not in row.values()


@pytest.mark.local_only(reason="Checks the device row count directly in SQLite.")
def test_device_registration_is_idempotent(workspace):
    path = f"/v1/devices/{workspace.laptop}"
    first = workspace.client.put(path, json={"name": "Renamed"}).json()
    second = workspace.client.put(path, json={"name": "Renamed"}).json()
    assert first == second
    with workspace.client.app.state.database.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 2


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"title": ""},
        {"title": "   "},
        {"title": None},
        {"content": None},
        {"title": 123},
        {"archived": "true"},
        {"archived": 1},
        {"tags": "tag"},
        {"tags": ["duplicate", "duplicate"]},
        {"tags": ["  a", "a  "]},
        {"tags": [True]},
        {"tags": [str(i) for i in range(21)]},
        {"tags": ["x" * 41]},
        {"title": "x" * 201},
        {"content": "x" * 100_001},
        {"unknown": "field"},
        {"content": "\ud800"},
    ],
)
def test_invalid_patch_does_not_change_document(workspace, changes):
    workspace.edit()
    payload = workspace.payload(1, changes)
    response = workspace.client.post(
        "/v1/sync", content=json.dumps(payload), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert all("input" not in error for error in response.json()["errors"])
    assert workspace.read()["version"] == 1
    assert len(workspace.history()) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("base_version", -1),
        ("base_version", True),
        ("base_version", "1"),
        ("base_version", 1.0),
        ("base_version", 2_147_483_648),
        ("request_id", "bad-id"),
        ("device_id", None),
        ("document_id", "bad-id"),
        ("unexpected", "not allowed"),
    ],
)
def test_invalid_envelope(workspace, field, value):
    body = workspace.payload()
    body[field] = value
    response = workspace.client.post("/v1/sync", json=body)
    assert response.status_code == 422
    assert workspace.client.get("/v1/changes").json()["items"] == []


def test_malformed_json_and_body_limits(workspace):
    response = workspace.client.post(
        "/v1/sync", content="{", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    response = workspace.client.post(
        "/v1/sync", content=b"x" * 262_145, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    response = workspace.client.post(
        "/v1/sync",
        content=iter([b"x" * 131_073, b"x" * 131_073]),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert workspace.client.get("/v1/changes").json()["items"] == []


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "after=-1", "after=9223372036854775808"])
def test_change_feed_query_limits(workspace, query):
    assert workspace.client.get(f"/v1/changes?{query}").status_code == 422


def test_history_and_feed_pagination_use_immutable_snapshots(workspace):
    workspace.edit()
    workspace.edit(1, {"title": "Second"})
    workspace.edit(2, {"title": "Third"})
    first = workspace.client.get("/v1/changes?limit=2").json()
    assert [item["version"] for item in first["items"]] == [1, 2]
    assert first["items"][0]["data"]["title"] == "Trip"
    assert first["has_more"] is True
    workspace.edit(3, {"content": "After the first page"})
    second = workspace.client.get(f"/v1/changes?after={first['next_cursor']}&limit=2").json()
    assert [item["version"] for item in second["items"]] == [3, 4]
    assert second["has_more"] is False
    empty = workspace.client.get(f"/v1/changes?after={second['next_cursor']}").json()
    assert empty["items"] == []
    assert empty["next_cursor"] == second["next_cursor"]
    page = workspace.client.get(f"/v1/documents/{workspace.document_id}/history?limit=2").json()
    assert page["next_version"] == 2
    assert page["has_more"] is True
    saved = workspace.client.get(f"/v1/documents/{workspace.document_id}/versions/1").json()
    assert saved["data"]["title"] == "Trip"


def test_document_list_keyset_pagination(workspace):
    for _ in range(3):
        assert workspace.edit(document_id=str(uuid4())).status_code == 201
    page = workspace.client.get("/v1/documents?limit=2").json()
    assert len(page["items"]) == 2
    assert page["has_more"] is True
    next_page = workspace.client.get(f"/v1/documents?after={page['next_document_id']}").json()
    assert len(next_page["items"]) == 1
    assert next_page["has_more"] is False
    ids = [item["document_id"] for item in page["items"] + next_page["items"]]
    assert ids == sorted(set(ids))


def test_unknown_document_and_history(workspace):
    path = f"/v1/documents/{uuid4()}"
    assert workspace.client.get(path).status_code == 404
    assert workspace.client.get(path + "/history").status_code == 404
    assert workspace.client.get(path + "/versions/1").status_code == 404


def test_workspaces_cannot_access_each_others_data(workspace):
    conflict = workspace.conflict()
    other = workspace.client.post("/v1/users", json={"name": "Other"}).json()
    headers = {"Authorization": f"Bearer {other['access_token']}"}
    doc_path = f"/v1/documents/{workspace.document_id}"
    for path in (
        doc_path,
        doc_path + "/history",
        doc_path + "/versions/1",
        f"/v1/conflicts/{conflict['conflict_id']}",
    ):
        assert workspace.client.get(path, headers=headers).status_code == 404
    assert workspace.client.get("/v1/changes", headers=headers).json()["items"] == []
    assert workspace.client.get("/v1/documents", headers=headers).json()["items"] == []
    assert (
        workspace.client.post("/v1/sync", json=workspace.payload(), headers=headers).status_code
        == 404
    )
    workspace.client.put(
        f"/v1/devices/{workspace.phone}", json={"name": "Other phone"}, headers=headers
    )
    response = workspace.client.post(
        f"/v1/conflicts/{conflict['conflict_id']}/resolve",
        json=workspace.resolution(),
        headers=headers,
    )
    assert response.status_code == 404
    response = workspace.client.post(
        "/v1/sync",
        json=workspace.payload(device=workspace.phone),
        headers=headers,
    )
    assert response.status_code == 201
    assert workspace.read()["version"] == 2
    assert workspace.read()["data"]["title"] == "Laptop"


def test_malformed_unicode_in_unknown_field_returns_validation_error(workspace):
    body = workspace.payload()
    body["changes"] = {"\ud800": "invalid field"}
    response = workspace.client.post(
        "/v1/sync", content=json.dumps(body), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert workspace.client.get("/v1/changes").json()["items"] == []
