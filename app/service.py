import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from app.database import Database
from app.errors import Problem
from app.merge import FIELDS, changed_fields, three_way_merge
from app.models import (
    DocumentData,
    Mutation,
    ResolutionRequest,
    RestoreRequest,
    SyncRequest,
    SyncResult,
)


def now() -> str:
    return datetime.now(UTC).isoformat()


def canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def snapshot(row: sqlite3.Row) -> dict:
    return {
        "document_id": row["document_id"],
        "version": row["version"],
        "data": json.loads(row["data"]),
        "updated_at": row["updated_at"],
        "device_id": row["device_id"],
        "request_id": row["request_id"],
    }


def revision(row: sqlite3.Row) -> dict:
    return {
        **snapshot(row),
        "cursor": row["sequence"],
        "kind": row["kind"],
        "base_version": row["base_version"],
        "restored_from": row["restored_from"],
    }


@dataclass(frozen=True)
class Reply:
    status: int
    body: dict
    replayed: bool = False


def result(request: Mutation, status: int, outcome: str, code: str, message: str, **extra) -> Reply:
    body = SyncResult(
        request_id=request.request_id, outcome=outcome, code=code, message=message, **extra
    ).model_dump(mode="json", exclude_none=True)
    return Reply(status, body)


def reject(request: Mutation, status: int, code: str, message: str, **extra) -> Reply:
    return result(request, status, "rejected", code, message, **extra)


class SyncService:
    def __init__(self, database: Database):
        self.db = database

    def register_user(self, name: str) -> dict:
        user_id = str(uuid4())
        token = "osync_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.db.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO users (user_id, name, token_hash, created_at) VALUES (?, ?, ?, ?)",
                (user_id, name, digest, now()),
            )
        return {"user_id": user_id, "name": name, "access_token": token, "token_type": "bearer"}

    def authenticate(self, token: str) -> str:
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT user_id FROM users WHERE token_hash = ?", (digest,)
            ).fetchone()
        if not row:
            raise Problem(401, "unauthorized", "A valid bearer token is required.")
        return row["user_id"]

    def register_device(self, user_id: str, device_id: str, name: str) -> dict:
        with self.db.transaction(write=True) as connection:
            connection.execute(
                """INSERT INTO devices (user_id, device_id, name, created_at) VALUES (?, ?, ?, ?)
                ON CONFLICT (user_id, device_id) DO UPDATE SET name = excluded.name""",
                (user_id, device_id, name, now()),
            )
            row = connection.execute(
                """SELECT device_id, name, created_at FROM devices
                WHERE user_id = ? AND device_id = ?""",
                (user_id, device_id),
            ).fetchone()
            return dict(row)

    def _run(
        self,
        user_id: str,
        operation: str,
        request: Mutation,
        execute: Callable[[sqlite3.Connection], Reply],
    ) -> Reply:
        payload = {
            "operation": operation,
            "body": request.model_dump(mode="json", exclude_none=True),
        }
        fingerprint = hashlib.sha256(canonical(payload).encode()).hexdigest()
        with self.db.transaction(write=True) as connection:
            cached = connection.execute(
                "SELECT * FROM requests WHERE user_id = ? AND request_id = ?",
                (user_id, str(request.request_id)),
            ).fetchone()
            if cached:
                if cached["fingerprint"] != fingerprint:
                    return reject(
                        request,
                        409,
                        "idempotency_key_reused",
                        "This request_id was already used for a different operation or payload.",
                    )
                return Reply(cached["status_code"], json.loads(cached["response"]), replayed=True)
            device = connection.execute(
                "SELECT 1 FROM devices WHERE user_id = ? AND device_id = ?",
                (user_id, str(request.device_id)),
            ).fetchone()
            if not device:
                reply = reject(request, 404, "device_not_found", "Register this device first.")
            else:
                connection.execute("SAVEPOINT mutation")
                try:
                    reply = execute(connection)
                except Problem as error:
                    connection.execute("ROLLBACK TO mutation")
                    reply = reject(request, error.status, error.code, error.message)
                finally:
                    connection.execute("RELEASE mutation")
            connection.execute(
                """INSERT INTO requests
                (user_id, request_id, fingerprint, status_code, response, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    str(request.request_id),
                    fingerprint,
                    reply.status,
                    canonical(reply.body),
                    now(),
                ),
            )
            return reply

    @staticmethod
    def _document(connection: sqlite3.Connection, user_id: str, document_id: str) -> dict | None:
        row = connection.execute(
            "SELECT * FROM documents WHERE user_id = ? AND document_id = ?",
            (user_id, document_id),
        ).fetchone()
        return snapshot(row) if row else None

    @staticmethod
    def _revision(
        connection: sqlite3.Connection, user_id: str, document_id: str, version: int
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM revisions WHERE user_id = ? AND document_id = ? AND version = ?",
            (user_id, document_id, version),
        ).fetchone()

    @staticmethod
    def _save(
        connection: sqlite3.Connection,
        user_id: str,
        document_id: str,
        previous: dict | None,
        data: dict,
        request: Mutation,
        *,
        kind: str,
        base_version: int,
        restored_from: int | None = None,
    ) -> dict:
        version = previous["version"] + 1 if previous else 1
        if version > 2_147_483_647:
            raise Problem(409, "version_limit", "This document has reached the version limit.")
        timestamp = now()
        serialized = canonical(DocumentData.model_validate(data).model_dump())
        values = (
            user_id,
            document_id,
            version,
            serialized,
            timestamp,
            str(request.device_id),
            str(request.request_id),
        )
        connection.execute(
            """INSERT INTO documents
            (user_id, document_id, version, data, updated_at, device_id, request_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (user_id, document_id) DO UPDATE SET
                version = excluded.version, data = excluded.data, updated_at = excluded.updated_at,
                device_id = excluded.device_id, request_id = excluded.request_id""",
            values,
        )
        connection.execute(
            """INSERT INTO revisions
            (user_id, document_id, version, data, updated_at, device_id, request_id,
             kind, base_version, restored_from)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (*values, kind, base_version, restored_from),
        )
        return {
            "document_id": document_id,
            "version": version,
            "data": json.loads(serialized),
            "updated_at": timestamp,
            "device_id": str(request.device_id),
            "request_id": str(request.request_id),
        }

    def sync(self, user_id: str, request: SyncRequest) -> Reply:
        def execute(connection: sqlite3.Connection) -> Reply:
            document_id = str(request.document_id)
            current = self._document(connection, user_id, document_id)
            changes = request.changes.supplied()
            if request.base_version == 0:
                if current:
                    return reject(
                        request,
                        409,
                        "document_exists",
                        "Version zero only creates new documents.",
                        document=current,
                    )
                if "title" not in changes:
                    return reject(
                        request, 422, "title_required", "A new document requires a title."
                    )
                data = DocumentData.model_validate(changes).model_dump()
                document = self._save(
                    connection,
                    user_id,
                    document_id,
                    None,
                    data,
                    request,
                    kind="created",
                    base_version=0,
                )
                return result(
                    request,
                    201,
                    "accepted",
                    "created",
                    "Document created.",
                    document=document,
                    changed_fields=list(FIELDS),
                )
            if not current:
                return reject(request, 404, "document_not_found", "The document does not exist.")
            if request.base_version > current["version"]:
                return reject(
                    request,
                    409,
                    "future_version",
                    "The base version has not been accepted yet.",
                    document=current,
                )
            base = self._revision(connection, user_id, document_id, request.base_version)
            if not base:
                return reject(
                    request, 409, "base_version_unavailable", "The base snapshot is unknown."
                )
            stale = request.base_version < current["version"]
            merge = three_way_merge(json.loads(base["data"]), current["data"], changes)
            if merge.conflicts:
                conflict_id = str(uuid4())
                connection.execute(
                    """INSERT INTO conflicts
                    (conflict_id, user_id, document_id, device_id, request_id, base_version,
                     server_version, proposed_changes, original_conflicts, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        conflict_id,
                        user_id,
                        document_id,
                        str(request.device_id),
                        str(request.request_id),
                        request.base_version,
                        current["version"],
                        canonical(changes),
                        canonical(merge.conflicts),
                        now(),
                    ),
                )
                return result(
                    request,
                    409,
                    "conflict",
                    "field_conflict",
                    "No fields were applied. Review and resolve the saved proposal.",
                    document=current,
                    stale=stale,
                    conflict_id=conflict_id,
                    conflicts=merge.conflicts,
                )
            changed = changed_fields(current["data"], merge.data)
            document = current
            if changed:
                document = self._save(
                    connection,
                    user_id,
                    document_id,
                    current,
                    merge.data,
                    request,
                    kind="merged" if stale else "updated",
                    base_version=request.base_version,
                )
            return result(
                request,
                200,
                "merged" if stale else "accepted",
                "updated" if changed else "no_change",
                "Compatible changes merged." if stale else "Change accepted.",
                document=document,
                changed_fields=changed,
                stale=stale,
            )

        return self._run(user_id, "sync", request, execute)

    @staticmethod
    def _conflict(connection: sqlite3.Connection, user_id: str, conflict_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM conflicts WHERE user_id = ? AND conflict_id = ?", (user_id, conflict_id)
        ).fetchone()
        if not row:
            raise Problem(404, "conflict_not_found", "The conflict does not exist.")
        return row

    def resolve(self, user_id: str, conflict_id: str, request: ResolutionRequest) -> Reply:
        def execute(connection: sqlite3.Connection) -> Reply:
            conflict = self._conflict(connection, user_id, conflict_id)
            if conflict["status"] != "open":
                return reject(
                    request, 409, "conflict_resolved", "This conflict is already resolved."
                )
            document_id = conflict["document_id"]
            current = self._document(connection, user_id, document_id)
            if request.expected_version != current["version"]:
                return reject(
                    request,
                    409,
                    "version_mismatch",
                    "Fetch the conflict again and review the latest state before resolving.",
                    document=current,
                    stale=request.expected_version < current["version"],
                )
            base = self._revision(connection, user_id, document_id, conflict["base_version"])
            changes = json.loads(conflict["proposed_changes"])
            merge = three_way_merge(json.loads(base["data"]), current["data"], changes)
            if set(request.resolutions) != set(merge.conflicts):
                return reject(
                    request,
                    422,
                    "resolution_fields_mismatch",
                    "Provide exactly one server/client choice for each current conflicting field.",
                    document=current,
                    conflicts=merge.conflicts,
                )
            for field, choice in request.resolutions.items():
                if choice == "client":
                    merge.data[field] = changes[field]
            changed = changed_fields(current["data"], merge.data)
            document = current
            if changed:
                document = self._save(
                    connection,
                    user_id,
                    document_id,
                    current,
                    merge.data,
                    request,
                    kind="resolved",
                    base_version=request.expected_version,
                )
            connection.execute(
                """UPDATE conflicts
                SET status = 'resolved', resolved_at = ?, resolution_request_id = ?
                WHERE user_id = ? AND conflict_id = ?""",
                (now(), str(request.request_id), user_id, conflict_id),
            )
            return result(
                request,
                200,
                "accepted",
                "resolved",
                "Conflict resolved.",
                document=document,
                changed_fields=changed,
                conflict_id=conflict_id,
            )

        return self._run(user_id, f"resolve:{conflict_id}", request, execute)

    def restore(self, user_id: str, document_id: str, request: RestoreRequest) -> Reply:
        def execute(connection: sqlite3.Connection) -> Reply:
            current = self._document(connection, user_id, document_id)
            if not current:
                return reject(request, 404, "document_not_found", "The document does not exist.")
            if request.expected_version != current["version"]:
                return reject(
                    request,
                    409,
                    "version_mismatch",
                    "Refresh the document before restoring.",
                    document=current,
                    stale=request.expected_version < current["version"],
                )
            target = self._revision(connection, user_id, document_id, request.target_version)
            if not target:
                return reject(
                    request, 404, "version_not_found", "The target version does not exist."
                )
            data = json.loads(target["data"])
            changed = changed_fields(current["data"], data)
            document = current
            if changed:
                document = self._save(
                    connection,
                    user_id,
                    document_id,
                    current,
                    data,
                    request,
                    kind="restored",
                    base_version=request.expected_version,
                    restored_from=request.target_version,
                )
            return result(
                request,
                200,
                "accepted",
                "restored" if changed else "no_change",
                "Restore accepted; existing history is unchanged.",
                document=document,
                changed_fields=changed,
            )

        return self._run(user_id, f"restore:{document_id}", request, execute)

    def get_document(self, user_id: str, document_id: str) -> dict:
        with self.db.transaction() as connection:
            document = self._document(connection, user_id, document_id)
            if not document:
                raise Problem(404, "document_not_found", "The document does not exist.")
            return document

    def list_documents(self, user_id: str, after: str, limit: int) -> dict:
        with self.db.transaction() as connection:
            rows = connection.execute(
                """SELECT * FROM documents WHERE user_id = ? AND document_id > ?
                ORDER BY document_id LIMIT ?""",
                (user_id, after, limit + 1),
            ).fetchall()
            page = rows[:limit]
            return {
                "items": [snapshot(row) for row in page],
                "next_document_id": page[-1]["document_id"] if page else None,
                "has_more": len(rows) > limit,
            }

    def get_revision(self, user_id: str, document_id: str, version: int) -> dict:
        with self.db.transaction() as connection:
            row = self._revision(connection, user_id, document_id, version)
            if not row:
                raise Problem(404, "version_not_found", "The document version does not exist.")
            return revision(row)

    def history(self, user_id: str, document_id: str, after: int, limit: int) -> dict:
        with self.db.transaction() as connection:
            if not self._document(connection, user_id, document_id):
                raise Problem(404, "document_not_found", "The document does not exist.")
            rows = connection.execute(
                """SELECT * FROM revisions WHERE user_id = ? AND document_id = ? AND version > ?
                ORDER BY version LIMIT ?""",
                (user_id, document_id, after, limit + 1),
            ).fetchall()
            page = rows[:limit]
            return {
                "items": [revision(row) for row in page],
                "next_version": page[-1]["version"] if page else after,
                "has_more": len(rows) > limit,
            }

    def changes(self, user_id: str, after: int, limit: int) -> dict:
        with self.db.transaction() as connection:
            rows = connection.execute(
                """SELECT * FROM revisions WHERE user_id = ? AND sequence > ?
                ORDER BY sequence LIMIT ?""",
                (user_id, after, limit + 1),
            ).fetchall()
            page = rows[:limit]
            return {
                "items": [revision(row) for row in page],
                "next_cursor": page[-1]["sequence"] if page else after,
                "has_more": len(rows) > limit,
            }

    def get_conflict(self, user_id: str, conflict_id: str) -> dict:
        with self.db.transaction() as connection:
            conflict = self._conflict(connection, user_id, conflict_id)
            base = self._revision(
                connection, user_id, conflict["document_id"], conflict["base_version"]
            )
            current = self._document(connection, user_id, conflict["document_id"])
            proposed = json.loads(conflict["proposed_changes"])
            merge = three_way_merge(json.loads(base["data"]), current["data"], proposed)
            return {
                "conflict_id": conflict_id,
                "document_id": conflict["document_id"],
                "device_id": conflict["device_id"],
                "request_id": conflict["request_id"],
                "status": conflict["status"],
                "base_version": conflict["base_version"],
                "server_version_at_detection": conflict["server_version"],
                "proposed_changes": proposed,
                "original_conflicts": json.loads(conflict["original_conflicts"]),
                "current_conflicts": merge.conflicts,
                "base_document": snapshot(base),
                "current_document": current,
                "created_at": conflict["created_at"],
                "resolved_at": conflict["resolved_at"],
                "resolution_request_id": conflict["resolution_request_id"],
            }
