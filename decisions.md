# Implementation decision log
This log records meaningful implementation choices and their reasons as work proceeds. It is not a transcript, and it does not claim unperformed validation or deployment.

## D01 — Isolated project, no changes to unrelated applications
Create `offline-sync-backend/` under the working directory. The starting directory is a home directory, not an existing backend repository. Do not initialize or commit a repository without a request to do so.

## D02 — FastAPI, Python, and a file-backed SQLite database
FastAPI provides request validation and generated OpenAPI documentation without a frontend. Python and SQLite are available locally. A single Railway service with a persistent volume keeps the demonstration straightforward and inexpensive. The trade-off is one database writer at a time and no multi-replica deployment; migrate the storage layer to PostgreSQL before horizontal scaling.

## D03 — Explicit transactions, WAL, and durable writes
Every sync, resolution, and restore starts `BEGIN IMMEDIATE` before reading the document or idempotency record. The lock covers the decision, document update, revision, conflict state, and saved response. This prevents a read/modify/write race across threads or processes. WAL allows concurrent readers; `synchronous=FULL`, foreign keys, a five-second busy timeout, and rollback on failure favor correctness over throughput. Contention that exceeds the timeout will return a retryable error, not a partial update.

## D04 — Small, explicitly typed document model
Documents contain `title`, `content`, `tags`, and `archived`. Patches distinguish omitted fields from explicit values; nulls, unknown fields, type coercion, duplicate tags, and empty patches are rejected. Tags are an ordered atomic field, not a set or text CRDT. String lengths, collection sizes, version bounds, and the HTTP body size are limited.

## D05 — Server-owned versions and immutable snapshots
Clients provide document UUIDs, request UUIDs, a registered device UUID, and a base version. Version zero means create, not blind upsert. The server stores every accepted state change as a complete snapshot and assigns the next document version. Device clocks are never used for conflict ordering. A no-op does not allocate another version.

## D06 — Value-based three-way, field-level merging
Compare each supplied field with its value in the saved base and current server versions. A base-equal proposal expresses no new intent; a server-unchanged field can take the proposal; an identical concurrent value is already satisfied. Only differing edits to the same field conflict. Omitted or base-equal values cannot roll back newer values. Equality is about current values, not whether a field was ever touched; a server value reverted to the base is mergeable.

## D07 — Atomic conflicts with explicit, version-guarded resolution
A mixed safe/conflicting patch changes nothing until resolution, but its complete proposal is saved. This avoids surprising partial success. Resolution recomputes the original proposal against the latest state, requires its exact expected version, and requires a server/client choice for every currently conflicting field. Safe original edits are retained; unrelated current fields are preserved. A moved server version forces the client to review again. Custom values can be submitted as an ordinary follow-up sync.

## D08 — Local development must not imply ephemeral production data
The default database is `./data/sync.db`. On Railway, startup requires `RAILWAY_VOLUME_MOUNT_PATH` and a database path inside that mount. Refusing to start without persistence is safer than appearing healthy while losing history and idempotency records at redeployment. Schema version 1 is explicit; an unknown schema version fails instead of being silently overwritten.

## D09 — Durable idempotency includes unsuccessful domain outcomes
Scope request UUIDs to a user across all mutation endpoints. Hash the normalized validated payload together with the operation and resource identity. Store the HTTP status and response in the same transaction as all effects, including a saved conflict. Identical retries return the original response with `Idempotency-Replayed: true`, even if the document has since advanced. Reusing a UUID for different content is a 409. Domain rejections are also stable; correcting a rejected request requires a new UUID. Authentication/schema failures and transient database failures are not cached.

## D10 — Bearer capabilities instead of an unrelated account/password system
Registration creates an isolated workspace and a cryptographically random 256-bit bearer token. Only its SHA-256 hash is stored. Every protected query is scoped to the authenticated user; submitted user IDs are not trusted. Devices are registered within that scope, and clients share the workspace token across their own devices. This is a demonstration identity model, not verified identity, per-device credentials, token recovery, or a production signup-abuse solution.

## D11 — Monotonic change feed, not timestamp polling
Every real accepted version gets a database-assigned sequence in the same serialized write transaction. Download changes after a cursor in ascending sequence order and advance only to the last returned item. Use immutable snapshots, not current document lookups, so pagination does not collapse or miss intermediate accepted versions. Sequence gaps across workspaces are expected. Clients apply only snapshots newer than their local accepted version and preserve unsent local edits separately.

## D12 — Restore appends; no-op writes do not
Restoring a saved version requires the exact current version and records a new revision with its target version. Existing history is never rewritten. If the restored content or any ordinary accepted edit already matches the current state, save the idempotent outcome but do not create a redundant revision. Conflict resolution still closes its conflict record when the chosen data makes no change.

## D13 — Validate before mutation and avoid reflecting submitted secrets
Pydantic validates UUIDs, strict scalar types, Unicode, and field limits before the service runs. HTTP middleware enforces the actual streamed body size, including requests without Content-Length. Validation errors return locations and messages but not submitted input values. API responses are non-cacheable, credentials stay out of logs, and Swagger does not persist authorization in browser storage. A local SVG favicon is provided for the API documentation; no application frontend is added.

## D14 — Test the failure boundary, not only successful examples
Use real file-backed SQLite databases in tests, separate application instances/connections, synchronized concurrent requests, and injected failures after writes. Also test savepoint rollback for domain errors: a cached rejection must not commit an earlier mutation even if later business logic rejects it. This exercises the same transaction mechanism used by the deployed service rather than an in-memory database with different locking behavior.

## D15 — Use the test client's supported transport
The first 120 tests passed, but the installed Starlette version warned that its legacy `httpx` test transport is deprecated. Switch the dev dependency to the published `pydantic/httpx2` package recommended by Starlette, rather than suppressing the warning. Keep the runnable HTTP demonstration on Python's standard library so it needs no separate HTTP client package.

## D16 — Container startup owns initialization
Use Python 3.14 in the Docker image, matching the installed local interpreter's major/minor version. `python -m app` binds to Railway's `PORT` on `0.0.0.0` with one Uvicorn worker. Schema initialization runs in FastAPI's startup lifespan after the volume is mounted, never in a Railway pre-deploy step. The image retains its default UID because Railway volumes are root-owned; a non-root deployment needs a writable volume ownership strategy first.

## D17 — Reproducible verification without exposing credentials
Add a standard-library HTTP demonstration and GitHub Actions checks for Linux/Windows. Local verification starts its own server with an isolated temporary database and stops only that process afterward. The first real-socket run verified docs, OpenAPI, favicon, merging, conflict resolution, replay, restore, and all five feed versions. This validates the network/ASGI path beyond in-process tests. CI configuration is supplied but is not claimed to have run on GitHub.

## D18 — Conflict inspection must preserve patch intent
Serialize only the original supplied proposal fields in conflict reads, rather than letting optional model defaults invent null fields that would be invalid if resubmitted. Escape invalid Unicode in diagnostic locations/messages so even malformed unknown field names return a validation response, not an encoding-related 500.

## D19 — Explicit Railway service settings, no deprecated config file
After browser authorization, create a dedicated `offline-sync-backend` project, `sync-api` service, and the default 500 MB `/data` volume in the authorized personal workspace. Configure one replica, `/healthz` readiness, Docker startup, bounded failure restarts, and no deployment overlap using `scripts/railway-service.graphql`. The live Railway schema was inspected before authoring this mutation. This avoids introducing Railway's deprecated `railway.toml`/`railway.json` format and makes the applied settings repeatable without embedding credentials. Source uploads exclude local data and tooling; account authentication alone is never reported as successful deployment.

## D20 — Verify production behavior and persistence before declaring completion
Railway deployment `8429fe24-5ef2-4cdd-a2b8-fedd02091d64` reached `SUCCESS` and passed platform readiness at `https://sync-api-production-6fe8.up.railway.app`. The public HTTPS API passed the same merge/conflict/retry/restore demo as the local server. A dedicated archived verification document was then created, the service was actually restarted, and its unchanged document, history, and replayed original response were verified afterward. Temporary verification credentials were removed from the shell environment; only hashes remain in the database. Demo data is retained in its isolated workspaces rather than introducing a destructive cleanup endpoint. Final local checks passed: 122 tests, 97% coverage, warnings-as-errors, Ruff, compilation, and dependency consistency.

## D21 — Reuse the provided pytest suite for genuine production HTTP verification
The original pytest fixtures always created an in-process app; a URL environment variable alone would not exercise production. Add explicit `--base-url` and `--allow-live-writes` options, replacing only the HTTP transport with an HTTPS client while preserving the existing assertions. Each live test gets a new isolated workspace and random resource IDs. Concurrent tests use two real HTTP clients; local runs retain two app instances sharing the temporary SQLite file. Mark merge-unit, configuration, direct-SQL, restart, and failure-injection tests `local_only` and skip them before fixture setup in live mode. Never simulate a production restart by merely creating a second HTTP client. Reject remote coverage flags and insecure non-loopback URLs to avoid misleading reports or exposing test credentials. Production application code and deployment settings are unchanged; this verification does not restart or redeploy the service.

Verification result: the production pytest invocation completed in 367.13 seconds with 77 passes, zero failures/errors, and 45 explicitly justified local-only skips. Counts were confirmed by parsing `.tools/production-tests.xml`. The full local suite separately passed all 122 tests with 97% coverage, and lint/format/compilation checks passed. Five collect-only invocations verified the runner's configuration rejection safeguards without contacting those example targets. The standalone HTTP demo also passed again against production; Railway still reported deployment `8429fe24-5ef2-4cdd-a2b8-fedd02091d64` as `SUCCESS`.
