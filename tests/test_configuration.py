import asyncio
import sqlite3

import pytest

from app.config import Settings
from app.database import Database
from app.middleware import RequestBodyLimit

pytestmark = pytest.mark.local_only(
    reason="Inspects local configuration/SQLite or injects malformed ASGI transport messages."
)


@pytest.fixture
def clean_environment(monkeypatch):
    for name in ("SYNC_DATABASE_PATH", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_VOLUME_MOUNT_PATH"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_local_configuration_uses_data_directory(clean_environment):
    assert Settings.from_environment().database_path.name == "sync.db"
    assert Settings.from_environment().database_path.parent.name == "data"


def test_railway_requires_persistent_volume(clean_environment):
    clean_environment.setenv("RAILWAY_ENVIRONMENT_ID", "test")
    with pytest.raises(RuntimeError, match="persistent volume"):
        Settings.from_environment()


def test_railway_database_must_be_within_volume(clean_environment, tmp_path):
    clean_environment.setenv("RAILWAY_ENVIRONMENT_ID", "test")
    clean_environment.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path / "volume"))
    clean_environment.setenv("SYNC_DATABASE_PATH", str(tmp_path / "ephemeral.db"))
    with pytest.raises(RuntimeError, match="inside"):
        Settings.from_environment()
    clean_environment.delenv("SYNC_DATABASE_PATH")
    assert Settings.from_environment().database_path == tmp_path / "volume" / "sync.db"


def test_unknown_schema_version_fails_without_overwriting(settings):
    connection = sqlite3.connect(settings.database_path)
    connection.execute("PRAGMA user_version = 99")
    connection.close()
    with pytest.raises(RuntimeError, match="Unsupported"):
        Database(settings).initialize()
    connection = sqlite3.connect(settings.database_path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
    connection.close()


def test_database_pragmas_and_integrity(app, client):
    with app.state.database.transaction() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_middleware_counts_each_chunk_and_ignores_false_content_length():
    messages = iter(
        [
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"456", "more_body": False},
        ]
    )
    sent = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    async def forbidden_app(scope, receive, send):
        pytest.fail("Oversized requests must not reach the application.")

    middleware = RequestBodyLimit(forbidden_app, max_bytes=5)
    asyncio.run(
        middleware(
            {"type": "http", "headers": [(b"content-length", b"1")]},
            receive,
            send,
        )
    )
    assert sent[0]["status"] == 413


@pytest.mark.parametrize("length", [b"-1", b"invalid"])
def test_invalid_content_length(client, length):
    response = client.post("/v1/users", content=b"{}", headers={"Content-Length": length.decode()})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_content_length"
