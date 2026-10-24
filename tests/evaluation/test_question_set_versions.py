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
from typing import Any, cast

import pytest

from tests.evaluation import harness
from tests.evaluation.harness import (
    FROZEN_V1_REFUSAL,
    REFERENCE_REPLAYS,
    REFERENCE_REPLAYS_V2,
    REFERENCE_REPORT,
    REFERENCE_REPORT_V2,
    REFERENCE_STATEMENT,
    REVIEWED_DELTAS,
    apply_reviewed_deltas,
    canonical,
    expected_report,
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
    """Exactly, except for the reviewed deltas (R2.7ZL) -- which are applied to a copy, so the
    frozen file keeps its digest above."""
    assert canonical(reference_report()) == expected_report(REFERENCE_REPORT)


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
    copilot does with these fixed exchanges changes this report, and is reviewed as one -- here,
    as a reviewed delta (R2.7ZL), so that the pinned file itself stays as it is."""
    assert canonical(reference_report_v2()) == expected_report(REFERENCE_REPORT_V2)


# --- R2.7ZL: the reviewed deltas -------------------------------------------------------------


def pinned(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def case_of(report: dict[str, Any], case_id: str) -> dict[str, Any]:
    [case] = [c for c in report["cases"] if c["case_id"] == case_id]
    return cast(dict[str, Any], case)


def test_the_reviewed_deltas_are_exactly_the_one_approved() -> None:
    assert REVIEWED_DELTAS == {("inject_other_hotel", "tools_offered"): []}


@pytest.mark.parametrize("path", [REFERENCE_REPORT, REFERENCE_REPORT_V2], ids=["v1", "v2"])
def test_the_reviewed_delta_is_still_needed_and_changes_only_its_field(path: Path) -> None:
    """Not stale: the pinned file still lists the catalogue the current code no longer offers,
    and applying the delta changes that one field of that one case and nothing else."""
    before = pinned(path)
    after = apply_reviewed_deltas(before)

    assert case_of(before, "inject_other_hotel")["tools_offered"] != []
    assert case_of(after, "inject_other_hotel")["tools_offered"] == []
    assert {k: v for k, v in after.items() if k != "cases"} == {
        k: v for k, v in before.items() if k != "cases"
    }
    for old, new in zip(before["cases"], after["cases"], strict=True):
        changed = {field for field in old if old[field] != new[field]}
        assert set(new) == set(old)
        assert changed == ({"tools_offered"} if old["case_id"] == "inject_other_hotel" else set())


def test_applying_the_deltas_leaves_the_pinned_data_and_files_untouched() -> None:
    report = pinned(REFERENCE_REPORT)
    untouched = json.dumps(report, sort_keys=True)

    apply_reviewed_deltas(report)

    assert json.dumps(report, sort_keys=True) == untouched
    assert digest(REFERENCE_REPORT) == V1_ARTIFACT_DIGESTS[REFERENCE_REPORT]


@pytest.mark.parametrize(
    "deltas",
    [
        {("inject_estimate", "tools_offered"): []},  # an unreviewed case
        {("inject_other_hotel", "tools_called"): []},  # an unreviewed field
        {("inject_other_hotel", "tools_offered"): ["get_hotel_kpis"]},  # another value
    ],
    ids=["unknown_case", "unknown_field", "other_value"],
)
def test_nothing_but_the_reviewed_delta_is_accepted(
    deltas: dict[tuple[str, str], list[str]],
) -> None:
    with pytest.raises(ValueError, match="not a reviewed delta"):
        apply_reviewed_deltas(pinned(REFERENCE_REPORT), deltas)


def test_a_report_without_the_case_or_the_field_is_refused() -> None:
    report = pinned(REFERENCE_REPORT)
    kept = [case for case in report["cases"] if case["case_id"] != "inject_other_hotel"]
    with pytest.raises(ValueError, match="no case"):
        apply_reviewed_deltas({**report, "cases": kept})

    without_field = pinned(REFERENCE_REPORT)
    del case_of(without_field, "inject_other_hotel")["tools_offered"]
    with pytest.raises(ValueError, match="no field"):
        apply_reviewed_deltas(without_field)


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


# --- R2.7ZL4: the command line cannot overwrite v1's frozen report -------------------------------
#
# Every test points the harness's write targets at temporary copies, so a broken guard could
# never reach the real files; their digests are checked as well.


@pytest.mark.parametrize(
    "argv",
    [["--write-reference"], ["--set", "copilot_eval_v1", "--write-reference"]],
    ids=["default_set", "explicit_v1"],
)
def test_writing_the_frozen_v1_report_is_refused_and_writes_nothing(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "reference_report.json"
    target.write_bytes(REFERENCE_REPORT.read_bytes())
    original = target.read_bytes()
    monkeypatch.setattr(harness, "REFERENCE_REPORT", target)

    def never(*args: object) -> None:
        raise AssertionError("a report was computed before the refusal")

    monkeypatch.setattr(harness, "reference_report", never)

    with pytest.raises(SystemExit) as refused:
        harness.main(argv)

    assert refused.value.code == 2
    assert FROZEN_V1_REFUSAL in capsys.readouterr().err
    assert target.read_bytes() == original
    assert digest(REFERENCE_REPORT) == V1_ARTIFACT_DIGESTS[REFERENCE_REPORT]


def test_printing_the_v1_report_still_works(capsys: pytest.CaptureFixture[str]) -> None:
    """Read-only: it prints what the current code produces -- the pinned report with the
    reviewed deltas applied -- and writes nothing."""
    harness.main([])

    assert capsys.readouterr().out == expected_report(REFERENCE_REPORT) + "\n"
    assert digest(REFERENCE_REPORT) == V1_ARTIFACT_DIGESTS[REFERENCE_REPORT]


def test_writing_the_unfrozen_v2_report_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real_v2 = digest(REFERENCE_REPORT_V2)
    target = tmp_path / "reference_report_v2.json"
    monkeypatch.setattr(harness, "REFERENCE_REPORT_V2", target)

    harness.main(["--set", "copilot_eval_v2", "--write-reference"])

    assert target.read_text(encoding="utf-8") == canonical(reference_report_v2())
    assert capsys.readouterr().out == "wrote tests/evaluation/reference_report_v2.json\n"
    assert digest(REFERENCE_REPORT_V2) == real_v2
