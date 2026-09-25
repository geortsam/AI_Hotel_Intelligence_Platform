"""Recorded exchanges: their file format, the model that replays them, the one that records them.

Stage 7.8. One format serves both directions, deliberately:

- **CI replays** `fixtures/reference_replays.json`, written by hand as the reference behaviour.
- **A live capture** (`scripts/copilot_live_eval.py`) records a real model's turns in the SAME
  format, so a captured run can later be committed and replayed in CI exactly like the reference.

`app.llm.testing.RecordedModel` cannot do this job: it maps a request's last message text to a
text answer, and so cannot replay a turn in which the model asks for tools. `ScriptedModel`
could replay the turns, but not the token counts a live recording captured -- hence the small
`ReplayModel` here.

**A replay is bound to what it was recorded against.** The file names the question set and the
prompt, each with its checksum; loading refuses a file recorded against different ones, because
replaying old answers to new questions would score nonsense and report it as a result. The prompt
is the one the copilot renders today (`app.services.copilot.PROMPT`): Stage 7.10 moved it to
`copilot_answer@v2`, and the Stage 7.8 reference exchanges were re-bound to it then -- their turns
unchanged, because none of them searches or cites a document.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.llm.base import ChatModel, ChatRequest, ChatResponse, TokenUsage, ToolCall
from app.services.copilot import PROMPT


class _Case(Protocol):
    @property
    def case_id(self) -> str: ...


class ReplayableSet(Protocol):
    """What a replay file is bound to: `copilot_eval_v1` or `copilot_knowledge_eval_v1`."""

    @property
    def identity(self) -> str: ...

    @property
    def checksum(self) -> str: ...

    @property
    def cases(self) -> Iterable[_Case]: ...


REPLAY_FORMAT = "copilot_eval_replay_v1"

#: The provider name the hand-written reference exchanges carry. Not a vendor: a label that the
#: report turns into "no language model was evaluated".
REFERENCE_PROVIDER = "reference"


class ReplayError(AssertionError):
    """The code under evaluation asked for a turn the recording does not contain."""


@dataclass(frozen=True, slots=True)
class Turn:
    """One model response: text, the tools it asked for, and what it reported costing."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "tool_calls": [
                {"id": call.id, "name": call.name, "arguments": dict(call.arguments)}
                for call in self.tool_calls
            ],
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Turn:
        return cls(
            text=str(raw.get("text", "")),
            tool_calls=tuple(
                ToolCall(id=str(c["id"]), name=str(c["name"]), arguments=dict(c["arguments"]))
                for c in raw.get("tool_calls", [])
            ),
            input_tokens=int(raw.get("input_tokens", 0)),
            output_tokens=int(raw.get("output_tokens", 0)),
        )


@dataclass(frozen=True, slots=True)
class Replays:
    """A recorded run: what it was recorded against, by what, when, and the turns per case."""

    provider: str
    model: str
    recorded_on: str
    question_set: str
    question_set_checksum: str
    prompt: str
    prompt_checksum: str
    cases: Mapping[str, tuple[Turn, ...]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": REPLAY_FORMAT,
            "provider": self.provider,
            "model": self.model,
            "recorded_on": self.recorded_on,
            "question_set": self.question_set,
            "question_set_checksum": self.question_set_checksum,
            "prompt": self.prompt,
            "prompt_checksum": self.prompt_checksum,
            "cases": {
                case_id: [turn.as_dict() for turn in turns]
                for case_id, turns in sorted(self.cases.items())
            },
        }


def load_replays(path: Path, question_set: ReplayableSet) -> Replays:
    """Read a replay file, refusing one recorded against another question set or prompt."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("format") != REPLAY_FORMAT:
        raise ValueError(f"{path.name}: not a {REPLAY_FORMAT} file")
    replays = Replays(
        provider=str(raw["provider"]),
        model=str(raw["model"]),
        recorded_on=str(raw["recorded_on"]),
        question_set=str(raw["question_set"]),
        question_set_checksum=str(raw["question_set_checksum"]),
        prompt=str(raw["prompt"]),
        prompt_checksum=str(raw["prompt_checksum"]),
        cases={
            case_id: tuple(Turn.from_dict(turn) for turn in turns)
            for case_id, turns in raw["cases"].items()
        },
    )
    if (replays.question_set, replays.question_set_checksum) != (
        question_set.identity,
        question_set.checksum,
    ):
        raise ValueError(f"{path.name} was recorded against another question set")
    if (replays.prompt, replays.prompt_checksum) != (PROMPT.identity, PROMPT.checksum):
        raise ValueError(f"{path.name} was recorded against another prompt")
    missing = {case.case_id for case in question_set.cases} - set(replays.cases)
    if missing:
        raise ValueError(f"{path.name} has no recording for {sorted(missing)}")
    return replays


def _response(request: ChatRequest, turn: Turn) -> ChatResponse:
    return ChatResponse(
        text=turn.text,
        parsed=None,
        usage=TokenUsage(input_tokens=turn.input_tokens, output_tokens=turn.output_tokens),
        latency_ms=0.0,
        provider="replay",
        model="replay",
        finish_reason="tool_use" if turn.tool_calls else "stop",
        prompt_id=request.prompt_id,
        prompt_version=request.prompt_version,
        tool_calls=turn.tool_calls,
    )


class ReplayModel:
    """Answers with the recorded turns, in order, and fails loudly past the end.

    Being asked for more turns than were recorded means the code under evaluation behaved
    differently from the code that was recorded -- a regression, reported as such.
    """

    def __init__(self, turns: Sequence[Turn]) -> None:
        self._turns = list(turns)
        self.requests: list[ChatRequest] = []

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if len(self.requests) > len(self._turns):
            raise ReplayError(
                f"asked for turn {len(self.requests)} of a {len(self._turns)}-turn recording"
            )
        return _response(request, self._turns[len(self.requests) - 1])


class RecordingModel:
    """Wraps any `ChatModel` and keeps every request and response. Changes nothing."""

    def __init__(self, inner: ChatModel) -> None:
        self._inner = inner
        self.requests: list[ChatRequest] = []
        self.responses: list[ChatResponse] = []

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        response = self._inner.complete(request)
        self.responses.append(response)
        return response

    def turns(self) -> tuple[Turn, ...]:
        """The recorded responses in replay form -- what a live capture writes to disk."""
        return tuple(
            Turn(
                text=response.text,
                tool_calls=response.tool_calls,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
            for response in self.responses
        )


__all__ = [
    "REFERENCE_PROVIDER",
    "REPLAY_FORMAT",
    "RecordingModel",
    "ReplayError",
    "ReplayModel",
    "ReplayableSet",
    "Replays",
    "Turn",
    "load_replays",
]
