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

*R2.9L.* `copilot_answer@v5` (R2.9K: v4 with one sentence naming the evaluated hotel) may be named
too, on exactly v4's terms: files named `...-copilot_answer-v5.json`, a new empty `--out`, the
record resolved from the registry with no fallback, the confirmation flag still required, and no
`v3_analysis`. The copilot fills the hotel's name in itself; nothing here supplies it.

*R2.9O.* `copilot_answer@v6` (v4's system turn, with a user-turn preamble naming the evaluated hotel
and placing any other hotel or property out of scope) may be named on the same terms as v5.

*R2.9R.* `copilot_answer@v7` (v6, with one sentence after its preamble: a question about which
guests or who identifies guests, one about how many does not) may be named on the same terms too.

*R2.9U.* `copilot_answer@v8` (v7, with one sentence more: a question about what guests wrote in
their reviews is answered only from review text in the hotel's documents) likewise.

*R2.9X.* `--case-policy` names the case-policy version a `copilot_eval_v3` run's
`model_level_v2` and `integrity_v3` blocks are scored under: `copilot_eval_v3_case_policy_v1`
by default, or `copilot_eval_v3_case_policy_v2` (`review_text` tolerates a search rather than
requiring one). Both blocks name the version used. Any other set has no case policy, so naming
v2 for one is refused before any client exists.

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

*R2.9B.* A `copilot_eval_v3` run -- under any prompt this script may render -- also carries the
R2.8AA scoring blocks, scored under `copilot_eval_v3_case_policy_v1`: `model_level_v2`
(`tests/evaluation/model_level_v2.py`, version `copilot_model_level_v2`: each case's outcome --
`not_asked`, `invalid_response`, `unavailable`, `tool_stopped`, `final_response` -- with the
error code a failed request raised, the case's `answer_policy`, the forecast policy's verdict for
`inject_estimate`, and the strict per-policy results) and `integrity_v3`
(`tests/evaluation/integrity_v3.py`, version `copilot_eval_integrity_v3`: the model's side, the
served text judged on its own, and the server's intervention and containment). They are added
beside every block above, which keep their meaning for comparison: the legacy blocks still score
`inject_estimate` as a decline and tolerate the KPI tool for `review_text`, the new ones apply
Decisions A and B. Under the unchanged 1,024-token output cap, `inject_estimate` may still end in
`invalid_response` (R2.8J, R2.8N-U, R2.8X), which `model_level_v2` reports as such. No other set
has a case policy, so no other set's report carries either block. Nothing here promotes a prompt:
the copilot still serves `copilot_answer@v2`. Each block also names, in `policies` (R2.9C), the
case policy, the forecast policy and -- for `integrity_v3` -- the `model_level_v2` assessment it
was scored under, by their canonical versions (docs/copilot-evaluation.md section 4); the replay
file, which holds turns only, carries none.

Every report also carries a `diagnostic_annotations` block (R2.8N-W,
`tests/evaluation/diagnostic_annotations.py`, version `copilot_eval_diagnostic_annotations_v1`):
a derived reading of `run_health`'s recorded diagnostics that changes no outcome or score. Its one
annotation marks a response cut off while writing tool calls that had already parsed more calls
than the tool loop accepts in one round.

## Repeated runs (R2.9H)

The same request has been answered differently at temperature 0 with a fixed seed (R2.9F), so one
run is a sample. `--repeat N` (1 to `MAX_REPEAT`, 10; default 1) runs the selection N times with
one client, and at most `8 * cases * N` HTTP requests -- each request still retried only as the
boundary already retries it.

- `--repeat 1` writes exactly the two files a run always wrote, named as before.
- `--repeat N` (N > 1) writes, per attempt K, a whole standard pair --
  `replays-<date>[-prompt]-attempt-K.json` and `report-<date>[-prompt]-attempt-K.json`, each
  loadable and scorable as any capture is, with its own `run_health` -- as soon as that attempt
  ends, so an attempt that cannot finish never takes an earlier attempt's files with it. Both
  carry `live_provenance.repetition`: `{"repeat": N, "attempt": K, "attempt_id": ...}`, the id
  `<date>-<prompt>-attempt-K-of-N` (`attempt_id`). Beside them, `repeats-<date>[-prompt].json`
  (`copilot_eval_repeats_v1`) lists every attempt's files and SHA-256, one row per attempt and
  case -- the model's outcome, its error code, the answer source, whether the server intervened,
  and the case's strict result from that attempt's `model_level_v2`, each in its own field -- and
  per case the counts across attempts. Counts only, never rates; nothing is merged into one score.

## Local model provenance (R2.9H)

An `ollama` run records `live_provenance.local_model`: the model name; the server's version, from
`GET /api/version` (a metadata request through the adapter's own loopback opener, which runs no
model); and the model's manifest digest, its twelve-character id (`ollama list`'s) and its weights'
layer digest and size, read from the local manifest under Ollama's default `~/.ollama/models`
-- read, never requested. A field that cannot be established is `{"status": "unavailable",
"reason": ...}`, never guessed. A run under any other provider records no such key, so its
provenance is exactly what it was.

The files contain the evaluation questions and the model's answers about a FICTIONAL hotel -- no
production data -- and are not committed automatically.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.request
from collections import Counter
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

#: The prompts a run may render (R2.7ZQ): the default, and v3, (R2.8E) v4, (R2.9L) v5, (R2.9O)
#: v6, (R2.9R) v7 or (R2.9U) v8 only when named. Allowing one here makes it renderable in an
#: evaluation run; it does not serve it anywhere.
DEFAULT_PROMPT = "copilot_answer@v2"
#: The one prompt `v3_pilot_analysis` is bound to; its block is added for that prompt only.
V3_ANALYSIS_PROMPT = "copilot_answer@v3"
PROMPTS = (
    DEFAULT_PROMPT,
    V3_ANALYSIS_PROMPT,
    "copilot_answer@v4",
    "copilot_answer@v5",
    "copilot_answer@v6",
    "copilot_answer@v7",
    "copilot_answer@v8",
)

#: R2.9X. The case-policy versions a `copilot_eval_v3` run may be scored under -- the same
#: identifiers `case_policy_v3.CASE_POLICY_VERSIONS` registers -- and the default, v1.
DEFAULT_CASE_POLICY = "copilot_eval_v3_case_policy_v1"
CASE_POLICIES = (DEFAULT_CASE_POLICY, "copilot_eval_v3_case_policy_v2")


class RunRefusedError(Exception):
    """A reason not to start. Raised before any provider client is built."""


#: R2.9H. The most attempts one run makes of each selected case. Each attempt is a full run of the
#: selection under the same request bounds, so a run makes at most `8 * cases * repeat` requests.
MAX_REPEAT = 10


def repeat_count(raw: str) -> int:
    """`--repeat`'s value: a whole number from 1 to `MAX_REPEAT`, or a clear refusal."""
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if not 1 <= value <= MAX_REPEAT or str(value) != raw.strip().lstrip("+"):
        raise argparse.ArgumentTypeError(
            f"--repeat must be a whole number from 1 to {MAX_REPEAT}, not {raw!r}"
        )
    return value


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
    parser.add_argument(
        "--repeat",
        type=repeat_count,
        default=1,
        help=(
            f"Run the selection this many times, 1 to {MAX_REPEAT} (R2.9H). Default: 1, which "
            "writes exactly the two files a run always wrote."
        ),
    )
    parser.add_argument(
        "--case-policy",
        choices=list(CASE_POLICIES),
        default=DEFAULT_CASE_POLICY,
        help=(
            "The case policy copilot_eval_v3's model_level_v2 and integrity_v3 blocks are scored "
            f"under (R2.9X). Default: {DEFAULT_CASE_POLICY}."
        ),
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


def _prompt_suffix(prompt: str) -> str:
    return "" if prompt == DEFAULT_PROMPT else "-" + prompt.replace("@", "-")


def output_paths(
    out: Path, recorded_on: str, prompt: str = DEFAULT_PROMPT, attempt: int | None = None
) -> tuple[Path, Path]:
    """The two files a run writes. A run under any prompt but the default names it: `@` becomes
    `-`, so `copilot_answer@v3` on 2026-10-27 writes `replays-2026-10-27-copilot_answer-v3.json`.
    One attempt of a repeated run (R2.9H) adds its number: `...-copilot_answer-v3-attempt-2.json`.
    """
    suffix = _prompt_suffix(prompt) + ("" if attempt is None else f"-attempt-{attempt}")
    return out / f"replays-{recorded_on}{suffix}.json", out / f"report-{recorded_on}{suffix}.json"


def repeats_path(out: Path, recorded_on: str, prompt: str = DEFAULT_PROMPT) -> Path:
    """R2.9H. The aggregate a repeated run writes beside its attempts' files."""
    return out / f"repeats-{recorded_on}{_prompt_suffix(prompt)}.json"


def attempt_id(recorded_on: str, prompt: str, attempt: int, repeat: int) -> str:
    """R2.9H. One attempt's identity: date, prompt, number and count -- the same for the same run,
    and never the same for two attempts of it: `2026-11-02-copilot_answer-v4-attempt-2-of-3`."""
    return f"{recorded_on}-{prompt.replace('@', '-')}-attempt-{attempt}-of-{repeat}"


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


#: R2.9H. What a provenance field records when it could not be established. Never a guess.
UNAVAILABLE = "unavailable"
#: Where a pulled model's manifest lives under Ollama's models directory, and its weights' layer.
OLLAMA_REGISTRY = "registry.ollama.ai"
OLLAMA_MODEL_LAYER = "application/vnd.ollama.image.model"
#: Ollama's metadata endpoint for its own version: a GET with no body, which runs no model.
OLLAMA_VERSION_PATH = "/api/version"
OLLAMA_VERSION_TIMEOUT_SECONDS = 5.0
#: Far more than `{"version": "..."}` needs; anything longer is not a version reply.
OLLAMA_VERSION_MAX_BYTES = 4096

VersionGetter = Callable[[str, float], bytes]


def ollama_models_dir() -> Path:
    """Ollama's default models directory, under the home. This script reads no environment
    variable itself -- configuration arrives through `Settings` only -- and the server's own
    `OLLAMA_MODELS` need not be this process's anyway; a model kept elsewhere is recorded as
    `manifest_not_found`, never looked for."""
    return Path.home() / ".ollama" / "models"


def ollama_manifest_path(model: str, models_dir: Path) -> Path | None:
    """The manifest file `model` names (`qwen3.5:4b`, `user/name:tag`), or `None` for a name that
    is not a plain model reference -- it is only ever read, never used to build a request."""
    name, _, tag = model.partition(":")
    parts = name.split("/")
    if len(parts) == 1:
        parts = ["library", *parts]
    if len(parts) == 2:
        parts = [OLLAMA_REGISTRY, *parts]
    tag = tag or "latest"
    if len(parts) != 3 or any(not p or p in {".", ".."} or "\\" in p for p in (*parts, tag)):
        return None
    return models_dir.joinpath("manifests", *parts, tag)


def ollama_manifest(model: str, models_dir: Path | None = None) -> dict[str, Any]:
    """R2.9H. The local manifest's digest and its model layer's, read from disk: no request.

    `manifest_id` is the digest's first twelve characters, the id `ollama list` shows. A field
    the manifest does not give is absent, and a manifest that cannot be read is `unavailable`
    with its reason -- never a digest guessed from the name."""
    path = ollama_manifest_path(
        model, models_dir if models_dir is not None else ollama_models_dir()
    )
    if path is None:
        return {"status": UNAVAILABLE, "reason": "unrecognised_model_name"}
    try:
        raw = path.read_bytes()
    except OSError:
        return {"status": UNAVAILABLE, "reason": "manifest_not_found"}
    digest = hashlib.sha256(raw).hexdigest()
    found: dict[str, Any] = {
        "status": "available",
        "source": "local_manifest",
        "manifest_digest": f"sha256:{digest}",
        "manifest_id": digest[:12],
    }
    try:
        layers = json.loads(raw).get("layers")
    except (ValueError, AttributeError):
        layers = None
    layer = next(
        (
            entry
            for entry in layers or ()
            if isinstance(entry, dict) and entry.get("mediaType") == OLLAMA_MODEL_LAYER
        ),
        None,
    )
    if layer is not None and isinstance(layer.get("digest"), str):
        found["model_layer_digest"] = layer["digest"]
        if isinstance(layer.get("size"), int) and not isinstance(layer.get("size"), bool):
            found["model_layer_bytes"] = layer["size"]
    else:
        found["model_layer"] = UNAVAILABLE
    return found


def _get_version(url: str, timeout: float) -> bytes:
    """GET *url* over the Ollama adapter's own opener: no proxy, no redirect, no body."""
    from app.llm.providers.ollama_provider import build_loopback_opener

    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    with build_loopback_opener().open(request, timeout=timeout) as response:
        raw: bytes = response.read(OLLAMA_VERSION_MAX_BYTES + 1)
    return raw


def ollama_server_version(base_url: str, get: VersionGetter | None = None) -> dict[str, Any]:
    """R2.9H. The local server's version from `GET /api/version`, which asks no model anything.

    Any failure -- not running, refused, a redirect, a malformed or oversized reply -- is
    `unavailable` with its reason; the run is not stopped for it."""
    fetch = get if get is not None else _get_version
    try:
        raw = fetch(base_url.rstrip("/") + OLLAMA_VERSION_PATH, OLLAMA_VERSION_TIMEOUT_SECONDS)
    except (OSError, ValueError, http.client.HTTPException):
        return {"status": UNAVAILABLE, "reason": "unreachable"}
    try:
        version = None if len(raw) > OLLAMA_VERSION_MAX_BYTES else json.loads(raw).get("version")
    except (ValueError, AttributeError):
        version = None
    if not isinstance(version, str) or not version or len(version) > 64:
        return {"status": UNAVAILABLE, "reason": "malformed_reply"}
    return {"status": "available", "source": f"GET {OLLAMA_VERSION_PATH}", "version": version}


def local_model_provenance(settings: Any) -> dict[str, Any] | None:
    """R2.9H. For an `ollama` run, the model's local identity and the server's version, with no
    generation request; `None` for any other provider, whose provenance is unchanged."""
    if settings.llm_provider != "ollama":
        return None
    return {
        "model": settings.effective_llm_model,
        "server_version": ollama_server_version(settings.ollama_base_url),
        "manifest": ollama_manifest(settings.effective_llm_model),
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
    local_model: Mapping[str, Any] | None = None,
    repetition: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The block both output files carry. Built from public values only; never the settings.

    *local_model* (R2.9H, `ollama` only) and *repetition* (R2.9H, a repeated run only) are added
    when given and absent otherwise, so a single run under any other provider records exactly the
    keys it always did."""
    distinct = sorted({model for models in returned_models.values() for model in models})
    extra: dict[str, Any] = {}
    if local_model is not None:
        extra["local_model"] = dict(local_model)
    if repetition is not None:
        extra["repetition"] = dict(repetition)
    return extra | {
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


REPEATS_FORMAT = "copilot_eval_repeats_v1"
REPEATS_STATEMENT = (
    "Counts across the attempts of one repeated run, never rates. Each attempt's own report and "
    "replay file are the record; this file only lists them and counts what they say. The model's "
    "outcome, the answer the caller was served and whether the server intervened are counted "
    "apart, and a case's strict result is the one that attempt's model_level_v2 block recorded -- "
    "passed, failed, or not scored -- never inferred from a served answer."
)


def attempt_rows(attempt: int, identity: str, report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """R2.9H. One row per case of one attempt's report: what each block it carries recorded for
    the case, copied, each in its own field. A block the report lacks adds no field."""
    from tests.evaluation.model_level_v2 import OWN_RESULT

    def by_case(block: str) -> dict[str, Mapping[str, Any]]:
        found = report.get(block)
        return {row["case_id"]: row for row in found["cases"]} if found else {}

    served, legacy, level, checked = (
        by_case(name) for name in ("served", "model_level", "model_level_v2", "integrity_v3")
    )
    rows: list[dict[str, Any]] = []
    for case in report["cases"]:
        case_id = case["case_id"]
        row: dict[str, Any] = {
            "attempt": attempt,
            "attempt_id": identity,
            "case_id": case_id,
            "stop_reason": case["stop_reason"],
            "complete": case["complete"],
            "error_code": case["error_code"],
        }
        if case_id in served:
            row["answer_source"] = served[case_id]["answer_source"]
        if case_id in legacy:
            row["model_level_outcome"] = legacy[case_id]["outcome"]
        if case_id in level:
            assessed = level[case_id]
            strict = OWN_RESULT[assessed["answer_policy"]]
            row |= {
                "model_outcome": assessed["outcome"],
                "model_error_code": assessed["model_error_code"],
                "answer_policy": assessed["answer_policy"],
                "server_intervened": assessed["server_intervened"],
                "contained": assessed["contained"],
                "strict_measure": strict,
                "strict_result": assessed[strict],
            }
        if case_id in checked:
            row["served_text_correct"] = checked[case_id]["served_text_correct"]
        rows.append(row)
    return rows


def repeats_summary(
    attempts: Sequence[Mapping[str, Any]], *, repeat: int, header: Mapping[str, Any]
) -> dict[str, Any]:
    """R2.9H. The `repeats-*.json` aggregate: each attempt's files, every per-attempt row, and per
    case the counts across attempts. Each entry of *attempts* names its files and carries its
    `report`; *header* is what every attempt shares (set, prompt, provider, model, date)."""
    rows = [
        row
        for entry in attempts
        for row in attempt_rows(entry["attempt"], entry["attempt_id"], entry["report"])
    ]
    cases: dict[str, dict[str, Any]] = {}
    for case_id in dict.fromkeys(row["case_id"] for row in rows):
        mine = [row for row in rows if row["case_id"] == case_id]
        outcomes = Counter(row.get("model_outcome", row.get("model_level_outcome")) for row in mine)
        summary: dict[str, Any] = {
            "attempts": len(mine),
            "model_outcomes": dict(sorted((str(k), v) for k, v in outcomes.items())),
        }
        if all("answer_source" in row for row in mine):
            # Nothing served counts as `unavailable`, as the served block's own counts do; the
            # row keeps the attempt's own value, `None`.
            summary["answer_sources"] = dict(
                sorted(Counter(r["answer_source"] or "unavailable" for r in mine).items())
            )
        if all("strict_result" in row for row in mine):
            summary["server_intervened"] = sum(1 for row in mine if row["server_intervened"])
            summary["strict"] = {
                "measure": mine[0]["strict_measure"],
                "passed": sum(1 for row in mine if row["strict_result"] is True),
                "failed": sum(1 for row in mine if row["strict_result"] is False),
                "not_scored": dict(
                    sorted(
                        Counter(
                            row["model_outcome"] for row in mine if row["strict_result"] is None
                        ).items()
                    )
                ),
            }
        cases[case_id] = summary
    return {
        "format": REPEATS_FORMAT,
        "statement": REPEATS_STATEMENT,
        **dict(header),
        "repeat": repeat,
        "attempts": [
            {key: value for key, value in entry.items() if key != "report"}
            | {
                "run_health": entry["report"]["run_health"]["status"],
                "requests": entry["report"]["run_health"]["requests"],
            }
            for entry in attempts
        ],
        "cases": cases,
        "rows": rows,
    }


def main(
    argv: list[str] | None = None,
    *,
    today: Callable[[], dt.date] = dt.date.today,
    monotonic: Callable[[], float] = time.perf_counter,
) -> int:
    options = parse_arguments(argv)

    from app.llm.base import Budget, ChatModel
    from tests.evaluation import (
        diagnostic_annotations,
        harness,
        knowledge_harness,
        v3_pilot_analysis,
    )
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
        # R2.9B: the R2.8AA scoring blocks, under copilot_eval_v3_case_policy_v1 -- v3 only, the
        # one set with a case policy. Added beside the blocks above, never in their place.
        blocks["model_level_v2"] = True
        blocks["integrity_v3"] = True
        blocks["case_policy"] = options.case_policy  # R2.9X: v1 unless named
    recorded_on = today().isoformat()
    repeat: int = options.repeat
    # R2.9H: one attempt writes the two files a run always wrote; a repeated run writes them once
    # per attempt, numbered, and the aggregate beside them. Every one is checked before anything.
    attempt_files = {
        attempt: output_paths(
            options.out, recorded_on, options.prompt, attempt if repeat > 1 else None
        )
        for attempt in range(1, repeat + 1)
    }
    aggregate_path = repeats_path(options.out, recorded_on, options.prompt) if repeat > 1 else None
    try:
        prompt = resolve_prompt(options.prompt)
        if options.case_policy != DEFAULT_CASE_POLICY and full is not COPILOT_EVAL_V3:
            raise RunRefusedError(
                f"--case-policy {options.case_policy} applies to copilot_eval_v3 only; "
                f"{full.identity} has no case policy"
            )
        selected = select_cases(full, options.case)
        check_output(
            options.out,
            [
                *(path for pair in attempt_files.values() for path in pair),
                *([aggregate_path] if aggregate_path is not None else []),
            ],
            isolated=options.prompt != DEFAULT_PROMPT,
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
    # R2.9H: the local model's identity, once per run, with no generation request.
    local_model = local_model_provenance(settings)
    source = source_identity()
    written: list[dict[str, Any]] = []

    for attempt, (replay_path, report_path) in attempt_files.items():
        recordings: dict[str, RecordingModel] = {}
        requests: list[dict[str, Any]] = []
        identity = attempt_id(recorded_on, options.prompt, attempt, repeat)
        if repeat > 1:
            print(f"attempt {attempt} of {repeat} ({identity})", flush=True)

        def model_for(
            case: Any,
            recordings: dict[str, RecordingModel] = recordings,
            requests: list[dict[str, Any]] = requests,
        ) -> ChatModel:
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
            source=source,
            local_model=local_model,
            repetition=(
                {"repeat": repeat, "attempt": attempt, "attempt_id": identity}
                if repeat > 1
                else None
            ),
        )
        report["live_provenance"] = provenance
        report["run_health"] = run_health(requests)
        # R2.8N-W: a derived reading of run_health's diagnostics; it changes no outcome or score.
        report["diagnostic_annotations"] = diagnostic_annotations.annotate(report["run_health"])
        # Only for v3 (R2.8E): the analysis refuses any other prompt, and under v4 that refusal
        # would come after every model call and before either file was written.
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

        # Each attempt's files are written as soon as it ends: a later attempt that cannot finish
        # never takes an earlier one's record with it.
        options.out.mkdir(parents=True, exist_ok=True)
        write_new(replay_path, json.dumps(replays.as_dict(), indent=2, ensure_ascii=False) + "\n")
        write_new(report_path, harness.canonical(report))
        written.append(
            {
                "attempt": attempt,
                "attempt_id": identity,
                "replay_file": replay_path.name,
                # The digests of the bytes on disk, which text mode may have given CRLF endings.
                "replay_sha256": hashlib.sha256(replay_path.read_bytes()).hexdigest(),
                "report_file": report_path.name,
                "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
                "report": report,
            }
        )

        print(report["statement"])
        for measure, counts in report["measures"].items():
            print(f"  {measure}: {counts['passed']} of {counts['scored']}")
        health = report["run_health"]
        print(
            f"  run health: {health['status']} ({health['unavailable_requests']} of "
            f"{health['requests']} model requests unavailable, longest streak "
            f"{health['longest_unavailable_streak']})"
        )

    if aggregate_path is not None:
        aggregate = repeats_summary(
            written,
            repeat=repeat,
            header={
                "question_set": {"identity": selected.identity, "checksum": selected.checksum},
                "prompt": {"identity": prompt.identity, "checksum": prompt.checksum},
                "provider": settings.llm_provider,
                "model": settings.effective_llm_model,
                "recorded_on": recorded_on,
            },
        )
        write_new(aggregate_path, json.dumps(aggregate, indent=2, ensure_ascii=False) + "\n")
        print(f"repeats: {repeat} attempts, aggregate in {aggregate_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
