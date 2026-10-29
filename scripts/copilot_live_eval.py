"""Run an evaluation set against the REAL configured language model, and record it (Stage 7.8).

This is the only way this repository evaluates an actual model, and it is never run by CI. It
calls the provider `LLM_PROVIDER` selects, and refuses to start without the flag that names what
that costs (R2.7O):

- `ollama` (the default) runs the LOCAL model on this machine -- `--confirm-local-model-call`;
- `anthropic` makes PAID API calls -- `--confirm-paid-api-call`, and `LLM_API_KEY` must be set.

Exactly one flag is accepted, and it must match the provider: a run never falls back from one
provider to the other.

    LLM_ENABLED=true LLM_PROVIDER=ollama python scripts/copilot_live_eval.py \\
        --out ../copilot-eval/local-1 --confirm-local-model-call --case kpis_may_occupancy

The key (anthropic only) is read from the environment (`LLM_API_KEY`) or the local `.env`, never
from the command line. `--set copilot_knowledge_eval_v1` (Stage 7.10) runs the document
questions instead of the default `copilot_eval_v1`; `--set copilot_eval_v2` (R2.7R) runs v1's
cases with four questions reworded to ask for every figure they are scored on -- prefer it
for a new live run; `--set copilot_eval_v3` (R2.8C) adds cases that test the model's own scope
compliance and reads v3's forecast fixture (`tests/evaluation/questions_v3.py`); `--case ID`
(repeatable) runs only those cases. Either way the recording names the set and the prompt the run
rendered.

## The prompt (R2.7ZQ)

`--prompt` names the registered prompt the run renders: `copilot_answer@v2` by default -- the one
the copilot serves -- or `copilot_answer@v3`, only when named. The record is resolved exactly from
the prompt registry, with no fallback, and passed through the harness's explicit prompt parameter;
the copilot's own `PROMPT` is never touched. A run under v3:

- writes `replays-<date>-copilot_answer-v3.json` and `report-<date>-copilot_answer-v3.json`, so
  its files can never be taken for, or collide with, a v2 run's;
- refuses an `--out` that already exists and is not empty -- each v3 pilot gets a directory of
  its own;
- adds a `v3_analysis` block to its report (`tests/evaluation/v3_pilot_analysis.py`): how each
  final answer's wording matches v3's fixed sentences, exactly, with outcome and tool use beside
  it. It is not a score, and no shared report schema changes.

*R2.8E.* `copilot_answer@v4` may also be named -- an evaluation-only choice, exactly like v3: its
files are named `...-copilot_answer-v4.json`, it needs a new empty `--out`, and it is resolved
from the registry with no fallback. Four things stay apart: the prompt production serves
(`app.services.copilot.PROMPT`, still v2), the prompts the registry holds, the prompts this script
may render (`PROMPTS`), and the one a given run rendered (recorded in both files). Naming v4 here
serves it nowhere and runs nothing by itself; a run still needs its confirmation flag, checked
before any client exists. A v4 report carries the `served`, `model_level` and `run_health` blocks
but no `v3_analysis`: that analysis is bound to v3's prompt and refuses any other, so it is added
for v3 runs only.

## Request bounds (R2.6B)

Per selected case: at most 4 logical model calls (3 tool rounds and a final answer), each at most
2 boundary attempts (one retry, on a timeout or an invalid structured answer only), each exactly
one HTTP request -- the SDK's own retries are switched off. So at most 8 HTTP requests per case.
A timed-out attempt is abandoned, not cancelled: its request may still complete and be billed,
and its tokens are then not in the report. Failed requests may be billed too. See
docs/copilot-evaluation.md section 7.

## What is refused, and when

Everything below is checked BEFORE a provider client exists, so a refusal costs nothing:

- `LLM_ENABLED` not true, or (anthropic) no `LLM_API_KEY`;
- `LLM_API_KEY` set but `LLM_PROVIDER` not set explicitly (R2.7V) -- before R2.7O that meant the
  paid provider, now the default is `ollama`; the settings refuse to load, with a fixed sentence;
- a confirmation flag that does not match the selected provider;
- an `OLLAMA_BASE_URL` that is not on this machine, or `LLM_BASE_URL` set beside `ollama`;
- `ANTHROPIC_CUSTOM_HEADERS` set in the environment (R2.7I) -- the SDK would add its headers to
  every request, so the settings refuse to load; the reason is printed as a fixed sentence;
- an unknown `--case` id;
- an `--out` inside this Git repository -- a capture must be reviewed before anything of it is
  committed, so it is written where `git add` cannot sweep it up;
- either output file already existing -- a previous run is never replaced; use a new `--out`;
- (`copilot_answer@v3`) an `--out` that exists and is not empty, or a prompt the registry does not
  hold exactly as named.

## What it writes

Two files in `--out`, both naming the exact cases selected and the same checksum for them:

- `replays-<date>.json` -- every model turn, in the format CI replays, plus a `live_provenance`
  block. Committing it (after review) turns this run into a reproducible fixture.
- `report-<date>.json` -- the scored report, with the same `live_provenance` block. For a
  `copilot_eval` set it also carries a `served` block (R2.7ZU): per case, the model's final text
  beside the answer the caller was served and the branch that produced it (`answer_source`), with
  served-answer measures kept apart from the model-output ones.

`live_provenance` records the configured model id, the model ids the provider RETURNED per turn
(`UNKNOWN` where a response named none -- never the configured name), the installed SDK version
(`UNKNOWN` if absent), the effective `LLM_THINKING` setting (R2.7F), the selection: case ids,
selected-set checksum, and the full set's checksum and size -- and (R2.7ZQ) the prompt's identity
and checksum and the request budget. The key is never printed, logged or written.

## Source, timing and run health (R2.8B)

- `live_provenance.source` -- the commit this repository's checkout was at (`git rev-parse HEAD`)
  and whether its working tree was `clean` or `dirty` (`git status --porcelain`, untracked files
  included). Read from local Git metadata only; nothing is fetched. `UNKNOWN` when Git cannot
  answer.
- `run_health.per_request` (report only) -- one row per model request, in the order made: the
  case, the request's number within it, its elapsed milliseconds and how it ended (`answered`, or
  the declared failure's code). A request is one call to the guarded model, so its time includes
  any retry the boundary made (it retries once when its own deadline expires) and the jitter
  before it; `attempts` is the boundary's count for an answered request and `null` for one that
  raised, because the boundary does not report attempts on a failure. Single HTTP attempts are
  not timed separately. A row whose request ended in `LLM_INVALID_RESPONSE` also carries
  `diagnostics` (R2.8M): the rejection's reason code, the provider, the provider's completion
  reason and output-token count when it reported them, how many tool calls were parsed, whether
  text or calls were present, and whether a structural rule rejected it -- never content. Other
  rows have no such key, and a report from before R2.8M has none at all.
- `run_health.status` -- `degraded` when `DEGRADED_AFTER_CONSECUTIVE_UNAVAILABLE` (2) or more
  consecutive requests, counted across case boundaries, raised `LLM_UNAVAILABLE` (the boundary's
  deadline expired twice, the adapter reported a timeout or refused connection, or the circuit
  was open); otherwise `normal`. Any other outcome ends a streak. Each such streak is listed with
  its first and last case. A degraded run still completes and is scored exactly as before: no
  case result changes; the status says the scores were taken while the provider was failing, and
  the run's summary prints it.

For a `copilot_eval` set the report also carries a `model_level` block (R2.8B,
`tests/evaluation/model_level.py`): the measures the model's final responses alone are held to.
A `copilot_eval_v3` run also carries an `integrity` block (R2.8L, `tests/evaluation/integrity.py`):
entity attribution, refusal reasons and each executed tool call's outcome -- the only set with the
explicit expectations that block needs -- and a `server_guard` block (R2.8N-G,
`tests/evaluation/expectations_v3_v2.py`, version `copilot_eval_v3_expectations_v2`): for each case
the server must refuse before any model call, this run's model side and served side apart, and
R2.8J's recorded model outcome beside them for `scope_bare_property_uuid`.

The files contain the evaluation questions and the model's answers about a FICTIONAL hotel -- no
production data -- and are not committed automatically.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "backend"))

QUESTION_SETS = (
    "copilot_eval_v1",
    "copilot_eval_v2",
    "copilot_eval_v3",
    "copilot_knowledge_eval_v1",
)

#: The prompts a run may render (R2.7ZQ): the default, and v3 or (R2.8E) v4 only when named.
#: Allowing one here makes it renderable in an evaluation run; it does not serve it anywhere.
DEFAULT_PROMPT = "copilot_answer@v2"
#: The one prompt `v3_pilot_analysis` is bound to; its block is added for that prompt only.
V3_ANALYSIS_PROMPT = "copilot_answer@v3"
PROMPTS = (DEFAULT_PROMPT, V3_ANALYSIS_PROMPT, "copilot_answer@v4")


class RunRefusedError(Exception):
    """A reason not to start. Raised before any provider client is built."""


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="A NEW directory outside this repository to write results to.",
    )
    consent = parser.add_mutually_exclusive_group(required=True)
    consent.add_argument(
        "--confirm-paid-api-call",
        action="store_true",
        help="With LLM_PROVIDER=anthropic: acknowledges paid API calls.",
    )
    consent.add_argument(
        "--confirm-local-model-call",
        action="store_true",
        help="With LLM_PROVIDER=ollama: acknowledges calls to the local model on this machine.",
    )
    parser.add_argument(
        "--set",
        dest="question_set",
        choices=list(QUESTION_SETS),
        default="copilot_eval_v1",
        help="Which frozen question set to run. Default: copilot_eval_v1.",
    )
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="Run only this case id (repeatable). Default: the whole set.",
    )
    parser.add_argument(
        "--prompt",
        choices=list(PROMPTS),
        default=DEFAULT_PROMPT,
        help=f"Which registered prompt to render. Default: {DEFAULT_PROMPT}.",
    )
    return parser.parse_args(argv)


def resolve_prompt(identity: str) -> Any:
    """The registered `PromptRecord` named exactly *identity* -- never another one instead."""
    from app.llm.prompts.registry import REGISTRY

    record = REGISTRY.get(identity) if identity in PROMPTS else None
    if record is None or record.identity != identity:
        raise RunRefusedError(f"prompt {identity!r} is not one this script may render")
    return record


def load_settings() -> Any:
    """`Settings` as the application reads it, or `None` when they refuse to load.

    `LLM_ENABLED=true` without a key is refused as the settings load; the caller reports both
    cases with the same sentence. The exception is not printed: a settings error must not become
    a way to echo configuration. Refusals with their own fixed sentence are reported by that
    sentence, because the usual one would mislead: `ANTHROPIC_CUSTOM_HEADERS` set in the
    environment (R2.7I), an off-machine `OLLAMA_BASE_URL` or `LLM_BASE_URL` beside `ollama`
    (R2.7O), and a key beside an `LLM_PROVIDER` that was not set explicitly (R2.7V).
    """
    from app.core.config import (
        IMPLICIT_PROVIDER_REFUSAL,
        INVALID_OLLAMA_URL_MESSAGE,
        LLM_BASE_URL_WITH_OLLAMA_REFUSAL,
        PROVIDER_HEADERS_REFUSAL,
        Settings,
    )

    try:
        return Settings()
    except ValueError as refused:
        for fixed in (
            PROVIDER_HEADERS_REFUSAL,
            IMPLICIT_PROVIDER_REFUSAL,
            INVALID_OLLAMA_URL_MESSAGE,
            LLM_BASE_URL_WITH_OLLAMA_REFUSAL,
        ):
            if fixed in str(refused):
                raise RunRefusedError(fixed) from None
        return None


def check_consent(settings: Any, options: argparse.Namespace) -> None:
    """Refuse unless the confirmation flag names the selected provider (R2.7O).

    Never a fallback: a flag for the other provider is a refusal, not a reason to switch.
    """
    if settings.llm_provider == "anthropic":
        if not options.confirm_paid_api_call:
            raise RunRefusedError(
                "LLM_PROVIDER=anthropic makes paid API calls: pass --confirm-paid-api-call."
            )
        if not settings.llm_api_key:
            raise RunRefusedError("LLM_ENABLED must be true and LLM_API_KEY set.")
    elif not options.confirm_local_model_call:
        raise RunRefusedError(
            "LLM_PROVIDER=ollama calls the local model: pass --confirm-local-model-call "
            "(--confirm-paid-api-call is for the anthropic provider)."
        )


def select_cases(full: Any, case_ids: Sequence[str]) -> Any:
    """The full set, or the subset naming exactly *case_ids*, in the full set's order."""
    if not case_ids:
        return full
    unknown = set(case_ids) - {case.case_id for case in full.cases}
    if unknown:
        raise RunRefusedError(f"unknown case ids: {sorted(unknown)}")
    return replace(full, cases=tuple(case for case in full.cases if case.case_id in case_ids))


def output_paths(out: Path, recorded_on: str, prompt: str = DEFAULT_PROMPT) -> tuple[Path, Path]:
    """The two files a run writes. A run under any prompt but the default names it: `@` becomes
    `-`, so `copilot_answer@v3` on 2026-10-27 writes `replays-2026-10-27-copilot_answer-v3.json`."""
    suffix = "" if prompt == DEFAULT_PROMPT else "-" + prompt.replace("@", "-")
    return out / f"replays-{recorded_on}{suffix}.json", out / f"report-{recorded_on}{suffix}.json"


def _comparable(path: Path) -> Path:
    """Resolved, and case-folded where the platform's paths are case-insensitive."""
    return Path(os.path.normcase(path.resolve()))


def inside_repository(path: Path, root: Path = REPO_ROOT) -> bool:
    candidate, repository = _comparable(path), _comparable(root)
    return candidate == repository or candidate.is_relative_to(repository)


def check_output(
    out: Path, paths: Iterable[Path], root: Path = REPO_ROOT, *, isolated: bool = False
) -> None:
    """Refuse an output location inside the repository, or one that already holds a result --
    and, when *isolated* (a non-default prompt, R2.7ZQ), one that holds anything at all."""
    if inside_repository(out, root):
        raise RunRefusedError(
            f"--out {out} is inside the repository ({root}). Write captures outside it, e.g. "
            "../copilot-eval/<run-name>, and review them before committing anything."
        )
    if isolated and out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise RunRefusedError(
            f"--out {out} already exists and is not empty. A run under a non-default prompt "
            "writes to a new, empty directory of its own."
        )
    existing = [path for path in paths if path.exists()]
    if existing:
        raise RunRefusedError(
            "refusing to overwrite an existing evaluation output: "
            + ", ".join(str(path) for path in existing)
            + ". Use a new --out directory; previous results are never replaced."
        )


#: What `sdk_identity` records for a provider reached without an SDK (R2.7O).
NO_SDK = "none"


def sdk_identity(provider: str = "anthropic") -> tuple[str, str]:
    """The provider SDK's distribution name and installed version (`UNKNOWN` if absent).

    The local `ollama` provider uses no SDK -- the standard library's `urllib` -- so it records
    `none` for both, and the Anthropic adapter is not imported to find that out.
    """
    if provider != "anthropic":
        return NO_SDK, NO_SDK
    from app.llm.providers.anthropic_provider import SDK_DISTRIBUTION, installed_sdk_version
    from tests.evaluation.replay import UNKNOWN

    return SDK_DISTRIBUTION, installed_sdk_version() or UNKNOWN


def ollama_options(settings: Any) -> dict[str, Any] | None:
    """The effective local-model options, recorded with the run; None for another provider."""
    if settings.llm_provider != "ollama":
        return None
    from app.llm.providers.ollama_provider import PRESENCE_PENALTY

    return {
        "base_url": settings.ollama_base_url,
        "num_ctx": settings.ollama_num_ctx,
        "num_predict": settings.llm_max_output_tokens,
        "temperature": settings.ollama_temperature,
        "seed": settings.ollama_seed,
        "presence_penalty": PRESENCE_PENALTY,
        "think": False if settings.llm_thinking == "disabled" else "provider_default",
    }


#: What `source_identity` records when Git cannot say (R2.8B). The replay module's word for it.
UNKNOWN_SOURCE = "UNKNOWN"

GitRunner = Callable[..., "subprocess.CompletedProcess[str]"]


def source_identity(root: Path = REPO_ROOT, run: GitRunner = subprocess.run) -> dict[str, str]:
    """The commit *root* is checked out at, and whether its working tree is clean (R2.8B).

    Two local Git commands, never a fetch. Either one failing -- Git missing, not a repository --
    records `UNKNOWN` for what it would have said rather than stopping the run.
    """

    def git(*args: str) -> str | None:
        try:
            done = run(
                ["git", "-C", str(root), *args],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout

    head = git("rev-parse", "HEAD")
    status = git("status", "--porcelain")
    return {
        "git_commit": head.strip() if head and head.strip() else UNKNOWN_SOURCE,
        "git_tree": UNKNOWN_SOURCE if status is None else ("dirty" if status.strip() else "clean"),
    }


#: R2.8B. How many consecutive model requests must raise `LLM_UNAVAILABLE`, as the boundary
#: raised it, before a run is reported `degraded`. Two is the smallest number that is a pattern
#: rather than one bad request; the R2.7 pilot's first two requests met it.
DEGRADED_AFTER_CONSECUTIVE_UNAVAILABLE = 2
#: `LlmUnavailableError.code`, which an open circuit shares. A test pins the two together.
UNAVAILABLE_CODE = "LLM_UNAVAILABLE"
RUN_HEALTH_VERSION = "copilot_run_health_v1"
ANSWERED = "answered"


class TimedModel:
    """Times every request to *inner* and appends one row per request to *log* (R2.8B).

    Changes nothing: the response is returned and a declared failure re-raised exactly as they
    came. A request is one `complete` call on the guarded model, so its time includes any retry
    the boundary made, and the jitter before it.
    """

    def __init__(
        self,
        inner: Any,
        log: list[dict[str, Any]],
        case_id: str,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._inner = inner
        self._log = log
        self._case_id = case_id
        self._monotonic = monotonic
        self._count = 0

    def complete(self, request: Any) -> Any:
        from app.llm.errors import LlmError, LlmInvalidResponseError

        self._count += 1
        started = self._monotonic()
        try:
            response = self._inner.complete(request)
        except LlmError as failed:
            # R2.8M: a rejected answer's content-free diagnostics, beside its code.
            diagnostics = (
                failed.diagnostics.as_dict()
                if isinstance(failed, LlmInvalidResponseError)
                else None
            )
            self._record(started, failed.code, None, diagnostics)
            raise
        self._record(started, ANSWERED, response.attempts)
        return response

    def _record(
        self,
        started: float,
        outcome: str,
        attempts: int | None,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        row: dict[str, Any] = {
            "case_id": self._case_id,
            "request": self._count,
            "elapsed_ms": max(0, round((self._monotonic() - started) * 1000)),
            "outcome": outcome,
            "attempts": attempts,
        }
        if diagnostics is not None:
            row["diagnostics"] = diagnostics
        self._log.append(row)


def run_health(
    requests: Sequence[Mapping[str, Any]],
    threshold: int = DEGRADED_AFTER_CONSECUTIVE_UNAVAILABLE,
) -> dict[str, Any]:
    """The report's `run_health` block: `normal` or `degraded`, and why. See the module."""
    streaks: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    for row in requests:
        if row["outcome"] == UNAVAILABLE_CODE:
            current.append(row)
            continue
        if current:
            streaks.append(current)
        current = []
    if current:
        streaks.append(current)
    degraded = [streak for streak in streaks if len(streak) >= threshold]
    return {
        "version": RUN_HEALTH_VERSION,
        "status": "degraded" if degraded else "normal",
        "rule": (
            f"degraded when {threshold} or more consecutive model requests, across cases, raise "
            f"{UNAVAILABLE_CODE}; case scores are not changed"
        ),
        "threshold": threshold,
        "requests": len(requests),
        "unavailable_requests": sum(len(streak) for streak in streaks),
        "longest_unavailable_streak": max((len(streak) for streak in streaks), default=0),
        "degraded_streaks": [
            {
                "first_case": streak[0]["case_id"],
                "last_case": streak[-1]["case_id"],
                "requests": len(streak),
            }
            for streak in degraded
        ],
        "per_request": [dict(row) for row in requests],
    }


def live_provenance(
    *,
    full: Any,
    selected: Any,
    provider: str,
    configured_model: str,
    thinking: str,
    returned_models: dict[str, tuple[str, ...]],
    sdk: tuple[str, str],
    prompt: Any,
    budget: Any,
    model_options: dict[str, Any] | None = None,
    source: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The block both output files carry. Built from public values only; never the settings."""
    distinct = sorted({model for models in returned_models.values() for model in models})
    return {
        "source": dict(source) if source is not None else None,
        "provider": provider,
        "configured_model": configured_model,
        "prompt": {"identity": prompt.identity, "checksum": prompt.checksum},
        "budget": {
            "timeout_seconds": budget.timeout_seconds,
            "max_output_tokens": budget.max_output_tokens,
        },
        "thinking": thinking,
        "returned_models": distinct,
        "returned_models_by_case": {case: list(models) for case, models in returned_models.items()},
        "sdk_distribution": sdk[0],
        "sdk_version": sdk[1],
        "ollama_options": model_options,
        "selection": {
            "subset": len(selected.cases) != len(full.cases),
            "case_ids": [case.case_id for case in selected.cases],
            "selected_case_count": len(selected.cases),
            "selected_set_checksum": selected.checksum,
            "full_set_identity": full.identity,
            "full_set_checksum": full.checksum,
            "full_set_case_count": len(full.cases),
        },
    }


def write_new(path: Path, text: str) -> None:
    """Create *path*; fail rather than replace it if it appeared since `check_output` ran."""
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)


def main(
    argv: list[str] | None = None,
    *,
    today: Callable[[], dt.date] = dt.date.today,
    monotonic: Callable[[], float] = time.perf_counter,
) -> int:
    options = parse_arguments(argv)

    from app.llm.base import Budget, ChatModel
    from tests.evaluation import harness, knowledge_harness, v3_pilot_analysis
    from tests.evaluation.knowledge_questions import COPILOT_KNOWLEDGE_EVAL_V1
    from tests.evaluation.questions import COPILOT_EVAL_V1
    from tests.evaluation.questions_v2 import COPILOT_EVAL_V2
    from tests.evaluation.questions_v3 import COPILOT_EVAL_V3
    from tests.evaluation.replay import RecordingModel, Replays

    try:
        settings = load_settings()
    except RunRefusedError as refused:
        print(str(refused), file=sys.stderr)
        return 2
    if settings is None or not settings.llm_enabled:
        print("LLM_ENABLED must be true and LLM_API_KEY set.", file=sys.stderr)
        return 2
    try:
        check_consent(settings, options)
    except RunRefusedError as refused:
        print(str(refused), file=sys.stderr)
        return 2

    full: Any = {
        "copilot_knowledge_eval_v1": COPILOT_KNOWLEDGE_EVAL_V1,
        "copilot_eval_v2": COPILOT_EVAL_V2,
        "copilot_eval_v3": COPILOT_EVAL_V3,
    }.get(options.question_set, COPILOT_EVAL_V1)
    runner: Any = knowledge_harness.run if full is COPILOT_KNOWLEDGE_EVAL_V1 else harness.run
    # R2.7ZU: a copilot_eval run also reports each case as the caller saw it, and (R2.8B) the
    # model's final responses alone. The knowledge harness records the served answer in its own
    # case results already.
    blocks: dict[str, Any] = (
        {} if full is COPILOT_KNOWLEDGE_EVAL_V1 else {"served": True, "model_level": True}
    )
    if full is COPILOT_EVAL_V3:
        blocks["integrity"] = True  # R2.8L: only v3 has the expectations it needs
        blocks["server_guard"] = True  # R2.8N-G: copilot_eval_v3_expectations_v2, v3 only
    recorded_on = today().isoformat()
    replay_path, report_path = output_paths(options.out, recorded_on, options.prompt)
    try:
        prompt = resolve_prompt(options.prompt)
        selected = select_cases(full, options.case)
        check_output(
            options.out, (replay_path, report_path), isolated=options.prompt != DEFAULT_PROMPT
        )
    except RunRefusedError as refused:
        print(str(refused), file=sys.stderr)
        return 2
    budget = Budget(
        timeout_seconds=settings.llm_timeout_seconds,
        max_output_tokens=settings.llm_max_output_tokens,
    )

    # Only now does a provider client come into existence.
    from app.llm.factory import build_chat_model

    model = build_chat_model(settings)
    recordings: dict[str, RecordingModel] = {}
    requests: list[dict[str, Any]] = []

    def model_for(case: Any) -> ChatModel:
        recorder = RecordingModel(TimedModel(model, requests, case.case_id, monotonic))
        recordings[case.case_id] = recorder
        print(f"  {case.case_id} ...", flush=True)
        return recorder

    report = runner(
        selected,
        model_for,
        mode="live",
        provider=settings.llm_provider,
        model=settings.effective_llm_model,
        recorded_on=recorded_on,
        # The prompt `--prompt` named, passed explicitly (R2.7ZN, R2.7ZQ): the run renders the
        # same record the capture below names.
        prompt=prompt,
        budget=budget,
        **blocks,
    )

    provenance = live_provenance(
        full=full,
        selected=selected,
        provider=settings.llm_provider,
        configured_model=settings.effective_llm_model,
        thinking=settings.llm_thinking,
        returned_models={case: rec.returned_models() for case, rec in recordings.items()},
        sdk=sdk_identity(settings.llm_provider),
        prompt=prompt,
        budget=budget,
        model_options=ollama_options(settings),
        source=source_identity(),
    )
    report["live_provenance"] = provenance
    report["run_health"] = run_health(requests)
    # Only for v3 (R2.8E): the analysis refuses any other prompt, and under v4 that refusal would
    # come after every model call and before either file was written.
    if options.prompt == V3_ANALYSIS_PROMPT:
        report["v3_analysis"] = v3_pilot_analysis.analyse(
            prompt,
            report["cases"],
            final_texts={
                case_id: recorder.responses[-1].text if recorder.responses else ""
                for case_id, recorder in recordings.items()
            },
            model_calls={case_id: len(rec.responses) for case_id, rec in recordings.items()},
            served=report.get("served"),
        )
    replays = Replays(
        provider=settings.llm_provider,
        model=settings.effective_llm_model,
        recorded_on=recorded_on,
        question_set=selected.identity,
        question_set_checksum=selected.checksum,
        prompt=prompt.identity,
        prompt_checksum=prompt.checksum,
        cases={case_id: recorder.turns() for case_id, recorder in recordings.items()},
        live_provenance=provenance,
    )

    options.out.mkdir(parents=True, exist_ok=True)
    write_new(replay_path, json.dumps(replays.as_dict(), indent=2, ensure_ascii=False) + "\n")
    write_new(report_path, harness.canonical(report))

    print(report["statement"])
    for measure, counts in report["measures"].items():
        print(f"  {measure}: {counts['passed']} of {counts['scored']}")
    health = report["run_health"]
    print(
        f"  run health: {health['status']} ({health['unavailable_requests']} of "
        f"{health['requests']} model requests unavailable, longest streak "
        f"{health['longest_unavailable_streak']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
