"""Reject strings that cannot fit their columns before a route writes anything.

The HTTP cases use the real application and its normal mock-DB test harness.
They check request validation, not PostgreSQL's truncation exception. Boundary
cases also pin the existing empty-string and nullable-field behavior.
"""
import uuid

import pytest

from apps.api.models.asset import Asset, MediaFile
from apps.api.models.metadata import Collection, MetadataField
from apps.api.models.project import Project
from apps.api.models.share import ShareLink
from apps.api.models.user import GuestUser, User
from apps.api.routers.setup import CreateSuperAdminRequest
from apps.api.schemas.asset import AssetUpdate
from apps.api.schemas.comment import GuestCommentCreate
from apps.api.schemas.metadata import CollectionCreate, MetadataFieldCreate
from apps.api.schemas.project import ProjectCreate, ProjectUpdate
from apps.api.schemas.share import MultiShareCreate, ShareLinkCreate, ShareLinkUpdate
from apps.api.schemas.upload import InitiateUploadRequest


ID = str(uuid.UUID(int=1))
UPLOAD = {
    "project_id": ID,
    "asset_name": "Clip",
    "original_filename": "clip.mp4",
    "mime_type": "video/mp4",
    "file_size_bytes": 1,
}

# method, path, schema, valid body, field, corresponding database column
CASES = [
    ("POST", "/setup/create-superadmin", CreateSuperAdminRequest,
     {"email": "new@example.com", "password": "test-password", "name": "Name"},
     "name", User.name),
    ("POST", "/projects", ProjectCreate, {"name": "Project"}, "name", Project.name),
    ("PATCH", f"/projects/{ID}", ProjectUpdate, {}, "name", Project.name),
    ("POST", f"/projects/{ID}/collections", CollectionCreate,
     {"name": "Collection"}, "name", Collection.name),
    ("POST", f"/projects/{ID}/metadata-fields", MetadataFieldCreate,
     {"name": "Field", "field_type": "text"}, "name", MetadataField.name),
    ("PATCH", f"/assets/{ID}", AssetUpdate, {}, "name", Asset.name),
    ("POST", "/upload/initiate", InitiateUploadRequest, UPLOAD, "asset_name", Asset.name),
    ("POST", f"/assets/{ID}/share", ShareLinkCreate, {}, "title", ShareLink.title),
    ("POST", f"/folders/{ID}/share", ShareLinkCreate, {}, "title", ShareLink.title),
    ("POST", f"/projects/{ID}/share", ShareLinkCreate, {}, "title", ShareLink.title),
    ("POST", f"/projects/{ID}/share/multi", MultiShareCreate,
     {"asset_ids": [ID]}, "title", ShareLink.title),
    ("PATCH", "/share/test-token", ShareLinkUpdate, {}, "title", ShareLink.title),
    ("POST", "/share/test-token/comment", GuestCommentCreate,
     {"body": "Comment"}, "guest_name", GuestUser.name),
    ("POST", "/share/test-token/comment", GuestCommentCreate,
     {"body": "Comment"}, "guest_email", GuestUser.email),
    ("POST", "/upload/initiate", InitiateUploadRequest,
     UPLOAD, "original_filename", MediaFile.original_filename),
    ("POST", f"/assets/{ID}/versions", InitiateUploadRequest,
     UPLOAD, "original_filename", MediaFile.original_filename),
]
CASE_IDS = [f"{method} {path} {field}" for method, path, _, _, field, _ in CASES]


@pytest.mark.parametrize("method,path,schema,body,field,column", CASES, ids=CASE_IDS)
def test_overlong_strings_are_rejected_before_writes(
    client, mock_db, test_user, monkeypatch, method, path, schema, body, field, column,
):
    from apps.api.main import app
    from apps.api.middleware.auth import get_current_user, get_optional_user
    from apps.api.middleware.global_rate_limit import GlobalRateLimitMiddleware

    app.dependency_overrides[get_current_user] = lambda: test_user
    app.dependency_overrides[get_optional_user] = lambda: None
    monkeypatch.setattr(GlobalRateLimitMiddleware, "_check", lambda *a: (True, 0))
    monkeypatch.setattr(
        "apps.api.middleware.rate_limit.check_rate_limit", lambda *a, **k: (True, 0)
    )
    limit = column.property.columns[0].type.length
    response = client.request(method, path, json={**body, field: "x" * (limit + 1)})

    assert response.status_code == 422, response.text
    assert any(
        error["loc"] == ["body", field]
        and error["type"] == "string_too_long"
        and error["ctx"]["max_length"] == limit
        for error in response.json()["detail"]
    )
    mock_db.add.assert_not_called()
    mock_db.flush.assert_not_called()
    mock_db.commit.assert_not_called()


@pytest.mark.parametrize("method,path,schema,body,field,column", CASES, ids=CASE_IDS)
@pytest.mark.parametrize("character", ["x", "界", "😀"])
def test_column_length_is_accepted_in_characters(
    method, path, schema, body, field, column, character,
):
    value = character * column.property.columns[0].type.length
    assert getattr(schema.model_validate({**body, field: value}), field) == value


@pytest.mark.parametrize("method,path,schema,body,field,column", CASES, ids=CASE_IDS)
def test_empty_strings_keep_their_existing_schema_behavior(method, path, schema, body, field, column):
    assert getattr(schema.model_validate({**body, field: ""}), field) == ""


@pytest.mark.parametrize("schema,body,field", [
    (ProjectUpdate, {}, "name"),
    (AssetUpdate, {}, "name"),
    (ShareLinkCreate, {}, "title"),
    (MultiShareCreate, {"asset_ids": [ID]}, "title"),
    (ShareLinkUpdate, {}, "title"),
    (GuestCommentCreate, {"body": "Comment"}, "guest_name"),
    (GuestCommentCreate, {"body": "Comment"}, "guest_email"),
])
def test_optional_strings_still_accept_null_and_omission(schema, body, field):
    assert getattr(schema.model_validate(body), field) is None
    assert getattr(schema.model_validate({**body, field: None}), field) is None


@pytest.mark.parametrize("email", ["İ" * 128, "İ" * 250 + "@a.co"], ids=["expanded-256", "expanded-505"])
def test_guest_email_cannot_overflow_after_route_lowercases_it(client, mock_db, email):
    assert len(email) <= 255 < len(email.lower())
    response = client.post(
        "/share/test-token/comment",
        json={"body": "Comment", "guest_name": "Guest", "guest_email": email},
    )
    assert response.status_code == 422, response.text
    assert any(error["loc"] == ["body", "guest_email"] for error in response.json()["detail"])
    mock_db.add.assert_not_called()
    mock_db.flush.assert_not_called()
    mock_db.commit.assert_not_called()


@pytest.mark.parametrize(
    "email", ["İ" * 125 + "@a.co", "Guest@Example.COM", "not-an-email"],
    ids=["normalized-limit", "mixed-case", "format-unchanged"],
)
def test_guest_email_length_check_preserves_value_and_format_semantics(email):
    assert len(email.lower()) <= 255
    body = GuestCommentCreate(body="Comment", guest_email=email)
    assert body.guest_email == email
