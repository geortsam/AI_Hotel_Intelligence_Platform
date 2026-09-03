"""API-root metadata for version 1.

Not a domain endpoint: it exposes no business data and touches no table. It exists so the
version prefix is a real, discoverable resource rather than an empty namespace, which also
makes router registration verifiable by a test.
"""

from __future__ import annotations

from fastapi import APIRouter

from app import __version__
from app.api.deps import SettingsDep
from app.schemas.meta import ApiMetaResponse

router = APIRouter(tags=["meta"])


@router.get(
    "/",
    response_model=ApiMetaResponse,
    summary="API metadata",
    description="Identifies this API version and links to its documentation.",
)
def api_meta(settings: SettingsDep) -> ApiMetaResponse:
    return ApiMetaResponse(
        name=settings.app_name,
        version=__version__,
        api_version="v1",
        documentation="/docs" if settings.docs_enabled else None,
    )
