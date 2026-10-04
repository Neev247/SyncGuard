import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from app.config import Settings

SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    user_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, device_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);
CREATE TABLE IF NOT EXISTS documents (
    user_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    device_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    PRIMARY KEY (user_id, document_id),
    FOREIGN KEY (user_id, device_id) REFERENCES devices(user_id, device_id)
);
CREATE TABLE IF NOT EXISTS revisions (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    device_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('created','updated','merged','resolved','restored')),
    base_version INTEGER NOT NULL CHECK (base_version >= 0),
    restored_from INTEGER,
    UNIQUE (user_id, document_id, version),
    FOREIGN KEY (user_id, document_id) REFERENCES documents(user_id, document_id),
    FOREIGN KEY (user_id, device_id) REFERENCES devices(user_id, device_id)
);
CREATE INDEX IF NOT EXISTS revisions_user_sequence ON revisions(user_id, sequence);
CREATE TABLE IF NOT EXISTS conflicts (
    conflict_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    base_version INTEGER NOT NULL,
    server_version INTEGER NOT NULL,
    proposed_changes TEXT NOT NULL,
    original_conflicts TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution_request_id TEXT,
    FOREIGN KEY (user_id, document_id) REFERENCES documents(user_id, document_id),
    FOREIGN KEY (user_id, device_id) REFERENCES devices(user_id, device_id)
);
CREATE INDEX IF NOT EXISTS conflicts_user_document ON conflicts(user_id, document_id);
CREATE TABLE IF NOT EXISTS requests (
    user_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, request_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);
PRAGMA user_version = 1;
COMMIT;
"""


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.settings.database_path,
            timeout=self.settings.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.settings.busy_timeout_ms)}")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def initialize(self) -> None:
        self.settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError(f"Unsupported database schema version: {version}")
            if connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] != "wal":
                raise RuntimeError("A file-backed SQLite database with WAL support is required.")
            connection.executescript(SCHEMA)
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
