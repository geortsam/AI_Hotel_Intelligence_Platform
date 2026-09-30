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
    copilot_enabled: bool = Field(
        description=(
            "Whether this deployment has the copilot switched on -- the value of its "
            "`llm_enabled` setting, and nothing else. False means every question is refused "
            "with LLM_DISABLED. True does not promise an answer: a question can still be "
            "refused while the provider is unavailable or an allowance is spent. Names no "
            "provider, model or credential."
        )
    )
