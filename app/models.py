from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    field_serializer,
    model_validator,
)

Name = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=80)
]
Title = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=200)
]
Content = Annotated[str, StringConstraints(strict=True, max_length=100_000)]
Tag = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=40)
]
Tags = Annotated[list[Tag], Field(strict=True, max_length=20)]
Version = Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]
BaseVersion = Annotated[int, Field(strict=True, ge=0, le=2_147_483_647)]
FieldName = Literal["title", "content", "tags", "archived"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def valid_unicode(self) -> Self:
        values = list(self.model_dump().values())
        while values:
            value = values.pop()
            if isinstance(value, str):
                try:
                    value.encode("utf-8")
                except UnicodeEncodeError as error:
                    raise ValueError("strings must contain valid Unicode characters") from error
            elif isinstance(value, dict):
                values.extend(value.values())
            elif isinstance(value, list):
                values.extend(value)
        return self


class UserCreate(Model):
    name: Name


class UserRegistered(Model):
    user_id: UUID
    name: str
    access_token: str
    token_type: Literal["bearer"] = "bearer"


class DeviceRegistration(Model):
    name: Name


class DeviceView(Model):
    device_id: UUID
    name: str
    created_at: str


class DocumentData(Model):
    title: Title
    content: Content = ""
    tags: Tags = Field(default_factory=list)
    archived: StrictBool = False

    @model_validator(mode="after")
    def unique_tags(self) -> Self:
        if len(self.tags) != len(set(self.tags)):
            raise ValueError("tags must be unique")
        return self


class DocumentPatch(Model):
    title: Title | None = None
    content: Content | None = None
    tags: Tags | None = None
    archived: StrictBool | None = None

    @model_validator(mode="after")
    def nonempty_and_nonnull(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("changes must contain at least one field")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("explicit null is not allowed; omit unchanged fields instead")
        if self.tags is not None and len(self.tags) != len(set(self.tags)):
            raise ValueError("tags must be unique")
        return self

    def supplied(self) -> dict:
        return self.model_dump(exclude_unset=True)


class Mutation(Model):
    request_id: UUID
    device_id: UUID


class SyncRequest(Mutation):
    document_id: UUID
    base_version: BaseVersion
    changes: DocumentPatch


class ResolutionRequest(Mutation):
    expected_version: Version
    resolutions: dict[FieldName, Literal["server", "client"]]


class RestoreRequest(Mutation):
    expected_version: Version
    target_version: Version


class DocumentView(Model):
    document_id: UUID
    version: int
    data: DocumentData
    updated_at: str
    device_id: UUID
    request_id: UUID


class FieldConflict(Model):
    base: str | list[str] | bool
    server: str | list[str] | bool
    client: str | list[str] | bool


class SyncResult(Model):
    outcome: Literal["accepted", "merged", "conflict", "rejected"]
    code: str
    message: str
    request_id: UUID
    document: DocumentView | None = None
    changed_fields: list[FieldName] = Field(default_factory=list)
    stale: bool = False
    conflict_id: UUID | None = None
    conflicts: dict[FieldName, FieldConflict] = Field(default_factory=dict)


class RevisionView(DocumentView):
    cursor: int
    kind: Literal["created", "updated", "merged", "resolved", "restored"]
    base_version: int
    restored_from: int | None


class HistoryPage(Model):
    items: list[RevisionView]
    next_version: int
    has_more: bool


class ChangePage(Model):
    items: list[RevisionView]
    next_cursor: int
    has_more: bool


class DocumentPage(Model):
    items: list[DocumentView]
    next_document_id: UUID | None
    has_more: bool


class ConflictView(Model):
    conflict_id: UUID
    document_id: UUID
    device_id: UUID
    request_id: UUID
    status: Literal["open", "resolved"]
    base_version: int
    server_version_at_detection: int
    proposed_changes: DocumentPatch
    original_conflicts: dict[FieldName, FieldConflict]
    current_conflicts: dict[FieldName, FieldConflict]
    base_document: DocumentView
    current_document: DocumentView
    created_at: str
    resolved_at: str | None
    resolution_request_id: UUID | None

    @field_serializer("proposed_changes")
    def serialize_proposal(self, value: DocumentPatch) -> dict:
        return value.supplied()


class ValidationIssue(Model):
    location: list[str | int]
    message: str
    type: str


class ErrorResponse(Model):
    outcome: Literal["rejected"] = "rejected"
    code: str
    message: str
    errors: list[ValidationIssue] = Field(default_factory=list)
