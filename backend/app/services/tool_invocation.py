"""Where a model's tool call meets this application's rules. One call, one audit event.

Stage 7.6. Every tool call a model makes passes through :meth:`ToolInvocationService.invoke`, in
this order, and no step can be skipped by anything the model says:

    1. resolve the hotel          the request's own hotel, via HotelScopeResolver -- 404 for a
                                  hotel that does not exist or the caller cannot see
    2. resolve the name           an exact registry lookup, and only among the names this
                                  request offered; anything else is `unknown_tool`
    3. check the role             the tool's declared minimum, BEFORE its arguments are even
                                  parsed and before any service runs; refusal is `forbidden`
    4. validate the arguments     the tool's input model, `extra="forbid"`; `invalid_arguments`
    5. run the tool               which delegates to one existing service method; a typed
                                  AppError is `failed`, anything else is `error`
    6. validate the output        the tool's output model -- nothing unvalidated reaches a model
    7. audit                      `tool.invoked` on the existing append-only trail, then commit
    8. admit its evidence         Stage 7.10: source labels the tool staged become citable only
                                  now, after the output that carries them has been validated and
                                  accounted for; a call that failed at any step leaves none

## The hotel is a parameter of this method, and never of the tool call

`hotel_public_id` is the caller's -- the hotel the authenticated request path named and the
scope resolver accepted. The model's `arguments` are validated against a schema that has no
field in which a hotel could be named and that refuses unknown keys, so a model writing
`{"hotel_id": ...}` gets `invalid_arguments`, audited, and nothing else. The tool then receives
a `ToolContext` built from this method's parameter, and every service it delegates to resolves
that same hotel again through the same resolver. There is no path by which model output selects
a hotel.

## The audit event, and what it does not contain

One ``tool.invoked`` event per call that reached a resolved hotel, whatever its outcome:

| Column / key | Value |
|---|---|
| ``hotel_id`` | the resolved hotel -- never one named by the model |
| ``actor_user_id`` | the authenticated caller, bound into the trail at construction |
| ``resource_type`` / ``resource_reference`` | ``tool`` / the **registered** name, or ``unknown`` |
| ``request_id`` | the existing correlation id, read by the trail from its ContextVar |
| ``outcome`` | one of six literals |
| ``error_code`` | the public machine code of a failure, or null on success |
| ``duration_ms`` | whole milliseconds, measured on a monotonic clock |
| ``arguments_sha256`` | a fingerprint of the arguments -- never the arguments |

**Not recorded:** the user's question, any answer, any prompt, any tool output, the arguments
themselves, the name a model asked for when it was not a registered one, and any identifier a
model supplied. §4.4: "the trail is an operational record, and free text there is a
data-retention liability."

A call for a hotel that does not resolve is not audited here: there is no hotel to attribute it
to, and the 404 propagates exactly as it would from any route.

## The unit of work

The audit event is staged on this service's session and committed by this service -- the rule
every writing service follows. `get_demand_forecast`'s service commits its own recorded
prediction first; the audit commit follows it. If recording the audit event fails, the error
propagates and **no result is returned to the loop**: a tool result nobody can account for does
not reach a model. An integrity failure at that commit is classified by `_translate` exactly as
every other audit writer classifies one (Stage 4.5.16): an audit-relation failure is this
server's `internal_fault`, never something attributed to the caller.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable, Collection, Mapping
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.copilot.citations import EvidenceLedger
from app.copilot.contracts import (
    INTERNAL_ERROR_CODE,
    INVALID_ARGUMENTS_CODE,
    UNKNOWN_TOOL_CODE,
    Outcome,
    ToolContext,
    ToolOutcome,
    ToolServices,
)
from app.copilot.registry import RegisteredTool, ToolRegistry
from app.core.errors import (
    GENERIC_CONFLICT_MESSAGE,
    GENERIC_SERVER_MESSAGE,
    AppError,
    ConflictError,
    internal_fault,
    is_audit_integrity_failure,
    sqlstate_of,
)
from app.models.enums import AuditAction, AuditResourceType
from app.services.audit import AuditTrail
from app.services.scope import HotelScopeResolver

logger = logging.getLogger(__name__)

#: The code the scope resolver's refusal carries. Classified by code rather than by class, the
#: same way every other layer module avoids naming the authorization error it did not raise.
REFUSED_CODE = "FORBIDDEN"

#: The audit reference for a call that named no registered, offered tool. A literal: the name the
#: model supplied is never stored.
UNKNOWN_REFERENCE = "unknown"

_MESSAGES: dict[str, str] = {
    UNKNOWN_TOOL_CODE: "No such tool is available.",
    INVALID_ARGUMENTS_CODE: "The arguments do not match the tool's input schema.",
    INTERNAL_ERROR_CODE: GENERIC_SERVER_MESSAGE,
}


def arguments_sha256(arguments: Any) -> str:
    """SHA-256 over the canonical JSON of *arguments*: sorted keys, no whitespace, ASCII.

    Canonical so that the same arguments always fingerprint the same way and two identical calls
    can be recognised in the trail. `default=str` so that a value JSON cannot encode is
    fingerprinted rather than crashing the audit of the call that sent it.
    """
    canonical = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


class ToolInvocationService:
    """Binds, authorizes, validates, runs and audits one tool call. See the module docstring."""

    def __init__(
        self,
        session: Session,
        registry: ToolRegistry,
        scope: HotelScopeResolver,
        audit: AuditTrail,
        services: ToolServices,
        *,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._session = session
        self._registry = registry
        self._scope = scope
        self._audit = audit
        self._services = services
        self._monotonic = monotonic

    def permitted_tools(self, hotel_public_id: uuid.UUID) -> tuple[str, ...]:
        """The registered names the caller's role at this hotel allows, sorted.

        This is what a request's catalogue is built from, so the model is only ever offered
        tools the caller could use. The role comparison is the resolver's, asked once per tool;
        a refusal excludes the tool and anything else propagates. The hotel is resolved first,
        so a hotel the caller cannot see is a 404 here, exactly as on any route.
        """
        self._scope.require_hotel(hotel_public_id)
        permitted: list[str] = []
        for contract in self._registry.contracts():
            try:
                self._scope.require_hotel_with_role(hotel_public_id, contract.min_role)
            except AppError as refused:
                if refused.code != REFUSED_CODE:
                    raise
                continue
            permitted.append(contract.name)
        return tuple(permitted)

    def invoke(
        self,
        hotel_public_id: uuid.UUID,
        name: str,
        arguments: Mapping[str, Any],
        *,
        offered: Collection[str],
        evidence: EvidenceLedger | None = None,
    ) -> ToolOutcome:
        """Run one model-requested call against the caller's hotel. Always audited once resolved.

        *name* and *arguments* are untrusted model output. *offered* is the set of names this
        request's catalogue contained; a registered tool that was not offered is treated as
        unknown, so a model cannot reach a tool by guessing a name it was not shown.

        *evidence* is the calling request's ledger of citable excerpts. Without one, the call gets
        a fresh ledger that nothing else can read -- evidence is never shared between requests.
        """
        ledger = evidence if evidence is not None else EvidenceLedger()
        started = self._monotonic()
        hotel = self._scope.require_hotel(hotel_public_id)
        hotel_id = hotel.id
        fingerprint = arguments_sha256(arguments)

        tool = self._resolve(name, offered)
        if tool is None:
            return self._finish(
                hotel_id, None, "unknown_tool", started, fingerprint, code=UNKNOWN_TOOL_CODE
            )
        contract = tool.contract

        try:
            self._scope.require_hotel_with_role(hotel_public_id, contract.min_role)
        except AppError as refused:
            if refused.code != REFUSED_CODE:
                raise
            return self._finish(
                hotel_id,
                contract.name,
                "forbidden",
                started,
                fingerprint,
                code=refused.code,
                message=refused.message,
            )

        try:
            parsed = contract.input_model.model_validate(dict(arguments))
        except (PydanticValidationError, TypeError, ValueError):
            return self._finish(
                hotel_id,
                contract.name,
                "invalid_arguments",
                started,
                fingerprint,
                code=INVALID_ARGUMENTS_CODE,
            )

        context = ToolContext(
            hotel_public_id=hotel_public_id, services=self._services, evidence=ledger
        )
        try:
            result = tool.run(context, parsed)
            output = contract.output_model.model_validate(result).model_dump(mode="json")
        except AppError as failed:
            ledger.discard()
            self._session.rollback()
            return self._finish(
                hotel_id,
                contract.name,
                "failed",
                started,
                fingerprint,
                code=failed.code,
                message=failed.message,
            )
        except Exception as unexpected:
            # Never a raw exception to the model. Logged for an operator by TYPE only: no
            # traceback, because a driver error's traceback can carry the row it failed on --
            # the rule `test_no_service_logs_a_driver_exception` enforces for every service.
            ledger.discard()
            self._session.rollback()
            logger.error(
                "tool %s raised %s",
                contract.name,
                type(unexpected).__name__,
                extra={"tool": contract.name, "error_type": type(unexpected).__name__},
            )
            return self._finish(
                hotel_id, contract.name, "error", started, fingerprint, code=INTERNAL_ERROR_CODE
            )

        try:
            outcome = self._finish(
                hotel_id, contract.name, "succeeded", started, fingerprint, output
            )
        except Exception:
            ledger.discard()
            raise
        ledger.admit()
        return outcome

    # --- internals ----------------------------------------------------------------------------

    def _resolve(self, name: object, offered: Collection[str]) -> RegisteredTool | None:
        """The registered tool for *name*, only if this request offered it. Fails closed."""
        if not isinstance(name, str) or name not in offered or name not in self._registry:
            return None
        return self._registry.get(name)

    def _finish(
        self,
        hotel_id: int,
        tool: str | None,
        outcome: Outcome,
        started: float,
        fingerprint: str,
        output: dict[str, Any] | None = None,
        *,
        code: str | None = None,
        message: str | None = None,
    ) -> ToolOutcome:
        """Record the event, commit it, and only then hand the outcome back."""
        duration_ms = round((self._monotonic() - started) * 1000)
        try:
            # A dict literal, so every key is reviewable at the call site -- the audit layering
            # suite reads these keys from the source and checks them against the approved set.
            self._audit.record(
                AuditAction.TOOL_INVOKED,
                AuditResourceType.TOOL,
                tool if tool is not None else UNKNOWN_REFERENCE,
                hotel_id=hotel_id,
                details={
                    "outcome": outcome,
                    "error_code": code,
                    "duration_ms": duration_ms,
                    "arguments_sha256": fingerprint,
                },
            )
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            raise self._translate(exc) from exc
        except Exception:
            self._session.rollback()
            raise

        return ToolOutcome(
            tool=tool,
            outcome=outcome,
            output=output,
            error_code=code,
            error_message=None if code is None else (message or _MESSAGES.get(code, message)),
        )

    def _translate(self, exc: IntegrityError) -> Exception:
        """An integrity failure at this service's commit, classified the one way every writer does.

        The only row this service writes is the audit event, so an audit-relation failure --
        the actor's foreign key, a vocabulary CHECK -- is the expected case, and it is this
        server's fault: `internal_fault`, a 500 carrying no relation or constraint name. It is
        checked FIRST, exactly as Stage 4.5.16 requires of every audit writer, so nothing below
        can attribute it to anything else.

        Anything else reaching this commit is not a row this service wrote, and is reported as
        the generic conflict every other writer falls back to rather than guessed at.
        """
        # No ``exc_info``: the driver renders the offending row into its message.
        logger.warning("Tool invocation integrity error (sqlstate=%s)", sqlstate_of(exc))

        if is_audit_integrity_failure(exc):
            return internal_fault(exc)

        return ConflictError(GENERIC_CONFLICT_MESSAGE)


__all__ = ["REFUSED_CODE", "UNKNOWN_REFERENCE", "ToolInvocationService", "arguments_sha256"]
