"""The security-header policy: which headers, which values, and when.

Pure decisions, no ASGI. Kept beside the other ``app.core`` policy modules and separate from
the middleware that applies them, exactly as :mod:`app.core.request_id` is separate from
:mod:`app.middleware.request_id`. The split earns its keep immediately: the one error handler
that runs OUTSIDE all user middleware -- Starlette routes the bare ``Exception`` handler to
ServerErrorMiddleware, which wraps everything -- has to apply the same headers itself, and it
can import this without ``app.core`` reaching into ``app.middleware``.

**Why these four are unconditional.** ``nosniff`` stops a browser second-guessing the declared
content type, which is what turns a JSON body containing attacker text into executed HTML.
``DENY`` and ``frame-ancestors`` stop the API being framed. ``no-referrer`` keeps full request
URLs -- which for this API contain public resource identifiers -- out of the ``Referer`` sent
to third parties. The ``Permissions-Policy`` denies device capabilities an API has no use for,
so a response that is somehow rendered as a document cannot reach for a camera.

**The CSP decision.** A blanket "APIs do not need CSP" would be wrong here and a copied
frontend CSP would be worse, so the policy contains only directives that mean something for a
non-document response: ``default-src 'none'`` (nothing may be loaded if a response is ever
interpreted as a document), ``frame-ancestors 'none'`` (the modern, non-deprecated form of
``X-Frame-Options``), ``base-uri 'none'`` and ``form-action 'none'``. There is deliberately no
``script-src``/``style-src``/``img-src`` reasoning, because this application serves no
document those would apply to. The one exception is the interactive documentation: Swagger UI
and ReDoc are real HTML pages that load their assets from a CDN, and ``default-src 'none'``
would render them blank. Those exact paths -- taken from the application object, not
re-typed -- are exempted, and they exist only where ``docs_enabled`` is true, which is
everywhere except production. In production this application serves no HTML at all, so the
policy applies to every response without exception.

**The HSTS decision.** HSTS is emitted only for a production environment on a request the
ORIGINAL client made over HTTPS. Both halves are load-bearing. Sending it in development would
teach a developer's browser to refuse ``http://localhost`` for a year, which is a hard problem
to undo and has nothing to do with securing anything. Deciding on the original client's scheme
rather than on ``scope["scheme"]`` is what makes it work behind a TLS-terminating proxy, where
the connection we see is plain HTTP -- and that scheme is resolved under the same trusted-proxy
rule as the client address, because ``X-Forwarded-Proto`` is exactly as forgeable.
"""

from __future__ import annotations

from typing import Final

from app.core.config import Settings

#: Denied outright. An API has no use for any of them, and a browser ignores names it does not
#: recognise, so listing a capability that a given browser lacks costs nothing.
PERMISSIONS_POLICY: Final = (
    "accelerometer=(), ambient-light-sensor=(), autoplay=(), battery=(), camera=(), "
    "display-capture=(), encrypted-media=(), fullscreen=(), geolocation=(), gyroscope=(), "
    "magnetometer=(), microphone=(), midi=(), payment=(), picture-in-picture=(), "
    "publickey-credentials-get=(), screen-wake-lock=(), usb=(), xr-spatial-tracking=()"
)

#: Only directives that are meaningful for a response that is not a document. See the module
#: docstring for why there is no script-src or style-src here.
CONTENT_SECURITY_POLICY: Final = (
    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)

#: Sent on every response regardless of environment or scheme.
BASE_SECURITY_HEADERS: Final[dict[str, str]] = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "permissions-policy": PERMISSIONS_POLICY,
}

STRICT_TRANSPORT_SECURITY: Final = "strict-transport-security"
CONTENT_SECURITY_POLICY_HEADER: Final = "content-security-policy"


def hsts_value(settings: Settings) -> str:
    """The HSTS policy string, built from configuration.

    ``preload`` is opt-in and defaults off. Submitting a domain to the preload list is close to
    irreversible -- removal takes months to reach users -- so it is not something a framework
    should turn on for a deployment that merely enabled HTTPS.
    """
    parts = [f"max-age={settings.hsts_max_age}"]
    if settings.hsts_include_subdomains:
        parts.append("includeSubDomains")
    if settings.hsts_preload:
        parts.append("preload")
    return "; ".join(parts)


def hsts_applies(settings: Settings, scheme: str) -> bool:
    """Whether this response should carry HSTS.

    Production only, HTTPS only. A max-age of zero is treated as "switched off" rather than
    emitted, because ``max-age=0`` actively tells a browser to FORGET an existing policy, and
    a deployment that zeroes the setting means "do not send this", not "undo it everywhere".
    """
    return settings.environment == "production" and scheme == "https" and settings.hsts_max_age > 0


def security_headers(
    settings: Settings, scheme: str, *, include_csp: bool = True
) -> dict[str, str]:
    """Every security header this response should carry, as lowercase names.

    Returned as a mapping rather than applied in place so the same decision can be reused by
    the one error handler that produces its response outside this middleware.
    """
    headers = dict(BASE_SECURITY_HEADERS)
    if include_csp:
        headers[CONTENT_SECURITY_POLICY_HEADER] = CONTENT_SECURITY_POLICY
    if hsts_applies(settings, scheme):
        headers[STRICT_TRANSPORT_SECURITY] = hsts_value(settings)
    return headers


__all__ = [
    "BASE_SECURITY_HEADERS",
    "CONTENT_SECURITY_POLICY",
    "CONTENT_SECURITY_POLICY_HEADER",
    "PERMISSIONS_POLICY",
    "STRICT_TRANSPORT_SECURITY",
    "hsts_applies",
    "hsts_value",
    "security_headers",
]
