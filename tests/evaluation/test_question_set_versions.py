"""R2.7R: `copilot_eval_v1` stays frozen; `copilot_eval_v2` is a distinct, pinned version of it.

v1 is the baseline recorded results name, so it must not move: its checksum and its two
historical artifacts (`fixtures/reference_replays.json`, `reference_report.json`) are pinned here
by content digest. v2 differs from v1 in exactly the four questions `REWORDED` lists, has its own
checksum, its own reference exchanges -- v1's hand-written turns, verbatim, so synthetic: no model
wrote them -- and its own pinned report, which CI reproduces exactly as it does v1's.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tests.evaluation import harness
from tests.evaluation.harness import (
    REFERENCE_REPLAYS,
    REFERENCE_REPLAYS_V2,
    REFERENCE_REPORT,
    REFERENCE_REPORT_V2,
    REFERENCE_STATEMENT,
    canonical,
    reference_report,
    reference_report_v2,
)
from tests.evaluation.questions import COPILOT_EVAL_V1
from tests.evaluation.questions_v2 import COPILOT_EVAL_V2, REWORDED
from tests.evaluation.replay import REFERENCE_PROVIDER, load_replays

V1_CHECKSUM = "1ca6bdc9a9916e8c05b93320d2f99b549b325e825de78309572c0874956784f6"
V2_CHECKSUM = "6e9b755c8907bbf8baabb7dde3b79f3424e3d10468c103fd777845e19a67228b"

#: SHA-256 of v1's historical artifacts, LF-normalised -- the bytes as of R2.7R, never regenerated.
V1_ARTIFACT_DIGESTS = {
    REFERENCE_REPLAYS: "e74c48912ebab489a99b92a8a8bbc8d9d28b87af084695ec43235728e3971061",
    REFERENCE_REPORT: "a7e3f7244926f4421d2e37018d0d4a00ef46be15822d0bf01ed3aa4f58fd801c",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


# --- v1: frozen ----------------------------------------------------------------------------------


def test_v1_keeps_its_identity_and_checksum() -> None:
    assert (COPILOT_EVAL_V1.identity, COPILOT_EVAL_V1.checksum) == ("copilot_eval_v1", V1_CHECKSUM)


def test_v1_historical_artifacts_are_byte_for_byte_unchanged() -> None:
    for path, expected in V1_ARTIFACT_DIGESTS.items():
        assert digest(path) == expected, path.name


def test_v1_reference_report_is_still_reproduced_from_its_own_exchanges() -> None:
    assert canonical(reference_report()) == REFERENCE_REPORT.read_text(encoding="utf-8")


# --- v2: distinct, and different in exactly the reworded questions -------------------------------


def test_v2_has_its_own_identity_and_checksum() -> None:
    assert (COPILOT_EVAL_V2.identity, COPILOT_EVAL_V2.checksum) == ("copilot_eval_v2", V2_CHECKSUM)
    assert COPILOT_EVAL_V2.checksum != COPILOT_EVAL_V1.checksum


def test_v2_differs_from_v1_only_in_the_four_reworded_questions() -> None:
    assert set(REWORDED) == {
        "kpis_may_occupancy",
        "daily_busiest_day",
        "accuracy_manager",
        "may_occupancy_and_busiest_day",
    }
    assert len(COPILOT_EVAL_V2.cases) == len(COPILOT_EVAL_V1.cases) == 24
    for old, new in zip(COPILOT_EVAL_V1.cases, COPILOT_EVAL_V2.cases, strict=True):
        assert (old.case_id, old.category, old.role, old.expected) == (
            new.case_id,
            new.category,
            new.role,
            new.expected,
        )
        assert (old.calls, old.figures, old.tolerated_tools) == (
            new.calls,
            new.figures,
            new.tolerated_tools,
        )
        assert (old.question != new.question) is (old.case_id in REWORDED), old.case_id
        if old.case_id in REWORDED:
            assert new.question == REWORDED[old.case_id]


# --- v2's reference artifacts ------------------------------------------------------------------


def test_v2_reference_exchanges_are_v1s_hand_written_turns_bound_to_v2() -> None:
    v1 = load_replays(REFERENCE_REPLAYS, COPILOT_EVAL_V1)
    v2 = load_replays(REFERENCE_REPLAYS_V2, COPILOT_EVAL_V2)  # refuses any other set

    assert v2.cases == v1.cases
    assert (v2.question_set, v2.question_set_checksum) == ("copilot_eval_v2", V2_CHECKSUM)
    assert v2.provider == REFERENCE_PROVIDER  # synthetic, not a model's recording


def test_v2_reference_run_matches_its_pinned_report() -> None:
    """The CI gate for v2, as `test_harness` has for v1: a code change that alters what the
    copilot does with these fixed exchanges changes this report, and is reviewed as one."""
    assert canonical(reference_report_v2()) == REFERENCE_REPORT_V2.read_text(encoding="utf-8")


def test_v2_reference_report_says_no_model_was_evaluated_and_passes_every_measure() -> None:
    report = json.loads(REFERENCE_REPORT_V2.read_text(encoding="utf-8"))

    assert report["statement"] == REFERENCE_STATEMENT
    assert report["mode"] == "replay"
    assert report["question_set"] == {
        "identity": "copilot_eval_v2",
        "checksum": V2_CHECKSUM,
        "size": 24,
    }
    for name, counts in report["measures"].items():
        assert counts["passed"] == counts["scored"], name


def test_a_live_run_names_the_set_it_ran() -> None:
    v2 = harness.live_statement("vendor", "model-x", "2026-10-22", 1, COPILOT_EVAL_V2.identity)
    v1 = harness.live_statement("vendor", "model-x", "2026-10-22", 1)

    assert "copilot_eval_v2" in v2 and "copilot_eval_v1" not in v2
    assert "copilot_eval_v1" in v1
