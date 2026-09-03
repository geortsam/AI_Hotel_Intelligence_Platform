"""API metadata contract."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ApiMetaResponse(BaseModel):
    """Identifies the API and points at its documentation."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(description="Application name.")
    version: str = Field(description="Running code version.")
    api_version: str = Field(description="Version of this API surface, e.g. 'v1'.")
    documentation: str | None = Field(
        default=None, description="Path to interactive docs; null when disabled."
    )
