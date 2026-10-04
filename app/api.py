from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.errors import Problem
from app.models import (
    ChangePage,
    ConflictView,
    DeviceRegistration,
    DeviceView,
    DocumentPage,
    DocumentView,
    ErrorResponse,
    HistoryPage,
    ResolutionRequest,
    RestoreRequest,
    RevisionView,
    SyncRequest,
    SyncResult,
    UserCreate,
    UserRegistered,
)
from app.service import Reply, SyncService

router = APIRouter(
    prefix="/v1",
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid workspace bearer token."},
        404: {"model": ErrorResponse, "description": "Resource not found in this workspace."},
        413: {"model": ErrorResponse, "description": "Request body exceeds 256 KiB."},
        422: {
            "model": ErrorResponse,
            "description": "Invalid request; no document changes applied.",
        },
        503: {"model": ErrorResponse, "description": "Transient database contention; retry."},
    },
)
bearer = HTTPBearer(
    auto_error=False,
    description=(
        "Use the access_token returned by POST /v1/users. Share it only with your own devices."
    ),
)


def get_service(request: Request) -> SyncService:
    return request.app.state.service


Service = Annotated[SyncService, Depends(get_service)]


def current_user(
    service: Service,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    if credentials is None:
        raise Problem(401, "unauthorized", "A valid bearer token is required.")
    return service.authenticate(credentials.credentials)


User = Annotated[str, Depends(current_user)]
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[int, Query(ge=0, le=9_223_372_036_854_775_807)]
VersionPath = Annotated[int, Path(ge=1, le=2_147_483_647)]
MUTATION_RESPONSES = {
    201: {"model": SyncResult, "description": "Created a new document."},
    404: {"model": SyncResult | ErrorResponse, "description": "Unknown resource or device."},
    409: {"model": SyncResult, "description": "Saved conflict or rejected unsafe mutation."},
    422: {
        "model": SyncResult | ErrorResponse,
        "description": "Invalid data or resolution choices.",
    },
}


def response(reply: Reply) -> JSONResponse:
    return JSONResponse(
        reply.body,
        status_code=reply.status,
        headers={"Idempotency-Replayed": str(reply.replayed).lower()},
    )


@router.post("/users", status_code=201, response_model=UserRegistered, tags=["Identity"])
def register_user(body: UserCreate, service: Service):
    """Create a demo workspace; save the returned token because it is not recoverable."""
    return service.register_user(body.name)


@router.put("/devices/{device_id}", response_model=DeviceView, tags=["Identity"])
def register_device(device_id: UUID, body: DeviceRegistration, service: Service, user: User):
    """Register a client-generated device UUID, or idempotently rename that device."""
    return service.register_device(user, str(device_id), body.name)


@router.post("/sync", response_model=SyncResult, responses=MUTATION_RESPONSES, tags=["Sync"])
def synchronize(body: SyncRequest, service: Service, user: User):
    """Create with base_version=0, or three-way merge an edit against a saved base version."""
    return response(service.sync(user, body))


@router.get("/documents", response_model=DocumentPage, tags=["Documents"])
def list_documents(service: Service, user: User, after: UUID | None = None, limit: Limit = 50):
    """Browse current documents. Bootstrap reliable synchronization with /changes?after=0."""
    return service.list_documents(user, str(after) if after else "", limit)


@router.get("/documents/{document_id}", response_model=DocumentView, tags=["Documents"])
def get_document(document_id: UUID, service: Service, user: User):
    return service.get_document(user, str(document_id))


@router.get("/documents/{document_id}/history", response_model=HistoryPage, tags=["History"])
def get_history(
    document_id: UUID, service: Service, user: User, after: Cursor = 0, limit: Limit = 50
):
    """Read immutable versions in ascending order; continue after next_version."""
    return service.history(user, str(document_id), after, limit)


@router.get(
    "/documents/{document_id}/versions/{version}", response_model=RevisionView, tags=["History"]
)
def get_version(document_id: UUID, version: VersionPath, service: Service, user: User):
    return service.get_revision(user, str(document_id), version)


@router.post(
    "/documents/{document_id}/restore",
    response_model=SyncResult,
    responses=MUTATION_RESPONSES,
    tags=["History"],
)
def restore_version(document_id: UUID, body: RestoreRequest, service: Service, user: User):
    """Restore a snapshot as a new version only if expected_version is still current."""
    return response(service.restore(user, str(document_id), body))


@router.get("/changes", response_model=ChangePage, tags=["Sync"])
def get_changes(service: Service, user: User, after: Cursor = 0, limit: Limit = 50):
    """Pull accepted snapshots in commit order; persist next_cursor after processing the page."""
    return service.changes(user, after, limit)


@router.get("/conflicts/{conflict_id}", response_model=ConflictView, tags=["Conflicts"])
def get_conflict(conflict_id: UUID, service: Service, user: User):
    """Read the saved proposal plus its refreshed comparison with the current document."""
    return service.get_conflict(user, str(conflict_id))


@router.post(
    "/conflicts/{conflict_id}/resolve",
    response_model=SyncResult,
    responses=MUTATION_RESPONSES,
    tags=["Conflicts"],
)
def resolve_conflict(conflict_id: UUID, body: ResolutionRequest, service: Service, user: User):
    """Choose server/client for every current conflicting field using an expected-version guard."""
    return response(service.resolve(user, str(conflict_id), body))
