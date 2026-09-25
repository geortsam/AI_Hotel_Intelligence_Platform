"""Run an evaluation set against the REAL configured language model, and record it (Stage 7.8).

This is the only way this repository evaluates an actual model, and it is never run by CI. It
makes paid API calls -- one question at a time, up to four model calls each -- so it refuses to
start without `--confirm-paid-api-call`.

    LLM_ENABLED=true LLM_API_KEY=... \\
        python scripts/copilot_live_eval.py --out eval-output --confirm-paid-api-call

`--set copilot_knowledge_eval_v1` (Stage 7.10) runs the document questions instead of the default
`copilot_eval_v1`. Either way the recording names the prompt the copilot actually renders --
`app.services.copilot.PROMPT`, `copilot_answer@v2` since Stage 7.10 -- never a prompt it did not
use.

Configuration comes from `Settings`, exactly as the application reads it: provider, model, key,
timeout and token ceiling. The script never prints, logs or writes the key. It requires the
optional provider SDK (`pip install -r backend/requirements-llm.txt`).

What it runs is the same stack CI replays through: the real copilot service, tool loop, tool
invocation boundary and figure check, over the fixed evaluation hotel. Only the model is live.

It writes two files to `--out`:

- `replays-<date>.json` -- every model turn, in the format CI replays. Committing it (after
  review) turns this run into a reproducible fixture.
- `report-<date>.json` -- the scored report. Its `statement` says what it does and does not
  mean: one model, one date, this question set, this prompt; no aggregate accuracy; nothing
  about the demand model.

The files contain the evaluation questions and the model's answers about a FICTIONAL hotel --
no production data -- and are not committed automatically.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "backend"))


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out", required=True, type=Path, help="Directory to write results to.")
    parser.add_argument(
        "--confirm-paid-api-call",
        required=True,
        action="store_true",
        help="Required. Acknowledges that this calls the configured provider and costs money.",
    )
    parser.add_argument(
        "--set",
        dest="question_set",
        choices=["copilot_eval_v1", "copilot_knowledge_eval_v1"],
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


def main(argv: list[str] | None = None) -> int:
    options = parse_arguments(argv)

    from dataclasses import replace
    from typing import Any

    from app.core.config import Settings
    from app.llm.base import Budget, ChatModel
    from app.llm.factory import build_chat_model
    from app.services.copilot import PROMPT
    from tests.evaluation import harness, knowledge_harness
    from tests.evaluation.knowledge_questions import COPILOT_KNOWLEDGE_EVAL_V1
    from tests.evaluation.questions import COPILOT_EVAL_V1
    from tests.evaluation.replay import RecordingModel, Replays

    settings = Settings()
    if not settings.llm_enabled or not settings.llm_api_key:
        print("LLM_ENABLED must be true and LLM_API_KEY set.", file=sys.stderr)
        return 2

    full: Any = (
        COPILOT_KNOWLEDGE_EVAL_V1
        if options.question_set == "copilot_knowledge_eval_v1"
        else COPILOT_EVAL_V1
    )
    runner: Any = knowledge_harness.run if full is COPILOT_KNOWLEDGE_EVAL_V1 else harness.run
    question_set = full
    if options.case:
        unknown = set(options.case) - {case.case_id for case in full.cases}
        if unknown:
            print(f"unknown case ids: {sorted(unknown)}", file=sys.stderr)
            return 2
        question_set = replace(
            full, cases=tuple(case for case in full.cases if case.case_id in options.case)
        )

    model = build_chat_model(settings)
    recorded_on = dt.date.today().isoformat()
    recordings: dict[str, RecordingModel] = {}

    def model_for(case: Any) -> ChatModel:
        recorder = RecordingModel(model)
        recordings[case.case_id] = recorder
        print(f"  {case.case_id} ...", flush=True)
        return recorder

    report = runner(
        question_set,
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

    replays = Replays(
        provider=settings.llm_provider,
        model=settings.llm_model,
        recorded_on=recorded_on,
        question_set=full.identity,
        question_set_checksum=full.checksum,
        prompt=PROMPT.identity,
        prompt_checksum=PROMPT.checksum,
        cases={case_id: recorder.turns() for case_id, recorder in recordings.items()},
    )

    options.out.mkdir(parents=True, exist_ok=True)
    (options.out / f"replays-{recorded_on}.json").write_text(
        json.dumps(replays.as_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (options.out / f"report-{recorded_on}.json").write_text(
        harness.canonical(report), encoding="utf-8"
    )

    print(report["statement"])
    for measure, counts in report["measures"].items():
        print(f"  {measure}: {counts['passed']} of {counts['scored']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
