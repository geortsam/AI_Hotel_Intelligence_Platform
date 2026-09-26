"""What a tool IS, as data. `docs/v2-architecture.md` §7.3's template, made into types.

Stage 7.6. Every field §7.3 lists for a tool has a home here, and a test per field:

- **name** — `ToolContract.name`: the registry key, and the audit reference.
- **purpose** — `ToolContract.description`: all the model is told besides the schema.
- **input schema** — `ToolContract.input_model`: Pydantic, `extra="forbid"`, **no tenant
  identifier**.
- **output schema** — `ToolContract.output_model`: the service response minus
  `hotel_public_id`.
- **authorization** — `ToolContract.min_role`: checked by `ToolInvocationService` before the
  tool runs.
- **tenant scope** — `ToolContext.hotel_public_id`: from the authorized request, never from the
  model.
- **citable evidence** — `ToolContext.evidence` (Stage 7.10): the request's own ledger of the
  excerpts a search returned, by source label. Only the knowledge tool writes to it.
- **side effects** — `ToolContract.side_effect`: declared, and tested against what the tool does.
- **data restrictions** — `ToolContract.withheld`: the response fields dropped, with the reason.
- **error behaviour** — `ToolOutcome`: a typed outcome with a public code; never a raw exception.
- **audit** — `tool.invoked` / `tool`, recorded by `ToolInvocationService` for every call.

## Where the hotel comes from

`ToolContext` is the only way a tool learns which hotel it is working for, and it is built by the
caller from the hotel the request path resolved — never from a model's arguments. A tool's
input model cannot carry a hotel: the registry refuses to register one whose JSON schema names a
hotel, tenant or property anywhere, and every input model forbids extra keys, so an argument
the model invents is refused rather than ignored.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.copilot.citations import EvidenceLedger
from app.models.enums import HotelRole
from app.schemas.analytics import MAX_RANGE_DAYS
from app.services.analytics import AnalyticsService
from app.services.insight import InsightService
from app.services.knowledge import KnowledgeService
from app.services.ml_performance import ForecastPerformanceService
from app.services.ml_serving import DemandPredictionService

#: What a tool may do besides read. A closed set of two.
#:
#: `records_served_prediction` exists for exactly one tool, `get_demand_forecast`, and is the
#: decision recorded in Stage 7.6: the demand service records every prediction it serves (Stage
#: 6.8) so that it can later be scored against what happened, and a forecast served to a model is
#: a served forecast. The write is idempotent — `uq_demand_predictions_identity` makes a repeat
#: of the same forecast a no-op — and it is not a business write: no booking, payment, member or
#: catalogue entry changes. It is declared rather than hidden, and a test asserts that every
#: other tool writes nothing.
SideEffect = Literal["none", "records_served_prediction"]

#: How one invocation ended. Closed, written into the audit trail, and returned to the loop.
Outcome = Literal[
    "succeeded",
    "unknown_tool",
    "forbidden",
    "invalid_arguments",
    "failed",
    "error",
]

#: The public codes an outcome that is not a service's own AppError is reported under.
UNKNOWN_TOOL_CODE = "UNKNOWN_TOOL"
INVALID_ARGUMENTS_CODE = "INVALID_ARGUMENTS"
INTERNAL_ERROR_CODE = "INTERNAL_ERROR"

#: Every tool output drops this, and says so in `withheld`. The model is working for one hotel
#: that it did not choose and cannot change; echoing that hotel's identifier back to it would give
#: it an identifier to repeat, with nothing gained.
HOTEL_IDENTIFIER_FIELD = "hotel_public_id"


class ToolArguments(BaseModel):
    """The base every tool input model inherits. Unknown keys are refused, not ignored.

    `extra="forbid"` is the structural half of the tenant rule: an argument named `hotel_id`
    that a model invents does not get silently dropped — it fails validation, and the failure is
    audited as `invalid_arguments`. Frozen so a validated argument set cannot be edited between
    validation and delegation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolOutput(BaseModel):
    """The base every tool output model inherits. Unknown keys are refused, not passed through.

    A service response that grows a field fails validation here until someone decides whether the
    model may see it — which is the point of §7.4's "tool output is validated against its schema
    before reaching the model".
    """

    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())


class DateRangeArguments(ToolArguments):
    """A bounded, inclusive date range. The three analytics tools share it.

    The span limit is enforced by the analytics service itself, which is the single source of
    that rule; the description repeats it so the model is told before it asks. A request outside
    it comes back as a typed `VALIDATION_ERROR` outcome, which is returned to the model.
    """

    date_from: dt.date = Field(description="First day of the range, inclusive (YYYY-MM-DD).")
    date_to: dt.date = Field(
        description=(
            f"Last day of the range, inclusive (YYYY-MM-DD). At most {MAX_RANGE_DAYS} days "
            "after date_from."
        )
    )


@dataclass(frozen=True, slots=True)
class ToolServices:
    """The existing services a tool may delegate to. Five, and nothing else.

    No session, no repository and no resolver: a tool reaches data only through a service that
    already applies its own scope check, which is why a tool needs no SQL of its own and could
    not write any.
    """

    analytics: AnalyticsService
    demand_prediction: DemandPredictionService
    forecast_performance: ForecastPerformanceService
    #: Stage 7.10. `search_hotel_knowledge` reads through `KnowledgeService.search` -- the same
    #: hotel- and status-filtered query the search route runs, not a second implementation.
    knowledge: KnowledgeService
    #: Stage 7.12. `get_hotel_priorities` reads the attention list through
    #: `InsightService.priorities` -- the same list `GET …/intelligence/priorities` serves.
    insight: InsightService


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Everything a tool is given besides its validated arguments.

    `hotel_public_id` is the hotel the authenticated request resolved. It is set by
    `ToolInvocationService` from its own `hotel_public_id` parameter, after the scope resolver
    has accepted it, and there is no code path by which a model's output reaches it.

    `evidence` is the request's ledger of citable excerpts. The copilot hands in one per question;
    anything else gets a fresh, empty one, so no ledger is ever shared between requests.
    """

    hotel_public_id: uuid.UUID
    services: ToolServices
    evidence: EvidenceLedger = field(default_factory=EvidenceLedger)


@dataclass(frozen=True, slots=True)
class ToolContract:
    """One tool's §7.3 definition. Inspectable without running anything."""

    name: str
    description: str
    min_role: HotelRole
    input_model: type[ToolArguments]
    output_model: type[ToolOutput]
    #: The existing service method this tool delegates to, as `Class.method`. Documentation that
    #: a test holds to the truth: the tool's source must call exactly this method.
    delegates_to: str
    side_effect: SideEffect = "none"
    #: Response fields the tool does not pass on, each with its reason.
    withheld: Mapping[str, str] = field(
        default_factory=lambda: {
            HOTEL_IDENTIFIER_FIELD: "the hotel is fixed by the request; the model never needs it"
        }
    )

    def input_schema(self) -> dict[str, Any]:
        """The JSON schema the model is shown. Deterministic for a given model class."""
        return self.input_model.model_json_schema()


#: A tool's implementation: validated arguments in, validated output out.
ToolRun = Callable[[ToolContext, Any], ToolOutput]


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """How one invocation ended, in a form both the loop and the audit trail can use.

    `tool` is the **registered** name when the call resolved to a tool, and `None` when it did
    not. The name a model asked for is never copied here: it is untrusted text, and an outcome
    that echoed it would carry model output into every place outcomes go.
    """

    tool: str | None
    outcome: Outcome
    output: Mapping[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome == "succeeded"

    def model_content(self) -> str:
        """What the model is shown as this call's result. JSON, key-sorted, deterministic.

        A failure is shown as an error object with its public code and message — the same
        strings an `ErrorResponse` would show a client, so nothing internal reaches the model.
        """
        if self.succeeded:
            return json.dumps(self.output, sort_keys=True, separators=(",", ":"))
        return json.dumps(
            {"error": {"code": self.error_code, "message": self.error_message}},
            sort_keys=True,
            separators=(",", ":"),
        )


__all__ = [
    "HOTEL_IDENTIFIER_FIELD",
    "INTERNAL_ERROR_CODE",
    "INVALID_ARGUMENTS_CODE",
    "UNKNOWN_TOOL_CODE",
    "DateRangeArguments",
    "Outcome",
    "SideEffect",
    "ToolArguments",
    "ToolContext",
    "ToolContract",
    "ToolOutcome",
    "ToolOutput",
    "ToolRun",
    "ToolServices",
]
