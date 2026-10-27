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
    REFERENCE_REPLAYS_V2_ANSWER_V3,
    REFERENCE_REPORT,
    REFERENCE_REPORT_V2,
    REFERENCE_REPORT_V2_ANSWER_V3,
    REFERENCE_STATEMENT,
    REVIEWED_DELTAS,
    apply_reviewed_deltas,
    canonical,
    expected_report,
    reference_report,
    reference_report_v2,
    reference_report_v2_answer_v3,
)
from tests.evaluation.knowledge_harness import REFERENCE_REPLAYS as KNOWLEDGE_REFERENCE_REPLAYS
from tests.evaluation.knowledge_harness import REFERENCE_REPORT as KNOWLEDGE_REFERENCE_REPORT
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
    v1 = load_replays(REFERENCE_REPLAYS, COPILOT_EVAL_V1, prompt=harness.REFERENCE_PROMPT)
    # refuses any other set
    v2 = load_replays(REFERENCE_REPLAYS_V2, COPILOT_EVAL_V2, prompt=harness.REFERENCE_PROMPT)

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


#: The cases whose `tools_offered` is reviewed: refused before any model (R2.7ZL, R2.7ZT).
REFUSED_BEFORE_THE_MODEL = ("inject_other_hotel", "another_property")


def test_the_reviewed_deltas_are_exactly_the_ones_approved() -> None:
    assert REVIEWED_DELTAS == {
        ("inject_other_hotel", "tools_offered"): [],  # R2.7ZL
        ("another_property", "tools_offered"): [],  # R2.7ZT, approved in that stage
    }


@pytest.mark.parametrize("path", [REFERENCE_REPORT, REFERENCE_REPORT_V2], ids=["v1", "v2"])
def test_each_reviewed_delta_is_still_needed_and_changes_only_its_field(path: Path) -> None:
    """Not stale: the pinned file still lists the catalogue the current code no longer offers,
    and applying the deltas changes that one field of those cases and nothing else."""
    before = pinned(path)
    after = apply_reviewed_deltas(before)

    for case_id in REFUSED_BEFORE_THE_MODEL:
        assert case_of(before, case_id)["tools_offered"] != []
        assert case_of(after, case_id)["tools_offered"] == []
    assert {k: v for k, v in after.items() if k != "cases"} == {
        k: v for k, v in before.items() if k != "cases"
    }
    for old, new in zip(before["cases"], after["cases"], strict=True):
        changed = {field for field in old if old[field] != new[field]}
        assert set(new) == set(old)
        expected = {"tools_offered"} if old["case_id"] in REFUSED_BEFORE_THE_MODEL else set()
        assert changed == expected


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
    prompt = harness.REFERENCE_PROMPT
    v2 = harness.live_statement(
        "vendor", "model-x", "2026-10-22", 1, COPILOT_EVAL_V2.identity, prompt=prompt
    )
    v1 = harness.live_statement("vendor", "model-x", "2026-10-22", 1, prompt=prompt)

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


# --- R2.7ZN: every reference run is still made under copilot_answer@v2 ----------------------------


def test_every_reference_run_names_and_runs_under_v2() -> None:
    from app.llm.prompts.registry import COPILOT_ANSWER_V2
    from tests.evaluation import knowledge_harness

    expected = {"identity": COPILOT_ANSWER_V2.identity, "checksum": COPILOT_ANSWER_V2.checksum}
    for report in (reference_report(), reference_report_v2(), knowledge_harness.reference_report()):
        assert report["prompt"] == expected


# --- R2.7ZP: a separate copilot_answer@v3 baseline, beside the frozen ones ---------------------
#
# A code-regression baseline: copilot_eval_v2's hand-written turns, bound to v3. No model wrote
# them, and a replay ignores the prompt it is rendered under -- so it shows the pipeline runs under
# v3, never how a model follows v3's rules.

V3_PROMPT = (
    "copilot_answer@v3",
    "04c4b19cfd614284aae7fc6456ccff4e498772df68a13561c1303fff90da056b",
)

#: SHA-256 of every frozen reference report, LF-normalised. None is regenerated for v3.
FROZEN_REPORT_DIGESTS = {
    REFERENCE_REPORT: "a7e3f7244926f4421d2e37018d0d4a00ef46be15822d0bf01ed3aa4f58fd801c",
    REFERENCE_REPORT_V2: "e998ee02886e2a33bd539396e5a5c6d08f2474016844ec73b8952dc72d78dbf7",
    KNOWLEDGE_REFERENCE_REPORT: "5bb1d69e8f16a27029566c89a2433567c85078cfb3b46c6407112a05a1fcb19b",
}

FROZEN_PATHS = {
    REFERENCE_REPLAYS,
    REFERENCE_REPORT,
    REFERENCE_REPLAYS_V2,
    REFERENCE_REPORT_V2,
    KNOWLEDGE_REFERENCE_REPLAYS,
    KNOWLEDGE_REFERENCE_REPORT,
}


def test_the_frozen_reports_are_byte_for_byte_unchanged() -> None:
    for path, expected in FROZEN_REPORT_DIGESTS.items():
        assert digest(path) == expected, path.name


def test_the_v3_baseline_is_selected_by_name_and_production_stays_on_v2() -> None:
    from app.llm.prompts.registry import COPILOT_ANSWER_V2, COPILOT_ANSWER_V3
    from app.services.copilot import PROMPT

    assert harness.BASELINE_V3_PROMPT is COPILOT_ANSWER_V3
    assert (COPILOT_ANSWER_V3.identity, COPILOT_ANSWER_V3.checksum) == V3_PROMPT
    assert harness.REFERENCE_PROMPT is COPILOT_ANSWER_V2
    assert PROMPT is COPILOT_ANSWER_V2


def test_the_v3_baseline_has_paths_of_its_own() -> None:
    new = {REFERENCE_REPLAYS_V2_ANSWER_V3, REFERENCE_REPORT_V2_ANSWER_V3}
    assert len(new) == 2
    assert not new & FROZEN_PATHS
    assert {path.name for path in new}.isdisjoint({path.name for path in FROZEN_PATHS})


def test_the_v3_baseline_is_v2s_hand_written_turns_bound_to_v3() -> None:
    v2 = load_replays(REFERENCE_REPLAYS_V2, COPILOT_EVAL_V2, prompt=harness.REFERENCE_PROMPT)
    v3 = load_replays(
        REFERENCE_REPLAYS_V2_ANSWER_V3, COPILOT_EVAL_V2, prompt=harness.BASELINE_V3_PROMPT
    )

    assert v3.cases == v2.cases
    assert (v3.prompt, v3.prompt_checksum) == V3_PROMPT
    assert (v3.question_set, v3.question_set_checksum) == ("copilot_eval_v2", V2_CHECKSUM)
    assert (v3.provider, v3.model, v3.recorded_on) == (v2.provider, v2.model, v2.recorded_on)
    assert v3.provider == REFERENCE_PROVIDER  # synthetic, not a model's recording


def rebound(tmp_path: Path, **changes: Any) -> Path:
    raw = json.loads(REFERENCE_REPLAYS_V2_ANSWER_V3.read_text(encoding="utf-8"))
    raw.update(changes)
    target = tmp_path / "replays.json"
    target.write_text(json.dumps(raw), encoding="utf-8")
    return target


def test_the_v3_baseline_is_refused_under_any_other_prompt_or_set() -> None:
    from app.llm.prompts.registry import COPILOT_ANSWER_V1, COPILOT_ANSWER_V2

    for other in (COPILOT_ANSWER_V1, COPILOT_ANSWER_V2):
        with pytest.raises(ValueError, match="another prompt"):
            load_replays(REFERENCE_REPLAYS_V2_ANSWER_V3, COPILOT_EVAL_V2, prompt=other)
    with pytest.raises(ValueError, match="another prompt"):  # and v2's file is refused under v3
        load_replays(REFERENCE_REPLAYS_V2, COPILOT_EVAL_V2, prompt=harness.BASELINE_V3_PROMPT)
    with pytest.raises(ValueError, match="another question set"):
        load_replays(
            REFERENCE_REPLAYS_V2_ANSWER_V3, COPILOT_EVAL_V1, prompt=harness.BASELINE_V3_PROMPT
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"prompt_checksum": "0" * 64},  # the right identity, a wrong checksum
        {"prompt": "copilot_answer@v4"},  # an unknown version, v3's checksum
        {"prompt": "copilot_answer@v2"},  # another version, v3's checksum
    ],
    ids=["wrong_checksum", "unknown_version", "mismatched_version"],
)
def test_a_v3_baseline_file_with_another_identity_or_checksum_is_refused(
    tmp_path: Path, changes: dict[str, str]
) -> None:
    with pytest.raises(ValueError, match="another prompt"):
        load_replays(
            rebound(tmp_path, **changes), COPILOT_EVAL_V2, prompt=harness.BASELINE_V3_PROMPT
        )


def test_the_v3_baseline_run_matches_its_report_exactly() -> None:
    """Its own gate, with no reviewed deltas: the report was written by the current code."""
    assert canonical(reference_report_v2_answer_v3()) == REFERENCE_REPORT_V2_ANSWER_V3.read_text(
        encoding="utf-8"
    )


def test_the_v3_baseline_differs_from_v2s_only_in_its_prompt_and_statement() -> None:
    """The replayed turns ignore the prompt, so every case and measure is v2's -- the evidence
    that this baseline cannot tell v3 from v2 in a model's behaviour, and says so."""
    v3 = json.loads(REFERENCE_REPORT_V2_ANSWER_V3.read_text(encoding="utf-8"))
    v2 = json.loads(expected_report(REFERENCE_REPORT_V2))

    assert {key for key in v2.keys() | v3.keys() if v2.get(key) != v3.get(key)} == {
        "prompt",
        "statement",
    }
    assert v3["prompt"] == {"identity": V3_PROMPT[0], "checksum": V3_PROMPT[1]}
    assert v3["statement"] == harness.BASELINE_V3_STATEMENT
    assert v3["statement"].startswith(REFERENCE_STATEMENT)
    assert "not evidence of how any model follows its rules" in v3["statement"]
    assert (v3["mode"], v3["model"]["provider"]) == ("replay", REFERENCE_PROVIDER)


def test_writing_the_v3_baseline_writes_only_its_own_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    frozen = {}
    for name in ("REFERENCE_REPORT", "REFERENCE_REPORT_V2"):
        copy = tmp_path / f"{name}.json"
        copy.write_bytes(getattr(harness, name).read_bytes())
        frozen[copy] = copy.read_bytes()
        monkeypatch.setattr(harness, name, copy)
    target = tmp_path / "reference_report_v2_answer_v3.json"
    monkeypatch.setattr(harness, "REFERENCE_REPORT_V2_ANSWER_V3", target)

    harness.main(["--set", "copilot_eval_v2", "--prompt", "copilot_answer@v3", "--write-reference"])

    assert target.read_text(encoding="utf-8") == canonical(reference_report_v2_answer_v3())
    assert capsys.readouterr().out == "wrote tests/evaluation/reference_report_v2_answer_v3.json\n"
    for copy, original in frozen.items():
        assert copy.read_bytes() == original
    for path, expected in FROZEN_REPORT_DIGESTS.items():
        assert digest(path) == expected, path.name


def test_printing_the_v3_baseline_writes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    harness.main(["--set", "copilot_eval_v2", "--prompt", "copilot_answer@v3"])

    assert (
        capsys.readouterr().out == REFERENCE_REPORT_V2_ANSWER_V3.read_text(encoding="utf-8") + "\n"
    )


def test_without_a_prompt_the_command_line_still_runs_v2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    harness.main(["--set", "copilot_eval_v2"])

    printed = json.loads(capsys.readouterr().out)
    assert printed == json.loads(expected_report(REFERENCE_REPORT_V2))
    assert printed["prompt"]["identity"] == "copilot_answer@v2"


def never(*args: object) -> None:
    raise AssertionError("a report was computed before the refusal")


@pytest.mark.parametrize(
    "argv",
    [
        ["--prompt", "copilot_answer@v3", "--write-reference"],
        ["--set", "copilot_eval_v1", "--prompt", "copilot_answer@v3", "--write-reference"],
    ],
    ids=["default_set", "explicit_v1"],
)
def test_v3_does_not_open_a_way_to_write_v1(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("reference_report", "reference_report_v2", "reference_report_v2_answer_v3"):
        monkeypatch.setattr(harness, name, never)

    with pytest.raises(SystemExit) as refused:
        harness.main(argv)

    assert refused.value.code == 2
    assert FROZEN_V1_REFUSAL in capsys.readouterr().err
    assert digest(REFERENCE_REPORT) == V1_ARTIFACT_DIGESTS[REFERENCE_REPORT]


def test_v3_has_no_baseline_for_v1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("reference_report", "reference_report_v2", "reference_report_v2_answer_v3"):
        monkeypatch.setattr(harness, name, never)

    with pytest.raises(SystemExit) as refused:
        harness.main(["--set", "copilot_eval_v1", "--prompt", "copilot_answer@v3"])

    assert refused.value.code == 2
    assert harness.V3_BASELINE_SET_REFUSAL in capsys.readouterr().err


@pytest.mark.parametrize("prompt", ["copilot_answer@v1", "copilot_answer@v4", "v3"])
def test_an_unknown_or_unsupported_prompt_is_refused(
    prompt: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("reference_report", "reference_report_v2", "reference_report_v2_answer_v3"):
        monkeypatch.setattr(harness, name, never)

    with pytest.raises(SystemExit) as refused:
        harness.main(["--set", "copilot_eval_v2", "--prompt", prompt, "--write-reference"])

    assert refused.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
