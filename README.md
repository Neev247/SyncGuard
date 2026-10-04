# Offline Sync Backend
A backend for devices that edit the same document while disconnected. Compatible field changes merge automatically; incompatible edits are saved as explicit conflicts instead of silently overwriting accepted data.

**Stack:** Python 3.14, FastAPI, Pydantic, SQLite with WAL. No frontend is required. Swagger, ReDoc, and a local favicon are included.

**Live API:** https://sync-api-production-6fe8.up.railway.app

**Try it:** https://sync-api-production-6fe8.up.railway.app/docs

- [Design decisions and reasons](decisions.md)
- [How execution actually travels through the code](flow.md)
- [Runnable two-device demonstration](scripts/demo.py)

## Run locally
Use Python 3.12 or newer; development and the Docker image use 3.14. Run these commands from the project directory.

### Windows PowerShell
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m app
```

### Linux/macOS
```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m app
```

Open:
- Swagger: http://127.0.0.1:8000/docs
- ReDoc: http://127.0.0.1:8000/redoc
- OpenAPI JSON: http://127.0.0.1:8000/openapi.json
- Readiness: http://127.0.0.1:8000/healthz

The database is initialized at startup in `./data/sync.db`. Set `SYNC_DATABASE_PATH` to change it and `PORT` to change the port. `.env.example` documents these variables; the app does **not** automatically load dotenv files. Development reload is optional: `python -m uvicorn app.main:app --reload`.

## Demonstrate without a frontend
With the server running, open another terminal:
```powershell
.\.venv\Scripts\python.exe scripts/demo.py
```
On Linux/macOS, use `.venv/bin/python scripts/demo.py`. For an actual deployed URL:
```powershell
.\.venv\Scripts\python.exe scripts/demo.py --base-url $env:SYNC_BASE_URL
```
The demo creates a fresh workspace and two devices, then verifies creation, an independent-field merge, a same-field conflict, explicit resolution, duplicate retries, restore, history, and paginated pull. It keeps its temporary bearer credential in process memory/environment and never prints it. Each run leaves one demonstration document and its history on the target server.

## Document and request model
A document contains:
- `title`: a trimmed, nonempty string, at most 200 characters.
- `content`: a string, at most 100,000 characters; empty is valid.
- `tags`: at most 20 unique, trimmed strings of 1–40 characters each. This is one ordered, atomic field.
- `archived`: a strict boolean. Archiving does not remove history or hide the document from synchronization.

Only `title` is required at creation. Other defaults are `content=""`, `tags=[]`, and `archived=false`. Updates must supply at least one field. Omitted fields are untouched; explicit `null` is rejected. Unknown fields, invalid Unicode, coercions such as `"1"` for a version, oversized bodies, and duplicate tags are rejected.

Each `POST /v1/sync` contains:
- `document_id`: a client-generated UUID, so creation can be prepared offline.
- `request_id`: a UUID identifying one logical operation, reused only for an identical retry.
- `device_id`: the UUID of a device registered in this workspace.
- `base_version`: the actual server version from which the device edited; zero means create.
- `changes`: the explicit field patch.

Versions are server-assigned positive integers per document. Every real state change appends a full immutable snapshot. No-op requests do not increase the version. Device timestamps are not accepted as ordering evidence.

## Merge and conflict rules
For each supplied field, let **B** be the saved base value, **S** the current server value, and **P** the proposed value:
1. If `P == B`, preserve `S`: the client did not change this field relative to its base.
2. Otherwise, if `S == B`, apply `P`: only the client changed it.
3. Otherwise, if `P == S`, preserve `S`: both changes already agree.
4. Otherwise, save a conflict containing all three values.

The comparison uses values, not an “ever modified” flag. A value changed and then restored to its original base is mergeable. Strings and tag arrays are atomic fields; this is not character-level merging, last-write-wins, a CRDT, or a version-vector implementation.

### Non-conflicting example
Both devices have v1: `title="Trip", content="Original notes"`.
- Laptop changes only `title` to `"Weekend trip"` → accepted as v2.
- Phone submits only `content="Book hotel"` from v1 → merged as v3.
- v3 retains both `"Weekend trip"` and `"Book hotel"`.

Even if the phone resends the unchanged v1 title along with its content edit, that title cannot revert the laptop's newer title. Identical independent title edits are a successful no-op, not a conflict.

### Conflicting example
Both devices have `title="Trip"` at v1.
- Laptop changes it to `"Weekend trip"` → v2.
- Phone proposes `"Work trip"` from v1 → HTTP 409, `outcome="conflict"`.
- The current document stays at v2. The full proposal is saved with a `conflict_id`.

If the phone's patch also includes a safe content edit, **none of the patch is partially applied**. The safe edit remains in the saved proposal and can be applied during resolution.

### Explicit resolution
1. Fetch `GET /v1/conflicts/{conflict_id}`.
2. Inspect `current_document` and `current_conflicts`.
3. Submit `expected_version=current_document.version` and a `"server"` or `"client"` choice for **exactly** each currently conflicting field.
4. The server recomputes the proposal under the write lock. If the version moved, it rejects the resolution with `version_mismatch`.

Choosing `"client"` deliberately accepts the saved proposed field; choosing `"server"` keeps its latest accepted value. Safe original edits still merge, and unrelated server fields survive. New intervening conflicts must also be reviewed. If all conflicts disappear, an empty `resolutions` object is valid. A successful resolution closes the record even when it changes no document values. To enter custom text, resolve first and send a normal new edit.

## Authentication and devices
`POST /v1/users` creates an isolated demo workspace and returns a random bearer token once. Store it securely and share it only with that user's devices. Every protected endpoint requires `Authorization: Bearer <token>`. Only a SHA-256 token hash is stored; no plaintext credential is kept in the database.

`PUT /v1/devices/{device_id}` registers a UUID within that workspace or idempotently renames an existing device. Every document, revision, conflict, device, and idempotency lookup is user-scoped. Another user's resource is reported as unknown.

This is a capability-based demonstration identity model, not a password/SSO system: there is no verified identity, token recovery, expiry/revocation, or per-device credential. Possession of the workspace token grants access to all its documents and devices. Public registration needs an external abuse/rate-limit policy before an unrestricted production launch.

## API endpoints
Public:
- `POST /v1/users` — create a workspace and receive its bearer token, HTTP 201.
- `GET /healthz` — confirm database readability, HTTP 200.
- `GET /docs`, `/redoc`, `/openapi.json`, `/favicon.svg` — API documentation.

Authenticated:
- `PUT /v1/devices/{device_id}` — register/rename a device.
- `POST /v1/sync` — create, update, merge, or save a conflict.
- `GET /v1/documents?after={document_uuid}&limit=50` — browse current documents.
- `GET /v1/documents/{document_id}` — read current state.
- `GET /v1/documents/{document_id}/history?after=0&limit=50` — ascending immutable versions.
- `GET /v1/documents/{document_id}/versions/{version}` — view one saved version.
- `POST /v1/documents/{document_id}/restore` — append a guarded restoration.
- `GET /v1/changes?after=0&limit=50` — download accepted snapshots in sequence order.
- `GET /v1/conflicts/{conflict_id}` — inspect a proposal and its latest comparison.
- `POST /v1/conflicts/{conflict_id}/resolve` — resolve against an expected current version.

Pagination limits are 1–100. History returns `next_version`; changes return `next_cursor`; document browsing returns `next_document_id`. They all return `has_more`. Document browsing is not a consistent multi-page snapshot under concurrent creation; use the change feed from cursor zero for initial synchronization.

## Example API requests and responses
### Register and create from PowerShell
```powershell
$base = "http://127.0.0.1:8000"
$user = Invoke-RestMethod -Method Post -Uri "$base/v1/users" `
    -ContentType "application/json" -Body '{"name":"Demo user"}'
$env:SYNC_TOKEN = $user.access_token
$headers = @{ Authorization = "Bearer $env:SYNC_TOKEN" }
$laptop = [guid]::NewGuid().ToString()
$phone = [guid]::NewGuid().ToString()
$document = [guid]::NewGuid().ToString()
Invoke-RestMethod -Method Put -Uri "$base/v1/devices/$laptop" `
    -Headers $headers -ContentType "application/json" -Body '{"name":"Laptop"}'
Invoke-RestMethod -Method Put -Uri "$base/v1/devices/$phone" `
    -Headers $headers -ContentType "application/json" -Body '{"name":"Phone"}'
$create = @{
    request_id = [guid]::NewGuid().ToString()
    device_id = $laptop
    document_id = $document
    base_version = 0
    changes = @{ title = "Trip"; content = "Original notes" }
}
Invoke-RestMethod -Method Post -Uri "$base/v1/sync" -Headers $headers `
    -ContentType "application/json" -Body ($create | ConvertTo-Json -Depth 5)
```
Example HTTP 201 response; IDs and timestamps are illustrative:
```json
{
  "outcome": "accepted",
  "code": "created",
  "message": "Document created.",
  "request_id": "00000000-0000-4000-8000-000000000001",
  "document": {
    "document_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "version": 1,
    "data": {"title": "Trip", "content": "Original notes", "tags": [], "archived": false},
    "updated_at": "2026-09-26T14:00:00+00:00",
    "device_id": "11111111-1111-4111-8111-111111111111",
    "request_id": "00000000-0000-4000-8000-000000000001"
  },
  "changed_fields": ["title", "content", "tags", "archived"],
  "stale": false,
  "conflicts": {}
}
```

### Offline edit
Submit this shape to `POST /v1/sync` with your actual IDs and a new request UUID:
```json
{
  "request_id": "00000000-0000-4000-8000-000000000002",
  "device_id": "22222222-2222-4222-8222-222222222222",
  "document_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "base_version": 1,
  "changes": {"content": "Book hotel"}
}
```
If a newer accepted revision changed only the title, HTTP 200 reports `outcome="merged"`, `stale=true`, `changed_fields=["content"]`, and the new complete document snapshot.

### Conflict response and resolution
A same-title disagreement returns HTTP 409. Relevant response fields are shown here; the actual response also includes the current `document` and `message`:
```json
{
  "outcome": "conflict",
  "code": "field_conflict",
  "request_id": "00000000-0000-4000-8000-000000000003",
  "conflict_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  "changed_fields": [],
  "stale": true,
  "conflicts": {
    "title": {"base": "Trip", "server": "Weekend trip", "client": "Work trip"}
  }
}
```
After fetching the conflict, submit to `POST /v1/conflicts/{conflict_id}/resolve`:
```json
{
  "request_id": "00000000-0000-4000-8000-000000000004",
  "device_id": "22222222-2222-4222-8222-222222222222",
  "expected_version": 2,
  "resolutions": {"title": "client"}
}
```
Success is HTTP 200 with `outcome="accepted"`, `code="resolved"`, and the resulting document. The expected version must be the version actually reviewed, not necessarily the version originally returned at conflict detection.

### Restore
`POST /v1/documents/{document_id}/restore`:
```json
{
  "request_id": "00000000-0000-4000-8000-000000000005",
  "device_id": "11111111-1111-4111-8111-111111111111",
  "expected_version": 3,
  "target_version": 1
}
```
If current is v3, restoring different v1 data creates v4 with `kind="restored"` and `restored_from=1`. v1–v3 remain unchanged. Restoring already-current values is an accepted no-op.

## HTTP outcomes and retry behavior
- **201 accepted:** new document created.
- **200 accepted:** current-base edit, explicit resolution, restore, or accepted no-op.
- **200 merged:** an outdated edit is compatible; `changed_fields=[]` indicates nothing needed changing.
- **409 conflict:** proposal saved; no document fields applied.
- **409 rejected:** unsafe/unknown version, create collision, resolved conflict, or changed payload under a reused request UUID.
- **404 rejected:** unknown document, version, conflict, or registered device in this workspace.
- **401 rejected:** missing/invalid bearer token, with `WWW-Authenticate: Bearer`.
- **422 rejected:** invalid data, missing creation title, or incorrect resolution field choices.
- **413 rejected:** body exceeds 256 KiB, including streamed requests.
- **400 rejected:** invalid Content-Length.
- **503 rejected:** storage/lock contention; `Retry-After: 1`. Retry the identical request.
- **500 rejected:** unexpected failure; transactions roll back and the internal detail is not returned.

Idempotency covers `/sync`, `/resolve`, and `/restore`. The key is `(user_id, request_id)` across these endpoints. A canonical hash includes the normalized payload and operation/resource identity. Key order does not matter; different content, a different device, or a different endpoint/resource does.

The HTTP status and JSON response are committed atomically with the document, history, and conflict effects. An identical retry returns the **original** response and `Idempotency-Replayed: true`; first execution has `false`. This includes business rejections and saved conflicts. Retrying an old success does not claim that its snapshot is the current server state. Fetch current data or continue the change feed.

Use a **new request UUID** after correcting a rejected base version, registering a previously unknown device, or changing resolution choices. HTTP authentication/schema failures happen before the operation cache. Unexpected failures and 503 contention failures roll back and are not cached. Registration itself is not an idempotent operation; persist the returned token.

## Client synchronization responsibilities
1. Store each document's last acknowledged server snapshot/version and keep unsent local edits separately.
2. Generate one request UUID per logical edit; keep the exact payload for network retries.
3. Use the actual acknowledged base. Do not invent future server versions for an offline queue. Coalesce pending edits per document, or send dependent edits after their predecessor is acknowledged.
4. A request delivered before its dependency may be rejected as `future_version` or `document_not_found`; review/rebase and use a new UUID. Distinct same-base edits arriving out of order are merged or conflicted, never ordered by device time.
5. Start `GET /v1/changes?after=0`. Process each page durably, then store its `next_cursor`; continue while `has_more` and poll again later. Do not derive a cursor from timestamps or assume consecutive integers.
6. Apply an incoming snapshot only if its version is newer than the locally acknowledged version. Do not overwrite unsent local work or regress from a replayed old response.
7. Keep conflict IDs for review. The change feed contains accepted **document revisions**, not rejected attempts or conflict-closure events with no document change.

## Consistency and storage
`BEGIN IMMEDIATE` acquires the SQLite write lock **before** checking the request cache and document state. It covers merge decisions, document writes, revision insertion, conflict updates, and response persistence. Separate requests cannot both decide against an unprotected old state. This works across threads/processes accessing the same local SQLite file; it is not a Python in-memory lock.

WAL permits concurrent readers. Writes use full synchronization and foreign keys. The normal busy timeout is five seconds, after which clients receive a retryable 503. Reads use a snapshot transaction. A savepoint rolls back any mutation preceding a handled domain rejection while still allowing that rejection to be cached.

Accepted changes receive a global SQLite sequence inside the same serialized transaction. Feed queries filter by user and order by sequence; gaps caused by other workspaces are normal. Records, snapshots, proposals, and idempotency entries are retained indefinitely in this version so old bases and retries remain meaningful. There is no delete endpoint; use `archived`.

## Tests and quality checks
```powershell
.\.venv\Scripts\python.exe -m pytest -q --cov=app --cov-report=term-missing
.\.venv\Scripts\python.exe -m ruff check app tests scripts
.\.venv\Scripts\python.exe -m ruff format --check app tests scripts
.\.venv\Scripts\python.exe -m compileall -q app tests scripts
```
Use `.venv/bin/python` on Linux/macOS. Tests cover the full merge truth table, stale/no-op patches, atomic conflicts, resolution races, restoration, malformed inputs, request-size limits, tenant isolation, pagination, restart durability, eight simultaneous duplicate retries, multi-instance concurrent edits, busy locks, and rollback after injected write failures. By default, all 122 tests use local code and temporary databases without touching a running server.

### Run the provided test suite against production
The suite also supports an explicit real-HTTP target:
```powershell
.\.venv\Scripts\python.exe -m pytest -v -W error --tb=short `
    --base-url https://sync-api-production-6fe8.up.railway.app `
    --allow-live-writes --junitxml=.tools/production-tests.xml
```
Verified on 2026-09-26: **77 passed, 0 failed, 0 errors, 45 skipped** in **367.13 seconds**. The JUnit report is `.tools/production-tests.xml`.

Live mode reuses the provided HTTP assertions with an HTTPS client instead of FastAPI's in-process client. It covers actual production authentication, workspace isolation, validation, merging, conflicts, resolution, restore, history, pagination, simultaneous edits, and eight concurrent identical retries. It requires `--allow-live-writes` because each test creates its own isolated workspace and random resource IDs; those verification records are retained. It does not modify existing user workspaces, restart the service, or redeploy code.

The 45 `local_only` cases are deliberately skipped **before fixtures execute**: pure Python merge tests, local configuration/ASGI checks, direct SQLite inspection, in-process restart tests, and injected storage failures. A second network client is not treated as a server restart. The full local suite still passes all 122 tests with 97% application coverage.

Do not use `--cov` with `--base-url`: coverage in the test process cannot measure the remote application's execution. The runner rejects that combination, non-HTTPS external targets, credential-bearing/path-prefixed URLs, and live targets without explicit write opt-in. CI continues to use the default local mode.

## Railway deployment
### Current status
Deployed and verified on 2026-09-26:
- API: https://sync-api-production-6fe8.up.railway.app
- Swagger: https://sync-api-production-6fe8.up.railway.app/docs
- Health: https://sync-api-production-6fe8.up.railway.app/healthz
- [Railway project](https://railway.com/project/8ef34447-b0f2-4639-892e-27354f8ade33)
- Service: `sync-api`, production environment, one replica, 500 MB persistent volume at `/data`.
- Deployment: `8429fe24-5ef2-4cdd-a2b8-fedd02091d64`, Railway status `SUCCESS`.

Local verification: **122 automated tests passed with warnings treated as errors; 97% coverage.** Latest production pytest verification: **77 passed, 0 failed, 45 local-only skips**, over real HTTPS. The standalone two-device demo also passed again, and Railway still reports the same deployment as `SUCCESS`.

Ruff lint/format checks, compilation, and dependency checks passed. Railway built the Linux Docker image and passed its `/healthz` readiness check. Public HTTPS tests verified health, Swagger, ReDoc, OpenAPI, and the favicon. During the original deployment verification, a real container restart was observed in the logs; afterward, a previously created document, immutable history, and original idempotent response were verified unchanged. The later live pytest run did not restart or redeploy production. GitHub Actions is configured but has not been run on GitHub in this delivery.

### Provision a new service
Node.js is needed for the Railway CLI, not for the backend. From this directory:
```powershell
npm install --prefix .tools --no-audit --no-fund @railway/cli@5.62.1
$railway = ".\.tools\node_modules\.bin\railway.cmd"
& $railway login
& $railway init --name offline-sync-backend
& $railway add --service sync-api
& $railway volume add --mount-path /data
```
Choose the intended account/workspace in the login/project prompts. For an existing project, use `railway link` and select the existing service rather than creating duplicates. A Railway account with permission and sufficient credits/plan capacity is required; services and persistent volumes can incur charges.

Apply the same settings used for this deployment:
```powershell
$state = & $railway status --json | ConvertFrom-Json
$serviceId = ($state.services.edges.node | Where-Object name -eq "sync-api").id
$environmentId = ($state.environments.edges.node | Where-Object name -eq "production").id
& $railway api --file scripts/railway-service.graphql `
    --raw-var "serviceId=$serviceId" --raw-var "environmentId=$environmentId"
```
The mutation sets **one replica**, the root `Dockerfile`, `python -m app`, readiness path **`/healthz`**, a 120-second readiness timeout, up to three on-failure restarts, and zero deployment overlap. Select the appropriate environment if not using `production`. These can also be configured in the Railway service settings. Do not add a pre-deploy database command. The Docker image's container healthcheck is separate from Railway's platform readiness check.

The attached volume must be mounted at `/data`. Railway supplies `RAILWAY_VOLUME_MOUNT_PATH`; the app chooses `/data/sync.db` automatically. Do not set `SYNC_DATABASE_PATH` to an ephemeral path. On Railway, the service deliberately refuses to start without a persistent volume or with a database path outside it.

```powershell
& $railway up
& $railway domain
```
Railway detects the Dockerfile, builds the image, and sets `PORT`. `railway up` uploads code; the domain command publishes the service. `.gitignore`, `.railwayignore`, and `.dockerignore` exclude virtual environments, local databases, token/config files, and CLI tooling.

### Verify the actual deployment
Set `SYNC_BASE_URL` to the actual HTTPS URL reported by Railway, then:
```powershell
Invoke-RestMethod "$env:SYNC_BASE_URL/healthz"
.\.venv\Scripts\python.exe scripts/demo.py --base-url $env:SYNC_BASE_URL
```
Also open `/docs` and `/favicon.svg`. Confirm the deployment is healthy in Railway, then restart/redeploy and verify that previously created data and request retries still exist. Do not treat a queued build or a generated domain alone as successful deployment.

### Operational limitations
- Keep one service replica and one local persistent volume. SQLite does not support independent per-replica files as one coherent database; use PostgreSQL plus row-level locking before horizontal scaling.
- Enable and test Railway volume backups. A live SQLite backup must include WAL-consistent state; use a stopped service/volume snapshot or SQLite's backup API, not an arbitrary copy of only `sync.db`.
- Anonymous signup, unlimited history retention, and non-expiring workspace tokens are demonstration trade-offs. Add identity lifecycle, quotas, rate limiting, monitoring, retention/compaction semantics, and key rotation before production use.
- Swagger/ReDoc load their JS/CSS from their default CDNs. The API itself and local favicon do not depend on those CDNs.
