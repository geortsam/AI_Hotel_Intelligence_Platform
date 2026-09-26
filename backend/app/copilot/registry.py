"""The allow-list. A name maps to a contract and a function, and nothing else is reachable.

Stage 7.6. The model names a tool; this module decides whether that name means anything.

    model output: ToolCall(name="get_hotel_kpis", arguments={...})
                                   |
                    ToolRegistry.get(name)       <- exact dict lookup; unknown -> UnknownToolError
                                   |
                    RegisteredTool(contract, run)  <- a function object imported statically below

## What makes this safe to hand untrusted names

- **The lookup is a dictionary key.** No `getattr`, no `importlib`, no `eval`, no string
  formatting into a module path. A name that is not a key reaches nothing, and a test asserts
  the module contains none of those constructs.
- **The table is built by explicit, static imports** in `build_default_registry`. What can run
  is exactly what those seven lines import, which a reviewer can read and a test can list.
- **Registration is checked, and fails loudly.** A duplicate name, a malformed name, an input
  model that tolerates unknown keys, or a schema that names a hotel, tenant or property anywhere
  — in either direction — is refused at registration, which happens at import. A registry that
  exists is a registry that passed.

## The tenant rule, structurally

No input schema may contain a property whose name mentions a hotel, tenant, property,
organisation, user or account, or ends in an identifier suffix. The check walks the whole JSON
schema, nested definitions included, so a hotel id cannot be smuggled in through a sub-object.
That is the brief's "structural security requirement, not merely a prompt instruction": the
model cannot supply a hotel because there is no field anywhere it could supply one in.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from app.copilot.contracts import ToolContract, ToolRun

#: A tool name: lower snake case, a verb first, short enough to be an audit reference (the
#: column holds 64 characters). The same shape every provider's tool-name rule accepts.
TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{2,63}$")

#: Fragments no input property name may contain. Matched case-insensitively against every
#: property at every depth of the input schema.
FORBIDDEN_INPUT_FRAGMENTS = (
    "hotel",
    "tenant",
    "property",
    "organisation",
    "organization",
    "user",
    "actor",
    "account",
    "member",
    "uuid",
)

#: Fragments no output property name may contain: the tool is working for one hotel and says
#: nothing about which, and returns no internal or public row identifier.
FORBIDDEN_OUTPUT_FRAGMENTS = ("hotel", "tenant", "uuid", "public_id")


class UnknownToolError(LookupError):
    """A name the registry does not hold. Carries no copy of the name: it is untrusted text."""

    def __init__(self) -> None:
        super().__init__("No tool is registered under the requested name.")


class ToolRegistrationError(ValueError):
    """A tool that fails the registry's structural rules. Raised at import, never at runtime."""


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    contract: ToolContract
    run: ToolRun


def property_names(schema: Any) -> Iterator[str]:
    """Every property name at every depth of a JSON schema, `$defs` included."""
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            yield from properties
        for value in schema.values():
            yield from property_names(value)
    elif isinstance(schema, list):
        for item in schema:
            yield from property_names(item)


def _names_an_identifier(name: str, fragments: tuple[str, ...]) -> bool:
    lowered = name.lower()
    return (
        lowered == "id"
        or lowered.endswith("_id")
        or lowered.endswith("_ids")
        or any(fragment in lowered for fragment in fragments)
    )


class ToolRegistry:
    """A closed table of tools. Built once, read many times, never mutated by model output."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, contract: ToolContract, run: ToolRun) -> None:
        """Add one tool, or refuse it. Every refusal is a programming error, raised loudly."""
        name = contract.name
        if not TOOL_NAME.fullmatch(name):
            raise ToolRegistrationError(f"Tool name {name!r} is not lower snake case.")
        if name in self._tools:
            raise ToolRegistrationError(f"Tool {name!r} is already registered.")
        if not contract.description.strip():
            raise ToolRegistrationError(f"Tool {name!r} has no description.")
        if contract.input_model.model_config.get("extra") != "forbid":
            raise ToolRegistrationError(f"Tool {name!r} input must forbid unknown keys.")
        if contract.output_model.model_config.get("extra") != "forbid":
            raise ToolRegistrationError(f"Tool {name!r} output must forbid unknown keys.")

        for field_name in property_names(contract.input_model.model_json_schema()):
            if _names_an_identifier(field_name, FORBIDDEN_INPUT_FRAGMENTS):
                raise ToolRegistrationError(
                    f"Tool {name!r} input names {field_name!r}. No tool may take a tenant, a "
                    "principal or a row identifier from the model."
                )
        for field_name in property_names(contract.output_model.model_json_schema()):
            if _names_an_identifier(field_name, FORBIDDEN_OUTPUT_FRAGMENTS):
                raise ToolRegistrationError(
                    f"Tool {name!r} output names {field_name!r}. A tool returns no hotel and "
                    "no row identifier."
                )

        self._tools[name] = RegisteredTool(contract=contract, run=run)

    def get(self, name: str) -> RegisteredTool:
        """The tool registered under exactly *name*, or `UnknownToolError`. Fails closed."""
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            raise UnknownToolError()
        return tool

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools

    def names(self) -> tuple[str, ...]:
        """Every registered name, sorted. The deterministic order everything else uses."""
        return tuple(sorted(self._tools))

    def contracts(self) -> tuple[ToolContract, ...]:
        return tuple(self._tools[name].contract for name in self.names())


def build_default_registry() -> ToolRegistry:
    """The seven tools, registered explicitly. Nothing is discovered.

    The five Stage 7.6 tools; `search_hotel_knowledge` (Stage 7.10), §7.2's sixth, deferred by
    Amendment A1 until Stage 7.9 built the `KnowledgeService` it delegates to; and
    `get_hotel_priorities` (Stage 7.12), the attention list. Every tool here is executable; none
    is a placeholder.
    """
    from app.copilot.tools import (
        daily_series,
        demand_forecast,
        forecast_accuracy,
        hotel_kpis,
        knowledge_search,
        priorities,
        revenue_breakdown,
    )

    registry = ToolRegistry()
    registry.register(hotel_kpis.CONTRACT, hotel_kpis.run)
    registry.register(daily_series.CONTRACT, daily_series.run)
    registry.register(revenue_breakdown.CONTRACT, revenue_breakdown.run)
    registry.register(demand_forecast.CONTRACT, demand_forecast.run)
    registry.register(forecast_accuracy.CONTRACT, forecast_accuracy.run)
    registry.register(knowledge_search.CONTRACT, knowledge_search.run)
    registry.register(priorities.CONTRACT, priorities.run)
    return registry


__all__ = [
    "FORBIDDEN_INPUT_FRAGMENTS",
    "FORBIDDEN_OUTPUT_FRAGMENTS",
    "TOOL_NAME",
    "RegisteredTool",
    "ToolRegistrationError",
    "ToolRegistry",
    "UnknownToolError",
    "build_default_registry",
    "property_names",
]
