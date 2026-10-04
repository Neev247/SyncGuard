from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.errors import Problem
from app.main import create_app


def parallel_requests(clients, bodies, path="/v1/sync"):
    barrier = Barrier(len(bodies))

    def submit(index):
        barrier.wait(timeout=10)
        return clients[index % len(clients)].post(path, json=bodies[index])

    with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
        return list(pool.map(submit, range(len(bodies))))


def test_simultaneous_conflicting_writes_across_clients(workspace, peer_client):
    workspace.edit()
    results = parallel_requests(
        [workspace.client, peer_client],
        [
            workspace.payload(1, {"title": "Laptop"}),
            workspace.payload(1, {"title": "Phone"}, device=workspace.phone),
        ],
    )
    assert sorted(response.status_code for response in results) == [200, 409]
    accepted = next(response.json() for response in results if response.status_code == 200)
    assert workspace.read()["data"]["title"] == accepted["document"]["data"]["title"]
    assert workspace.read()["version"] == 2
    assert len(workspace.history()) == 2


def test_simultaneous_independent_edits_both_survive(workspace, peer_client):
    workspace.edit()
    results = parallel_requests(
        [workspace.client, peer_client],
        [
            workspace.payload(1, {"title": "Laptop"}),
            workspace.payload(1, {"content": "Phone"}, device=workspace.phone),
        ],
    )
    assert [response.status_code for response in results] == [200, 200]
    assert sorted(response.json()["outcome"] for response in results) == ["accepted", "merged"]
    assert workspace.read()["data"]["title"] == "Laptop"
    assert workspace.read()["data"]["content"] == "Phone"
    assert workspace.read()["version"] == 3


def test_simultaneous_duplicate_requests_apply_exactly_once(workspace, peer_client):
    body = workspace.payload()
    results = parallel_requests([workspace.client, peer_client], [body] * 8)
    assert {response.status_code for response in results} == {201}
    assert all(response.json() == results[0].json() for response in results)
    assert sum(response.headers["Idempotency-Replayed"] == "false" for response in results) == 1
    assert len(workspace.history()) == 1


def test_simultaneous_create_collision_does_not_replace_data(workspace):
    results = parallel_requests(
        [workspace.client],
        [
            workspace.payload(0, {"title": "One"}),
            workspace.payload(0, {"title": "Two"}, device=workspace.phone),
        ],
    )
    assert sorted(response.status_code for response in results) == [201, 409]
    winner = next(response.json() for response in results if response.status_code == 201)
    assert workspace.read()["data"] == winner["document"]["data"]
    assert len(workspace.history()) == 1


def test_two_resolutions_cannot_resolve_same_conflict_twice(workspace):
    conflict = workspace.conflict()
    path = f"/v1/conflicts/{conflict['conflict_id']}/resolve"
    results = parallel_requests(
        [workspace.client],
        [
            workspace.resolution(resolutions={"title": "client"}),
            workspace.resolution(resolutions={"title": "server"}),
        ],
        path=path,
    )
    assert sorted(response.status_code for response in results) == [200, 409]
    loser = next(response.json() for response in results if response.status_code == 409)
    assert loser["code"] == "conflict_resolved"


@pytest.mark.local_only(reason="Restarts an in-process application against a temporary database.")
def test_persistence_and_idempotency_survive_new_application(workspace, settings):
    body = workspace.payload()
    original = workspace.client.post("/v1/sync", json=body)
    workspace.edit(1, {"title": "Persisted"})
    conflict = workspace.edit(1, {"title": "Pending"}).json()
    with TestClient(create_app(settings)) as restarted:
        restarted.headers.update(workspace.client.headers)
        replay = restarted.post("/v1/sync", json=body)
        assert replay.json() == original.json()
        assert replay.headers["Idempotency-Replayed"] == "true"
        document = restarted.get(f"/v1/documents/{workspace.document_id}").json()
        assert document["data"]["title"] == "Persisted"
        assert len(restarted.get("/v1/changes").json()["items"]) == 2
        assert restarted.get(f"/v1/conflicts/{conflict['conflict_id']}").json()["status"] == "open"


@pytest.mark.local_only(reason="Intentionally holds an exclusive local database write lock.")
def test_lock_contention_is_retryable_without_caching_failure(workspace):
    body = workspace.payload()
    database = workspace.client.app.state.database
    with database.transaction(write=True):
        response = workspace.client.post("/v1/sync", json=body)
        assert response.status_code == 503
        assert response.json()["code"] == "database_busy"
        assert response.headers["Retry-After"] == "1"
    assert workspace.client.post("/v1/sync", json=body).status_code == 201
    assert len(workspace.history()) == 1


@pytest.mark.parametrize("failure", ["unexpected", "domain"])
@pytest.mark.local_only(reason="Monkeypatches the storage writer to inject failures.")
def test_failure_after_document_write_rolls_back(workspace, monkeypatch, failure):
    workspace.edit()
    service = workspace.client.app.state.service
    original_save = service._save

    def fail_after_save(*args, **kwargs):
        original_save(*args, **kwargs)
        if failure == "domain":
            raise Problem(409, "injected_rejection", "Injected domain rejection.")
        raise RuntimeError("Injected failure after writing a revision.")

    body = workspace.payload(1, {"title": "Must roll back"})
    monkeypatch.setattr(service, "_save", fail_after_save)
    response = workspace.client.post("/v1/sync", json=body)
    assert response.status_code == (409 if failure == "domain" else 500)
    assert workspace.read()["version"] == 1
    assert workspace.read()["data"]["title"] == "Trip"
    assert len(workspace.history()) == 1
    monkeypatch.setattr(service, "_save", original_save)
    retry = workspace.client.post("/v1/sync", json=body)
    if failure == "domain":
        assert retry.json() == response.json()
        assert retry.headers["Idempotency-Replayed"] == "true"
    else:
        assert retry.status_code == 200
        assert retry.headers["Idempotency-Replayed"] == "false"


@pytest.mark.local_only(reason="Installs a failing SQLite trigger in a temporary database.")
def test_idempotency_storage_failure_rolls_back_entire_mutation(workspace):
    workspace.edit()
    database = workspace.client.app.state.database
    with database.transaction(write=True) as connection:
        connection.execute(
            """CREATE TRIGGER injected_request_failure BEFORE INSERT ON requests
            BEGIN SELECT RAISE(ABORT, 'Injected request storage failure'); END"""
        )
    body = workspace.payload(1, {"title": "Must roll back"})
    response = workspace.client.post("/v1/sync", json=body)
    assert response.status_code == 500
    assert workspace.read()["version"] == 1
    assert len(workspace.history()) == 1
    with database.transaction(write=True) as connection:
        connection.execute("DROP TRIGGER injected_request_failure")
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM requests WHERE request_id = ?", (body["request_id"],)
            ).fetchone()[0]
            == 0
        )
    assert workspace.client.post("/v1/sync", json=body).status_code == 200


def test_unknown_device_rejection_replays_after_device_registration(workspace):
    device_id = str(uuid4())
    body = workspace.payload(device=device_id)
    rejected = workspace.client.post("/v1/sync", json=body)
    assert rejected.status_code == 404
    workspace.client.put(f"/v1/devices/{device_id}", json={"name": "Registered later"})
    assert workspace.client.post("/v1/sync", json=body).json() == rejected.json()
    body["request_id"] = str(uuid4())
    assert workspace.client.post("/v1/sync", json=body).status_code == 201


@pytest.mark.local_only(reason="Writes another workspace through the in-process service object.")
def test_change_feed_gaps_do_not_skip_own_changes(workspace):
    workspace.edit()
    first = workspace.client.get("/v1/changes").json()
    service = workspace.client.app.state.service
    other_user = service.register_user("Other")["user_id"]
    service.register_device(other_user, workspace.laptop, "Laptop")
    from app.models import SyncRequest

    service.sync(other_user, SyncRequest.model_validate(workspace.payload()))
    workspace.edit(1, {"title": "Second"})
    page = workspace.client.get(f"/v1/changes?after={first['next_cursor']}").json()
    assert [item["version"] for item in page["items"]] == [2]
    assert page["next_cursor"] > first["next_cursor"] + 1
