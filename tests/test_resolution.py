from uuid import uuid4

import pytest


@pytest.mark.parametrize("choice,expected_title", [("server", "Laptop"), ("client", "Phone")])
def test_resolve_preserves_safe_pending_changes(workspace, choice, expected_title):
    conflict = workspace.conflict(changes={"title": "Phone", "content": "Pending"})
    path = f"/v1/conflicts/{conflict['conflict_id']}/resolve"
    response = workspace.client.post(path, json=workspace.resolution(resolutions={"title": choice}))
    assert response.status_code == 200
    assert response.json()["code"] == "resolved"
    assert workspace.read()["data"]["title"] == expected_title
    assert workspace.read()["data"]["content"] == "Pending"
    assert workspace.read()["version"] == 3
    assert workspace.history()[-1]["kind"] == "resolved"
    view = workspace.client.get(f"/v1/conflicts/{conflict['conflict_id']}").json()
    assert view["status"] == "resolved"
    assert view["resolution_request_id"] == response.json()["request_id"]


def test_stale_resolution_cannot_overwrite_intervening_edit(workspace):
    conflict = workspace.conflict()
    workspace.edit(2, {"title": "Even newer", "content": "Keep me"})
    path = f"/v1/conflicts/{conflict['conflict_id']}/resolve"
    stale = workspace.client.post(path, json=workspace.resolution(version=2))
    assert stale.status_code == 409
    assert stale.json()["code"] == "version_mismatch"
    assert workspace.read()["data"]["title"] == "Even newer"
    reviewed = workspace.client.get(f"/v1/conflicts/{conflict['conflict_id']}").json()
    assert reviewed["current_conflicts"]["title"]["server"] == "Even newer"
    response = workspace.client.post(path, json=workspace.resolution(version=3))
    assert response.status_code == 200
    assert workspace.read()["data"]["content"] == "Keep me"
    assert workspace.read()["data"]["title"] == "Phone"


def test_resolution_recomputes_new_conflicts(workspace):
    conflict = workspace.conflict(changes={"title": "Phone", "content": "Pending"})
    workspace.edit(2, {"content": "Intervening"})
    path = f"/v1/conflicts/{conflict['conflict_id']}/resolve"
    response = workspace.client.post(path, json=workspace.resolution(version=3))
    assert response.status_code == 422
    assert response.json()["code"] == "resolution_fields_mismatch"
    assert set(response.json()["conflicts"]) == {"title", "content"}
    assert workspace.read()["version"] == 3
    response = workspace.client.post(
        path,
        json=workspace.resolution(version=3, resolutions={"title": "client", "content": "server"}),
    )
    assert response.status_code == 200
    assert workspace.read()["data"]["content"] == "Intervening"


def test_empty_resolution_allowed_if_conflict_has_disappeared(workspace):
    conflict = workspace.conflict()
    workspace.edit(2, {"title": "Phone"})
    response = workspace.client.post(
        f"/v1/conflicts/{conflict['conflict_id']}/resolve",
        json=workspace.resolution(version=3, resolutions={}),
    )
    assert response.status_code == 200
    assert response.json()["changed_fields"] == []
    assert workspace.read()["version"] == 3


def test_keep_server_noop_still_closes_conflict_and_is_idempotent(workspace):
    conflict = workspace.conflict()
    path = f"/v1/conflicts/{conflict['conflict_id']}/resolve"
    body = workspace.resolution(resolutions={"title": "server"})
    original = workspace.client.post(path, json=body)
    assert original.status_code == 200
    assert original.json()["changed_fields"] == []
    assert workspace.read()["version"] == 2
    replay = workspace.client.post(path, json=body)
    assert replay.json() == original.json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    body["request_id"] = str(uuid4())
    rejected = workspace.client.post(path, json=body)
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "conflict_resolved"


@pytest.mark.parametrize(
    "choices", [{}, {"content": "client"}, {"title": "client", "tags": "server"}]
)
def test_resolution_requires_exact_conflicting_fields(workspace, choices):
    conflict = workspace.conflict()
    response = workspace.client.post(
        f"/v1/conflicts/{conflict['conflict_id']}/resolve",
        json=workspace.resolution(resolutions=choices),
    )
    assert response.status_code == 422
    assert workspace.read()["version"] == 2


def test_unknown_conflict(workspace):
    unknown = str(uuid4())
    assert workspace.client.get(f"/v1/conflicts/{unknown}").status_code == 404
    response = workspace.client.post(
        f"/v1/conflicts/{unknown}/resolve", json=workspace.resolution()
    )
    assert response.status_code == 404


def test_restore_appends_history_and_replays(workspace):
    workspace.edit()
    original_version = workspace.history()[0]
    workspace.edit(1, {"title": "New", "archived": True})
    path = f"/v1/documents/{workspace.document_id}/restore"
    body = {
        "request_id": str(uuid4()),
        "device_id": workspace.phone,
        "expected_version": 2,
        "target_version": 1,
    }
    first = workspace.client.post(path, json=body)
    assert first.status_code == 200
    assert first.json()["document"]["version"] == 3
    assert first.json()["document"]["data"] == original_version["data"]
    assert workspace.history()[0] == original_version
    assert workspace.history()[-1]["restored_from"] == 1
    assert workspace.history()[-1]["kind"] == "restored"
    assert workspace.client.post(path, json=body).json() == first.json()
    body["request_id"] = str(uuid4())
    stale = workspace.client.post(path, json=body)
    assert stale.status_code == 409
    assert stale.json()["code"] == "version_mismatch"
    assert workspace.read()["version"] == 3


def test_restore_noop_and_unknown_versions(workspace):
    workspace.edit()
    path = f"/v1/documents/{workspace.document_id}/restore"
    body = {
        "request_id": str(uuid4()),
        "device_id": workspace.phone,
        "expected_version": 1,
        "target_version": 1,
    }
    assert workspace.client.post(path, json=body).json()["code"] == "no_change"
    assert workspace.read()["version"] == 1
    body.update(request_id=str(uuid4()), target_version=10)
    assert workspace.client.post(path, json=body).status_code == 404
    assert (
        workspace.client.post(
            f"/v1/documents/{uuid4()}/restore",
            json={
                **body,
                "request_id": str(uuid4()),
            },
        ).status_code
        == 404
    )


def test_request_ids_span_mutation_endpoints(workspace):
    create = workspace.payload()
    workspace.client.post("/v1/sync", json=create)
    body = {
        "request_id": create["request_id"],
        "device_id": workspace.laptop,
        "expected_version": 1,
        "target_version": 1,
    }
    response = workspace.client.post(f"/v1/documents/{workspace.document_id}/restore", json=body)
    assert response.status_code == 409
    assert response.json()["code"] == "idempotency_key_reused"


def test_conflict_view_does_not_invent_null_patch_fields(workspace):
    conflict = workspace.conflict()
    view = workspace.client.get(f"/v1/conflicts/{conflict['conflict_id']}").json()
    assert view["proposed_changes"] == {"title": "Phone"}
