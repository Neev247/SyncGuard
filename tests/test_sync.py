from uuid import uuid4

import pytest


def test_create_and_device_metadata(workspace):
    response = workspace.edit()
    assert response.status_code == 201
    body = response.json()
    assert body["outcome"] == "accepted"
    assert body["document"]["version"] == 1
    assert body["document"]["device_id"] == workspace.laptop
    assert body["document"]["data"] == {
        "title": "Trip",
        "content": "Base notes",
        "tags": [],
        "archived": False,
    }
    assert workspace.history()[0]["kind"] == "created"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Idempotency-Replayed"] == "false"


def test_current_edit_changes_only_supplied_fields(workspace):
    workspace.edit()
    updated = workspace.edit(1, {"content": "", "archived": True})
    assert updated.status_code == 200
    assert updated.json()["changed_fields"] == ["content", "archived"]
    assert workspace.read()["data"]["title"] == "Trip"
    assert workspace.read()["version"] == 2


def test_stale_independent_fields_merge(workspace):
    workspace.edit()
    workspace.edit(1, {"title": "Laptop"})
    response = workspace.edit(1, {"content": "Phone"}, device=workspace.phone)
    assert response.status_code == 200
    assert response.json()["outcome"] == "merged"
    assert response.json()["stale"] is True
    assert response.json()["document"]["version"] == 3
    assert workspace.read()["data"]["title"] == "Laptop"
    assert workspace.read()["data"]["content"] == "Phone"
    assert workspace.history()[-1]["base_version"] == 1
    assert workspace.history()[-1]["kind"] == "merged"


def test_stale_full_snapshot_cannot_revert_unmodified_fields(workspace):
    workspace.edit()
    workspace.edit(1, {"title": "New title", "tags": ["new"], "archived": True})
    response = workspace.edit(
        1,
        {"title": "Trip", "content": "Phone", "tags": [], "archived": False},
        device=workspace.phone,
    )
    assert response.status_code == 200
    assert response.json()["changed_fields"] == ["content"]
    assert workspace.read()["data"] == {
        "title": "New title",
        "content": "Phone",
        "tags": ["new"],
        "archived": True,
    }


@pytest.mark.parametrize("stale", [False, True])
def test_identical_edit_is_noop(workspace, stale):
    workspace.edit()
    workspace.edit(1, {"title": "New"})
    response = workspace.edit(1 if stale else 2, {"title": "New"})
    assert response.status_code == 200
    assert response.json()["code"] == "no_change"
    assert response.json()["changed_fields"] == []
    assert workspace.read()["version"] == 2
    assert len(workspace.history()) == 2


def test_stale_base_equal_edit_does_not_overwrite_newer_value(workspace):
    workspace.edit()
    workspace.edit(1, {"title": "New"})
    response = workspace.edit(1, {"title": "Trip"})
    assert response.status_code == 200
    assert response.json()["code"] == "no_change"
    assert workspace.read()["data"]["title"] == "New"


def test_conflict_preserves_all_data_and_entire_proposal(workspace):
    conflict = workspace.conflict(changes={"title": "Phone", "content": "Safe but pending"})
    assert conflict["outcome"] == "conflict"
    assert conflict["conflicts"] == {
        "title": {"base": "Trip", "server": "Laptop", "client": "Phone"},
    }
    assert workspace.read()["version"] == 2
    assert workspace.read()["data"]["content"] == "Base notes"
    view = workspace.client.get(f"/v1/conflicts/{conflict['conflict_id']}").json()
    assert view["proposed_changes"]["content"] == "Safe but pending"
    assert view["base_document"]["version"] == 1
    assert view["status"] == "open"


def test_atomic_array_conflict(workspace):
    workspace.edit()
    workspace.edit(1, {"tags": ["server"]})
    response = workspace.edit(1, {"tags": ["client"]})
    assert response.status_code == 409
    assert set(response.json()["conflicts"]) == {"tags"}
    assert workspace.read()["data"]["tags"] == ["server"]


def test_create_collision_is_not_upsert(workspace):
    workspace.edit()
    response = workspace.edit(0, {"title": "Collision"})
    assert response.status_code == 409
    assert response.json()["code"] == "document_exists"
    assert workspace.read()["version"] == 1


def test_future_and_out_of_order_edit_require_new_request_after_rejection(workspace):
    workspace.edit()
    early = workspace.payload(2, {"content": "After dependency"})
    first = workspace.client.post("/v1/sync", json=early)
    assert first.status_code == 409
    assert first.json()["code"] == "future_version"
    assert workspace.edit(1, {"title": "Dependency"}).status_code == 200
    replay = workspace.client.post("/v1/sync", json=early)
    assert replay.json() == first.json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    early["request_id"] = str(uuid4())
    assert workspace.client.post("/v1/sync", json=early).status_code == 200
    assert workspace.read()["data"]["title"] == "Dependency"


def test_request_before_document_creation_is_rejected(workspace):
    response = workspace.edit(1, {"title": "Unknown"})
    assert response.status_code == 404
    assert response.json()["code"] == "document_not_found"


def test_unknown_device_cannot_write(workspace):
    response = workspace.edit(device=str(uuid4()))
    assert response.status_code == 404
    assert response.json()["code"] == "device_not_found"
    assert workspace.client.get("/v1/documents").json()["items"] == []


def test_create_requires_title(workspace):
    response = workspace.edit(0, {"content": "Missing title"})
    assert response.status_code == 422
    assert response.json()["code"] == "title_required"
    assert workspace.client.get("/v1/changes").json()["items"] == []


def test_same_request_replays_original_response_even_after_newer_write(workspace):
    payload = workspace.payload()
    original = workspace.client.post("/v1/sync", json=payload)
    workspace.edit(1, {"title": "Later"})
    response = workspace.client.post("/v1/sync", json=dict(reversed(list(payload.items()))))
    assert response.status_code == original.status_code == 201
    assert response.json() == original.json()
    assert response.headers["Idempotency-Replayed"] == "true"
    assert workspace.read()["version"] == 2
    assert workspace.read()["data"]["title"] == "Later"


@pytest.mark.parametrize("different", ["changes", "device_id", "document_id"])
def test_same_request_id_with_different_body_rejected(workspace, different):
    payload = workspace.payload()
    workspace.client.post("/v1/sync", json=payload)
    if different == "changes":
        payload["changes"] = {"title": "Different"}
    elif different == "device_id":
        payload["device_id"] = workspace.phone
    else:
        payload["document_id"] = str(uuid4())
    response = workspace.client.post("/v1/sync", json=payload)
    assert response.status_code == 409
    assert response.json()["code"] == "idempotency_key_reused"
    assert workspace.read()["version"] == 1


@pytest.mark.local_only(reason="Checks the conflict row count directly in SQLite.")
def test_retry_conflict_does_not_create_another_conflict(workspace):
    workspace.edit()
    workspace.edit(1, {"title": "Laptop"})
    payload = workspace.payload(1, {"title": "Phone"})
    original = workspace.client.post("/v1/sync", json=payload)
    response = workspace.client.post("/v1/sync", json=payload)
    assert response.json() == original.json()
    assert response.headers["Idempotency-Replayed"] == "true"
    with workspace.client.app.state.database.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM conflicts").fetchone()[0] == 1
