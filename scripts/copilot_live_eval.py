"""Run an evaluation set against the REAL configured language model, and record it (Stage 7.8).

This is the only way this repository evaluates an actual model, and it is never run by CI. It
makes paid API calls, so it refuses to start without `--confirm-paid-api-call`.

    LLM_ENABLED=true LLM_MODEL=<model id> \\
        python scripts/copilot_live_eval.py --out ../copilot-eval/run-1 --confirm-paid-api-call

The key is read from the environment (`LLM_API_KEY`) or the local `.env`, never from the command
line. `--set copilot_knowledge_eval_v1` (Stage 7.10) runs the document questions instead of the
default `copilot_eval_v1`; `--case ID` (repeatable) runs only those cases. Either way the
recording names the prompt the copilot actually renders -- `app.services.copilot.PROMPT`.

## Request bounds (R2.6B)

Per selected case: at most 4 logical model calls (3 tool rounds and a final answer), each at most
2 boundary attempts (one retry, on a timeout or an invalid structured answer only), each exactly
one HTTP request -- the SDK's own retries are switched off. So at most 8 HTTP requests per case.
A timed-out attempt is abandoned, not cancelled: its request may still complete and be billed,
and its tokens are then not in the report. Failed requests may be billed too. See
docs/copilot-evaluation.md section 7.

## What is refused, and when

Everything below is checked BEFORE a provider client exists, so a refusal costs nothing:

- `LLM_ENABLED` not true, or no `LLM_API_KEY`;
- `ANTHROPIC_CUSTOM_HEADERS` set in the environment (R2.7I) -- the SDK would add its headers to
  every request, so the settings refuse to load; the reason is printed as a fixed sentence;
- an unknown `--case` id;
- an `--out` inside this Git repository -- a capture must be reviewed before anything of it is
  committed, so it is written where `git add` cannot sweep it up;
- either output file already existing -- a previous run is never replaced; use a new `--out`.

## What it writes

Two files in `--out`, both naming the exact cases selected and the same checksum for them:

- `replays-<date>.json` -- every model turn, in the format CI replays, plus a `live_provenance`
  block. Committing it (after review) turns this run into a reproducible fixture.
- `report-<date>.json` -- the scored report, with the same `live_provenance` block.

`live_provenance` records the configured model id, the model ids the provider RETURNED per turn
(`UNKNOWN` where a response named none -- never the configured name), the installed SDK version
(`UNKNOWN` if absent), the effective `LLM_THINKING` setting (R2.7F), and the selection: case ids,
selected-set checksum, and the full set's checksum and size. The key is never printed, logged or
written.

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

QUESTION_SETS = ("copilot_eval_v1", "copilot_knowledge_eval_v1")


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
    parser.add_argument(
        "--confirm-paid-api-call",
        required=True,
        action="store_true",
        help="Required. Acknowledges that this calls the configured provider and costs money.",
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
    return parser.parse_args(argv)


def load_settings() -> Any:
    """`Settings` as the application reads it, or `None` when they refuse to load.

    `LLM_ENABLED=true` without a key is refused as the settings load; the caller reports both
    cases with the same sentence. The exception is not printed: a settings error must not become
    a way to echo configuration. One refusal has its own fixed sentence, because the usual one
    would mislead: `ANTHROPIC_CUSTOM_HEADERS` set in the environment (R2.7I).
    """
    from app.core.config import PROVIDER_HEADERS_REFUSAL, Settings

    try:
        return Settings()
    except ValueError as refused:
        if PROVIDER_HEADERS_REFUSAL in str(refused):
            raise RunRefusedError(PROVIDER_HEADERS_REFUSAL) from None
        return None


def select_cases(full: Any, case_ids: Sequence[str]) -> Any:
    """The full set, or the subset naming exactly *case_ids*, in the full set's order."""
    if not case_ids:
        return full
    unknown = set(case_ids) - {case.case_id for case in full.cases}
    if unknown:
        raise RunRefusedError(f"unknown case ids: {sorted(unknown)}")
    return replace(full, cases=tuple(case for case in full.cases if case.case_id in case_ids))


def output_paths(out: Path, recorded_on: str) -> tuple[Path, Path]:
    return out / f"replays-{recorded_on}.json", out / f"report-{recorded_on}.json"


def _comparable(path: Path) -> Path:
    """Resolved, and case-folded where the platform's paths are case-insensitive."""
    return Path(os.path.normcase(path.resolve()))


def inside_repository(path: Path, root: Path = REPO_ROOT) -> bool:
    candidate, repository = _comparable(path), _comparable(root)
    return candidate == repository or candidate.is_relative_to(repository)


def check_output(out: Path, paths: Iterable[Path], root: Path = REPO_ROOT) -> None:
    """Refuse an output location inside the repository, or one that already holds a result."""
    if inside_repository(out, root):
        raise RunRefusedError(
            f"--out {out} is inside the repository ({root}). Write captures outside it, e.g. "
            "../copilot-eval/<run-name>, and review them before committing anything."
        )
    existing = [path for path in paths if path.exists()]
    if existing:
        raise RunRefusedError(
            "refusing to overwrite an existing evaluation output: "
            + ", ".join(str(path) for path in existing)
            + ". Use a new --out directory; previous results are never replaced."
        )


def sdk_identity() -> tuple[str, str]:
    """The provider SDK's distribution name and installed version (`UNKNOWN` if absent)."""
    from app.llm.providers.anthropic_provider import SDK_DISTRIBUTION, installed_sdk_version
    from tests.evaluation.replay import UNKNOWN

    return SDK_DISTRIBUTION, installed_sdk_version() or UNKNOWN


def live_provenance(
    *,
    full: Any,
    selected: Any,
    provider: str,
    configured_model: str,
    thinking: str,
    returned_models: dict[str, tuple[str, ...]],
    sdk: tuple[str, str],
) -> dict[str, Any]:
    """The block both output files carry. Built from public values only; never the settings."""
    distinct = sorted({model for models in returned_models.values() for model in models})
    return {
        "provider": provider,
        "configured_model": configured_model,
        "thinking": thinking,
        "returned_models": distinct,
        "returned_models_by_case": {case: list(models) for case, models in returned_models.items()},
        "sdk_distribution": sdk[0],
        "sdk_version": sdk[1],
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
    from app.services.copilot import PROMPT
    from tests.evaluation import harness, knowledge_harness
    from tests.evaluation.knowledge_questions import COPILOT_KNOWLEDGE_EVAL_V1
    from tests.evaluation.questions import COPILOT_EVAL_V1
    from tests.evaluation.replay import RecordingModel, Replays

    try:
        settings = load_settings()
    except RunRefusedError as refused:
        print(str(refused), file=sys.stderr)
        return 2
    if settings is None or not settings.llm_enabled or not settings.llm_api_key:
        print("LLM_ENABLED must be true and LLM_API_KEY set.", file=sys.stderr)
        return 2

    full: Any = (
        COPILOT_KNOWLEDGE_EVAL_V1
        if options.question_set == "copilot_knowledge_eval_v1"
        else COPILOT_EVAL_V1
    )
    runner: Any = knowledge_harness.run if full is COPILOT_KNOWLEDGE_EVAL_V1 else harness.run
    recorded_on = today().isoformat()
    replay_path, report_path = output_paths(options.out, recorded_on)
    try:
        selected = select_cases(full, options.case)
        check_output(options.out, (replay_path, report_path))
    except RunRefusedError as refused:
        print(str(refused), file=sys.stderr)
        return 2

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
        model=settings.llm_model,
        recorded_on=recorded_on,
        budget=Budget(
            timeout_seconds=settings.llm_timeout_seconds,
            max_output_tokens=settings.llm_max_output_tokens,
        ),
    )

    provenance = live_provenance(
        full=full,
        selected=selected,
        provider=settings.llm_provider,
        configured_model=settings.llm_model,
        thinking=settings.llm_thinking,
        returned_models={case: rec.returned_models() for case, rec in recordings.items()},
        sdk=sdk_identity(),
    )
    report["live_provenance"] = provenance
    replays = Replays(
        provider=settings.llm_provider,
        model=settings.llm_model,
        recorded_on=recorded_on,
        question_set=selected.identity,
        question_set_checksum=selected.checksum,
        prompt=PROMPT.identity,
        prompt_checksum=PROMPT.checksum,
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
