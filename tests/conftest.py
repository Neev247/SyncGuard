from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def pytest_addoption(parser):
    group = parser.getgroup("live-api")
    group.addoption(
        "--base-url",
        default=None,
        help="Run HTTP tests against this API origin instead of an in-process app.",
    )
    group.addoption(
        "--allow-live-writes",
        action="store_true",
        help="Allow creating isolated test workspaces/documents at --base-url.",
    )


def pytest_configure(config):
    url = config.getoption("base_url")
    allow_writes = config.getoption("allow_live_writes")
    if url is None:
        if allow_writes:
            raise pytest.UsageError("--allow-live-writes requires --base-url.")
        return
    try:
        parsed = urlsplit(url.strip())
        valid = (
            parsed.scheme in ("https", "http")
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        )
    except ValueError:
        valid = False
    if not valid:
        raise pytest.UsageError(
            "--base-url must be an HTTP(S) origin without credentials or a path."
        )
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise pytest.UsageError("Live tests require HTTPS except for a loopback server.")
    if not allow_writes:
        raise pytest.UsageError(
            "Live API tests create isolated workspaces. Add --allow-live-writes to opt in."
        )
    if config.getoption("cov_source", default=[]):
        raise pytest.UsageError(
            "Local --cov cannot measure the remote server; omit it in live mode."
        )
    config.option.base_url = url.strip().rstrip("/")


def pytest_report_header(config):
    target = config.getoption("base_url")
    return (
        f"API target: {target} (live HTTP)"
        if target
        else "API target: in-process, temporary SQLite"
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("base_url") is None:
        return
    for item in items:
        marker = item.get_closest_marker("local_only")
        if marker:
            reason = marker.kwargs.get("reason", "Requires local application or database access.")
            item.add_marker(pytest.mark.skip(reason=f"Local-only: {reason}"))


@pytest.fixture
def settings(tmp_path, pytestconfig):
    if pytestconfig.getoption("base_url") is not None:
        pytest.fail(
            "A local database fixture was requested in live mode; mark this test local_only."
        )
    return Settings(database_path=tmp_path / "test.db", busy_timeout_ms=1000)


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(request):
    target = request.config.getoption("base_url")
    if target is not None:
        with httpx2.Client(base_url=target, timeout=30, follow_redirects=True) as live_client:
            yield live_client
    else:
        with TestClient(request.getfixturevalue("app"), raise_server_exceptions=False) as local:
            yield local


@pytest.fixture
def peer_client(workspace, request):
    target = request.config.getoption("base_url")
    if target is not None:
        with httpx2.Client(
            base_url=target,
            headers=workspace.client.headers,
            timeout=30,
            follow_redirects=True,
        ) as peer:
            yield peer
    else:
        with TestClient(create_app(request.getfixturevalue("settings"))) as peer:
            peer.headers.update(workspace.client.headers)
            yield peer


@dataclass
class Workspace:
    client: TestClient | httpx2.Client
    user_id: str
    laptop: str
    phone: str
    document_id: str

    def payload(self, base=0, changes=None, *, device=None, request_id=None, document_id=None):
        return {
            "request_id": request_id or str(uuid4()),
            "device_id": device or self.laptop,
            "document_id": document_id or self.document_id,
            "base_version": base,
            "changes": changes
            if changes is not None
            else {"title": "Trip", "content": "Base notes"},
        }

    def edit(self, base=0, changes=None, **kwargs):
        return self.client.post("/v1/sync", json=self.payload(base, changes, **kwargs))

    def read(self):
        response = self.client.get(f"/v1/documents/{self.document_id}")
        assert response.status_code == 200
        return response.json()

    def history(self):
        response = self.client.get(f"/v1/documents/{self.document_id}/history")
        assert response.status_code == 200
        return response.json()["items"]

    def resolution(self, version=2, resolutions=None, request_id=None):
        return {
            "request_id": request_id or str(uuid4()),
            "device_id": self.phone,
            "expected_version": version,
            "resolutions": resolutions if resolutions is not None else {"title": "client"},
        }

    def conflict(self, *, changes=None):
        assert self.edit().status_code == 201
        assert self.edit(1, {"title": "Laptop"}).status_code == 200
        response = self.edit(1, changes or {"title": "Phone"}, device=self.phone)
        assert response.status_code == 409
        return response.json()


@pytest.fixture
def workspace(client, pytestconfig):
    name = "Owner"
    if pytestconfig.getoption("base_url") is not None:
        name = f"Live verification {uuid4()}"
    response = client.post("/v1/users", json={"name": name})
    assert response.status_code == 201
    user = response.json()
    client.headers["Authorization"] = f"Bearer {user['access_token']}"
    laptop, phone = str(uuid4()), str(uuid4())
    for device_id, name in ((laptop, "Laptop"), (phone, "Phone")):
        assert client.put(f"/v1/devices/{device_id}", json={"name": name}).status_code == 200
    return Workspace(client, user["user_id"], laptop, phone, str(uuid4()))
