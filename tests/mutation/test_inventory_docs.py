"""The documented size of the mutation inventory is the inventory's.

`tests/README.md` (Mutation checks) and the root `README.md` (the CI job table) said 79
mutations, from stages 7.6 to 7.14, F2 and F16, five of them with database killers, long after the
inventory had grown to 328 across 29 stages. Every current figure checked here is computed from
`inventory.MUTATIONS`, never written into the test, so the prose cannot drift again unnoticed.
The one fixed fact is which stages the original inventory held; its size is still computed.
Matching ignores line wrapping, because prose is re-wrapped freely.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.mutation.harness import Runner
from tests.mutation.inventory import MUTATIONS

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TESTS_README = REPOSITORY_ROOT / "tests" / "README.md"
README = REPOSITORY_ROOT / "README.md"

#: How the README writes a range of stages: "7.6" EN DASH "7.14".
EN_DASH = chr(0x2013)
#: The inventory's stages, in the order it lists them -- which is the order the stages ran.
STAGES = list(dict.fromkeys(m.stage for m in MUTATIONS))
#: The stages the inventory originally held, before it grew. A historical fact, so written out.
ORIGINAL_STAGES = ("7.6", "7.7", "7.8", "7.9", "7.10", "7.11", "7.12", "7.13", "7.14", "F2", "F16")
#: How the README names that original subset.
ORIGINAL_PHRASE = f"stages 7.6{EN_DASH}7.14, F2 and F16"
#: The total the READMEs used to claim for the whole inventory.
STALE_TOTAL = 79


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def mutation_section() -> str:
    text = TESTS_README.read_text(encoding="utf-8")
    return flat(text.split("## Mutation checks", 1)[1].split("\n## ", 1)[0])


def series(stage: str) -> str:
    """The family a stage belongs to: "7" for 7.6, "H" for H4, "F" for F2-ADR, "R" for R2.2b."""
    found = re.match(r"[A-Z]+|\d+", stage)
    if found is None:
        raise ValueError(f"not a stage name: {stage!r}")
    return found.group(0)


def named_stages(phrase: str) -> list[str]:
    """The stages a phrase like "7.6-7.14, F2, H1-H4 and R2.2b" names (written with an en dash,
    as the README writes ranges), ranges expanded in inventory order. A name the inventory does
    not have raises, and so does a range that crosses from one stage series into another --
    "7.6-R2.2b" would otherwise stand for the whole inventory."""
    named: list[str] = []
    for part in re.split(r", | and ", phrase):
        first, _, last = part.partition(EN_DASH)
        start = STAGES.index(first)
        span = STAGES[start : STAGES.index(last) + 1] if last else [first]
        if not span or {series(stage) for stage in span} != {series(first)}:
            raise ValueError(f"the range {part!r} crosses stage series")
        named.extend(span)
    return named


def sentences(text: str) -> list[str]:
    """*text* split after each full stop that ends a sentence -- not the dots in "7.14". A
    sentence may begin with a digit ("79 mutations is ..."), so that starts one too."""
    return re.split(r"(?<=\.)\s+(?=[A-Z0-9(`*])", text)


def unqualified_claims(text: str, count: int) -> list[str]:
    """The sentences of *text* claiming "<count> mutations" (as a whole number, so "379
    mutations" is not "79 mutations") without saying they describe the original inventory."""
    claim = re.compile(rf"(?<![\d.]){count} mutations\b")
    return [
        sentence
        for sentence in sentences(text)
        if claim.search(sentence) and not ("originally" in sentence and ORIGINAL_PHRASE in sentence)
    ]


# --- the current inventory -----------------------------------------------------------------------


def test_the_tests_readme_states_the_inventory_s_size_and_runners() -> None:
    section = mutation_section()
    pytest_count = sum(m.runner is Runner.PYTEST for m in MUTATIONS)
    vitest_count = sum(m.runner is Runner.VITEST for m in MUTATIONS)
    assert (
        f"{len(MUTATIONS)} mutations across {len(STAGES)} stages, {pytest_count} with pytest "
        f"killers and {vitest_count} with Vitest killers" in section
    )
    database = sum(m.needs_database for m in MUTATIONS)
    assert f"{database} mutations have integration-test killers" in section


def test_the_database_flag_is_exactly_the_mutations_with_an_integration_killer() -> None:
    """`needs_database` is declared by hand; the README calls those mutations the ones with
    integration-test killers. The two must stay the same set, or the README is wrong."""
    flagged = {m.id for m in MUTATIONS if m.needs_database}
    integration = {
        m.id for m in MUTATIONS if any(k.startswith("tests/integration/") for k in m.killers)
    }
    assert flagged == integration


def test_the_tests_readme_states_what_a_local_run_without_database_or_node_runs() -> None:
    section = mutation_section()
    database = [m for m in MUTATIONS if m.needs_database]
    frontend = [m for m in MUTATIONS if m.runner is Runner.VITEST]
    assert not [m for m in frontend if m.needs_database]  # "no Vitest mutation is among them"
    runs = [m for m in MUTATIONS if not m.needs_database and m.runner is Runner.PYTEST]
    assert (
        f"runs {len(runs)} mutations and reports the other {len(MUTATIONS) - len(runs)} as "
        f"*not run* ({len(database)} needing a database, {len(frontend)} frontend)" in section
    )
    assert f"runs all {len(MUTATIONS)}" in section


def test_the_tests_readme_names_every_stage_and_no_other() -> None:
    phrase = re.search(r"Stages (.+?) each broke their own guarantees", mutation_section())
    assert phrase is not None
    named = named_stages(phrase.group(1))
    assert named == STAGES  # every stage, once, in order


def test_the_root_readme_states_the_inventory_s_size() -> None:
    assert f"re-applies the {len(MUTATIONS)} deliberate breaks" in flat(
        README.read_text(encoding="utf-8")
    )


# --- the historical figure -----------------------------------------------------------------------


def test_the_original_inventory_is_described_as_history_with_its_own_counts() -> None:
    original = [m for m in MUTATIONS if m.stage in ORIGINAL_STAGES]
    assert {m.stage for m in original} == set(ORIGINAL_STAGES)
    database = sum(m.needs_database for m in original)
    assert (
        f"originally held only {ORIGINAL_PHRASE}: {len(original)} mutations, {database} of them "
        "needing a database" in mutation_section()
    )


def test_the_stale_counts_are_gone() -> None:
    section = mutation_section()
    assert unqualified_claims(section, STALE_TOTAL) == []
    assert "Five mutations have integration-test killers" not in section
    assert "about ten minutes" not in section
    assert "re-applies the 79 deliberate breaks" not in flat(README.read_text(encoding="utf-8"))


# --- the readers themselves ----------------------------------------------------------------------


def test_the_stage_reader_expands_ranges_and_refuses_unknown_names() -> None:
    assert named_stages(f"7.6{EN_DASH}7.8 and F2") == ["7.6", "7.7", "7.8", "F2"]
    assert named_stages(f"H1{EN_DASH}H4") == ["H1", "H2", "H3", "H4"]
    with pytest.raises(ValueError):
        named_stages("7.6 and Z9")


@pytest.mark.parametrize(
    "phrase",
    [
        f"7.6{EN_DASH}R2.2b",  # the whole inventory in one range
        f"7.14{EN_DASH}F2",  # from one series straight into the next
        f"F2{EN_DASH}F4",  # same letter at both ends, but I4 and M1 lie between them
        f"H4{EN_DASH}H1",  # backwards: names nothing
    ],
)
def test_a_range_may_not_cross_stage_series(phrase: str) -> None:
    with pytest.raises(ValueError):
        named_stages(phrase)


@pytest.mark.parametrize("current", [179, 279, 379])
def test_a_larger_count_ending_in_79_is_not_the_stale_claim(current: int) -> None:
    assert unqualified_claims(f"The inventory has {current} mutations.", STALE_TOTAL) == []


@pytest.mark.parametrize(
    "text",
    [
        "The inventory has 79 mutations.",
        "Those runs are now committed: 79 mutations, each an exact edit.",
        "It originally held 79 mutations.",  # "originally" alone does not say which subset
    ],
)
def test_an_unqualified_claim_of_79_is_refused(text: str) -> None:
    assert unqualified_claims(text, STALE_TOTAL) == [text]


def test_the_historical_sentence_is_allowed() -> None:
    history = f"The inventory originally held only {ORIGINAL_PHRASE}: 79 mutations, 5 of them."
    assert unqualified_claims(history, STALE_TOTAL) == []


#: The README's historical sentence, as a reader would find it.
HISTORY = (
    f"Historically, the inventory originally held only {ORIGINAL_PHRASE}: 79 mutations, 5 of "
    "them needing a database."
)


def test_a_claim_starting_with_a_digit_is_not_absorbed_by_the_historical_sentence() -> None:
    """R2.8N-P: a sentence beginning with a digit was merged into the one before it, so a claim
    right after the historical sentence borrowed its qualification and passed."""
    claim = "79 mutations is the current size."
    assert unqualified_claims(f"{HISTORY} {claim}", STALE_TOTAL) == [claim]
    assert unqualified_claims(HISTORY, STALE_TOTAL) == []  # the history alone is still allowed


@pytest.mark.parametrize("current", [179, 279, 379])
def test_a_digit_initial_larger_count_after_the_history_is_not_the_stale_claim(
    current: int,
) -> None:
    text = f"{HISTORY} {current} mutations is the current size."
    assert unqualified_claims(text, STALE_TOTAL) == []


def test_the_readme_section_rejects_the_digit_initial_claim_after_its_history() -> None:
    """The same, on the real section: the historical sentence there keeps its pass, and a
    digit-initial claim placed straight after it is caught."""
    section = mutation_section()
    assert unqualified_claims(section, STALE_TOTAL) == []
    tail = "That older subset is all the figure 79 describes."
    assert tail in section
    edited = section.replace(tail, "79 mutations is the current size.")
    assert unqualified_claims(edited, STALE_TOTAL) == ["79 mutations is the current size."]
