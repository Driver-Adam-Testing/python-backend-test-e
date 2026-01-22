from typing import Any
from uuid import UUID

from pydantic import BaseModel, field_validator


class UploadRequest(BaseModel):
    file_path: str

    @field_validator("file_path")
    def must_be_zip_or_pdf(cls, v: Any) -> Any:
        if not v.lower().endswith(".zip") and not v.lower().endswith(".pdf"):
            raise ValueError("file_path must be a zip file or pdf")
        return v


class UploadAutoDocConfigRequest(BaseModel):
    page_version_node_id: UUID
    file_path: str

    @field_validator("file_path")
    def must_be_toml(cls, v: Any) -> Any:
        if not v.lower().endswith(".toml"):
            raise ValueError("file_path must be a toml")
        return v


class UploadResponse(BaseModel):
    upload_url: str
    primary_asset_id: UUID
    version_id: UUID


class UploadAutoDocConfigResponse(BaseModel):
    upload_url: str
