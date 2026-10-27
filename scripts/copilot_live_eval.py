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
for a new live run; `--case ID` (repeatable) runs only those cases. Either way the recording
names the set and the prompt the run rendered.

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

The files contain the evaluation questions and the model's answers about a FICTIONAL hotel -- no
production data -- and are not committed automatically.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "backend"))

QUESTION_SETS = ("copilot_eval_v1", "copilot_eval_v2", "copilot_knowledge_eval_v1")

#: The prompts a run may render (R2.7ZQ): the default, and v3 only when named.
DEFAULT_PROMPT = "copilot_answer@v2"
PROMPTS = (DEFAULT_PROMPT, "copilot_answer@v3")


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
) -> dict[str, Any]:
    """The block both output files carry. Built from public values only; never the settings."""
    distinct = sorted({model for models in returned_models.values() for model in models})
    return {
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
) -> int:
    options = parse_arguments(argv)

    from app.llm.base import Budget, ChatModel
    from tests.evaluation import harness, knowledge_harness, v3_pilot_analysis
    from tests.evaluation.knowledge_questions import COPILOT_KNOWLEDGE_EVAL_V1
    from tests.evaluation.questions import COPILOT_EVAL_V1
    from tests.evaluation.questions_v2 import COPILOT_EVAL_V2
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
    }.get(options.question_set, COPILOT_EVAL_V1)
    runner: Any = knowledge_harness.run if full is COPILOT_KNOWLEDGE_EVAL_V1 else harness.run
    # R2.7ZU: a copilot_eval run also reports each case as the caller saw it. The knowledge
    # harness records the served answer in its own case results already.
    served: dict[str, Any] = {} if full is COPILOT_KNOWLEDGE_EVAL_V1 else {"served": True}
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

    def model_for(case: Any) -> ChatModel:
        recorder = RecordingModel(model)
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
        **served,
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
    )
    report["live_provenance"] = provenance
    if options.prompt != DEFAULT_PROMPT:
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
